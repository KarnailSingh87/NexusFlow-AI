"""Task-routed AI service layer over the Nebius Token Factory.

This module sits *above* :class:`~app.services.nebius.client.NebiusClient` rather
than beside it. The client already owns the transport concerns that must not be
re-implemented per call site: the pinned ``/v1`` base URL, ``Bearer`` auth from
``NEBIUS_API_KEY``, exponential backoff with jitter, ``Retry-After`` handling,
and the typed error hierarchy. Duplicating any of that here would give the
service layer a second, silently divergent retry policy.

What this layer adds is the part the client cannot know — *which model should
answer this kind of request*:

======================  ===============================================
Task                    Route
======================  ===============================================
``chat``                configured default (no routing opinion)
``summarize``           Nemotron 3 Nano — cheap, high volume
``extract_entities``    Nemotron 3 Nano — cheap, schema-bound
``classify``            Nemotron 3 Nano — latency-sensitive, closed set
``draft``               Nemotron 3.5 Lightning — interactive execution
``code``                Nemotron 3 Super — multi-step reasoning
``reason``              Nemotron 3 Super — multi-step reasoning
``deep_audit``          Nemotron 3 Ultra — frontier reliability
``embed``               Qwen3 Embedding 8B
======================  ===============================================

Every route is a decision *record*, not just a model string, so callers can log
or bill against the rationale. Model IDs resolve through
:class:`~app.core.config.Settings` at call time, so an operator can repoint any
single task without touching code and without this module caching stale values.

Structured output
-----------------
``response_format={"type": "json_object"}`` is a *request*, not a guarantee.
Nemotron 3 Nano and Ultra are reasoning models: they emit a reasoning trace
before the final answer, and may wrap the object in a ```json fence or precede it
with prose. :meth:`AIService.chat_json` therefore requests JSON mode and then
*verifies* it — extracting the outermost balanced object, and if that fails,
retrying once with the offending output fed back as a correction.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Self

from app.core.config import Settings, settings
from app.core.logging import get_logger
from app.core.metrics import TokenUsage, metrics
from app.core.metrics import TokenUsage as MetricTokenUsage
from app.services.nebius.client import (
    NebiusClient,
    NebiusError,
    NebiusRateLimitError,
)
from app.services.nebius.registry import get_model_card

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Task vocabulary
# ---------------------------------------------------------------------------
#: Workload shapes the routing table understands. Kept a Literal so a typo at a
#: call site is a type error, not a silent promotion to a frontier model.
TaskType = Literal[
    "chat",
    "summarize",
    "extract_entities",
    "classify",
    "draft",
    "code",
    "reason",
    "deep_audit",
    "embed",
]

#: Every accepted task, in routing order. Used for error messages and for the
#: tests that assert the table has no unreachable entries.
KNOWN_TASKS: Final[tuple[TaskType, ...]] = (
    "chat",
    "summarize",
    "extract_entities",
    "classify",
    "draft",
    "code",
    "reason",
    "deep_audit",
    "embed",
)


class UnknownTaskError(ValueError):
    """Raised when a task type is absent from the routing table."""

    def __init__(self, task_type: str) -> None:
        super().__init__(f"Unknown task_type {task_type!r}. Known tasks: {', '.join(KNOWN_TASKS)}")
        self.task_type = task_type


class StructuredOutputError(NebiusError):
    """Raised when a model would not produce parseable JSON.

    Subclasses :class:`NebiusError` so the existing FastAPI exception handlers
    translate it into a 502 without new wiring.
    """

    status_code = 502


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TaskRoute:
    """The routing decision for one task type."""

    task_type: TaskType
    #: Attribute name on :class:`Settings` holding the model ID. Looked up at
    #: call time rather than captured at import, so ``Settings`` overrides and
    #: test doubles are honoured.
    setting_name: str
    tier: str
    #: Why this model, surfaced in logs and available to callers.
    rationale: str
    #: Sampling temperature. ``None`` defers to ``LLM_DEFAULT_TEMPERATURE``.
    temperature: float | None
    max_tokens: int
    #: True when this route calls the embeddings endpoint, not chat completions.
    embedding: bool = False

    def resolve(self, config: Settings | None = None) -> str:
        """Resolve the concrete model ID for this route."""
        cfg = config or settings
        model: str = (getattr(cfg, self.setting_name) or "").strip()
        if not model:
            raise ValueError(
                f"Task {self.task_type!r} resolved to an empty model via "
                f"{self.setting_name.upper()}."
            )
        return model

    def resolved_temperature(self, config: Settings | None = None) -> float:
        """Return the sampling temperature, deferring to the configured default."""
        cfg = config or settings
        return cfg.llm_default_temperature if self.temperature is None else self.temperature

    def label(self, config: Settings | None = None) -> str | None:
        """Display label for the resolved model, when the registry knows it."""
        card = get_model_card(self.resolve(config))
        return card.label if card else None


#: Complexity is bought deliberately: reasoning tiers cost up to ~12x more per
#: output token than Nano, so the default must be the cheapest model that can do
#: the job rather than the strongest one available.
TASK_ROUTES: Final[Mapping[str, TaskRoute]] = {
    "chat": TaskRoute(
        task_type="chat",
        setting_name="nemotron_default_model",
        tier="default",
        rationale="Operator's configured default; no routing opinion applied.",
        temperature=None,
        max_tokens=2048,
    ),
    "summarize": TaskRoute(
        task_type="summarize",
        setting_name="nemotron_nano_model",
        tier="fast",
        rationale="Compression is cheap and high-volume; Nano costs a fraction of "
        "Super per output token and needs no deep reasoning.",
        temperature=0.2,
        max_tokens=1024,
    ),
    "extract_entities": TaskRoute(
        task_type="extract_entities",
        setting_name="nemotron_nano_model",
        tier="fast",
        rationale="Schema-bound extraction with a small fixed output; a reasoning "
        "tier adds latency and cost for no accuracy gain.",
        temperature=0.0,
        max_tokens=2048,
    ),
    "classify": TaskRoute(
        task_type="classify",
        setting_name="nemotron_nano_model",
        tier="fast",
        rationale="Closed-set labelling is cheap and latency-sensitive when it "
        "sits in a UI request path.",
        temperature=0.0,
        max_tokens=512,
    ),
    "draft": TaskRoute(
        task_type="draft",
        setting_name="nemotron_fast_model",
        tier="fast",
        rationale="Interactive drafting wants latency over depth; Lightning is "
        "tuned as the always-on execution layer.",
        temperature=None,
        max_tokens=2048,
    ),
    "code": TaskRoute(
        task_type="code",
        setting_name="nemotron_balanced_model",
        tier="balanced",
        rationale="Code generation and review need multi-step reasoning but not "
        "frontier depth; Super is the cost/latency optimum.",
        temperature=0.2,
        max_tokens=4096,
    ),
    "reason": TaskRoute(
        task_type="reason",
        setting_name="nemotron_balanced_model",
        tier="balanced",
        rationale="General multi-step reasoning defaults to Super.",
        temperature=0.3,
        max_tokens=4096,
    ),
    "deep_audit": TaskRoute(
        task_type="deep_audit",
        setting_name="nemotron_frontier_model",
        tier="frontier",
        rationale="Deep audit is explicitly the hard case: Ultra's 550B MoE trades "
        "cost for the reliability an audit trail needs.",
        temperature=0.1,
        max_tokens=8192,
    ),
    "embed": TaskRoute(
        task_type="embed",
        setting_name="nemotron_embedding_model",
        tier="embedding",
        rationale="Embeddings come from the dedicated encoder, not a chat model.",
        temperature=0.0,
        max_tokens=0,
        embedding=True,
    ),
}


def normalise_task_type(task_type: str) -> str:
    """Fold casing and separators so human-entered task names still resolve."""
    return task_type.strip().lower().replace("-", "_").replace(" ", "_")


def resolve_task_route(task_type: str, *, config: Settings | None = None) -> TaskRoute:
    """Look up the routing decision for ``task_type``.

    Accepts any casing or separator style, since routing is chosen by humans in
    dashboards as often as by code: ``"deep_audit"``, ``"DEEP-AUDIT"`` and
    ``"Deep Audit"`` all resolve identically.
    """
    normalised = normalise_task_type(task_type)
    route = TASK_ROUTES.get(normalised)
    if route is None:
        raise UnknownTaskError(task_type)
    _ = config  # consumed by TaskRoute.resolve; kept for signature symmetry
    return route


def route_for(task_type: str, *, config: Settings | None = None) -> tuple[TaskRoute, str]:
    """Return ``(route, resolved_model_id)`` for ``task_type``."""
    cfg = config or settings
    route = resolve_task_route(task_type, config=cfg)
    return route, route.resolve(cfg)


def explain_routing(*, config: Settings | None = None) -> list[dict[str, Any]]:
    """Return the whole routing table with resolved models, for diagnostics."""
    cfg = config or settings
    return [
        {
            "task_type": task,
            "model": route.resolve(cfg),
            "label": route.label(cfg),
            "tier": route.tier,
            "temperature": route.resolved_temperature(cfg),
            "max_tokens": route.max_tokens,
            "embedding": route.embedding,
            "rationale": route.rationale,
        }
        for task, route in ((t, TASK_ROUTES[t]) for t in KNOWN_TASKS)
    ]


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
#: The JSON-mode wire contract sent to Token Factory.
JSON_MODE: Final[dict[str, str]] = {"type": "json_object"}

_FENCE_RE = re.compile(r"```(?:json|JSON)?[ \t]*\n?(?P<body>.*?)```", re.S)

#: Reasoning models may wrap their scratchpad in one of these tags.
_THINK_BLOCK_RE = re.compile(r"<(think|thinking|scratchpad|reasoning)>.*?</\1>", re.S | re.I)


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first balanced JSON object out of a model reply.

    Tolerates the shapes that actually come back from reasoning models in JSON
    mode: a bare object, a ```json fence, prose before the object, and a
    reasoning block ahead of both. Raises :class:`StructuredOutputError` when
    nothing parses, so the caller can retry with a correction.
    """
    if not text or not text.strip():
        raise StructuredOutputError("Model returned an empty response where JSON was required.")

    # A reasoning trace can itself contain brace-like text, so drop it first.
    candidate = _THINK_BLOCK_RE.sub(" ", text).strip()

    for source in _json_candidates(candidate):
        parsed = _try_parse(source)
        if isinstance(parsed, dict):
            return parsed

    raise StructuredOutputError(
        "Model did not return a JSON object.",
        detail={"completion_preview": text[:500]},
    )


def _json_candidates(text: str) -> list[str]:
    """Return progressively more forgiving ways of locating the object."""
    candidates = [text]

    candidates += [match.group("body") for match in _FENCE_RE.finditer(text)]

    # The outer span survives trailing prose such as "Let me know if you need
    # more detail."
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    return candidates


def _try_parse(source: str) -> Any:
    try:
        return json.loads(source)
    except (ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AIResult:
    """One completion plus the routing and usage metadata around it."""

    content: str
    model: str
    task_type: TaskType
    tier: str
    usage: MetricTokenUsage
    raw: dict[str, Any]
    latency_ms: int

    def json(self) -> Any:
        """Parse the completion as JSON, or raise :class:`StructuredOutputError`."""
        return extract_json_object(self.content)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class AIService:
    """A task-bound facade over :class:`NebiusClient`.

    Build one through :func:`get_nemotron_client` so the model is chosen by
    routing policy rather than by whoever writes the call site.
    """

    def __init__(
        self,
        *,
        task_type: str = "chat",
        config: Settings | None = None,
        client: NebiusClient | None = None,
    ) -> None:
        self._config = config or settings
        self.route = resolve_task_route(task_type, config=self._config)
        self.task_type: TaskType = self.route.task_type
        self._owns_client = client is None
        self._client = client or NebiusClient(self._config)

    # -- lifecycle ---------------------------------------------------------
    @property
    def client(self) -> NebiusClient:
        """The underlying transport client."""
        return self._client

    @property
    def model(self) -> str:
        """The model this service routes to."""
        return self.route.resolve(self._config)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the transport only when this service created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- completions -------------------------------------------------------
    async def chat(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AIResult:
        """Run a completion on the routed model and account for its tokens.

        ``response_format`` is forwarded to Token Factory when given; see
        :meth:`chat_json` for the JSON-mode contract it is used for.
        """
        started = time.perf_counter()
        payload = await self._client.create_chat_completion(
            messages=_with_system(messages, system),
            model=model or self.model,
            response_format=response_format,
            temperature=self.route.resolved_temperature(self._config)
            if temperature is None
            else temperature,
            max_tokens=self.route.max_tokens if max_tokens is None else max_tokens,
        )
        return self._to_result(payload, started=started)

    async def chat_json(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        system: str | None = None,
        schema_hint: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        max_repairs: int = 1,
    ) -> tuple[Any, AIResult]:
        """Run a completion that must yield parseable JSON.

        Requests JSON mode, then verifies the reply actually parses. If the
        model still emits prose or a fence that defeats the parser, the bad
        output is fed back as a correction and the call retried up to
        ``max_repairs`` times.

        Returns ``(parsed, result)`` so callers get the structured payload and
        the usage record for billing without a second upstream call.
        """
        prompt = _with_system(messages, _json_system_prompt(system, schema_hint))
        # JSON mode is a *request*; the verification below is what makes it a
        # contract. Reasoning models still sometimes wrap the object.
        result = await self.chat(
            messages=prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            response_format=JSON_MODE,
        )

        try:
            return extract_json_object(result.content), result
        except StructuredOutputError as exc:
            logger.warning(
                "JSON mode returned unparseable output for task %s (model=%s): %s",
                self.task_type,
                result.model,
                exc.message,
            )

        for attempt in range(1, max_repairs + 1):
            repaired = await self.chat(
                messages=[
                    *prompt,
                    {"role": "assistant", "content": result.content[:2000]},
                    {
                        "role": "user",
                        "content": (
                            "That was not valid JSON. Reply with a single JSON object "
                            "and nothing else: no prose, no explanation, no code fence."
                        ),
                    },
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
                response_format=JSON_MODE,
            )
            try:
                return extract_json_object(repaired.content), repaired
            except StructuredOutputError:
                result = repaired
                logger.warning(
                    "JSON repair %d/%d still unparseable for task %s",
                    attempt,
                    max_repairs,
                    self.task_type,
                )

        raise StructuredOutputError(
            f"Task {self.task_type!r} could not be coerced into JSON after "
            f"{max_repairs} repair attempt(s).",
            detail={"completion_preview": result.content[:500]},
        )

    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        """Embed texts using this route's embedding model."""
        if not self.route.embedding:
            raise ValueError(
                f"Task {self.task_type!r} is not an embedding route; "
                "use get_nemotron_client('embed')."
            )
        resolved = model or self.model
        vectors = await self._client.create_embeddings(input_texts=texts, model=resolved)
        logger.info("embedded %d text(s) with %s", len(texts), resolved)
        return vectors

    # -- convenience -------------------------------------------------------
    async def summarize(self, text: str, *, max_words: int = 120) -> dict[str, Any]:
        """Condense ``text`` into a structured summary."""
        parsed, _ = await self._for("summarize").chat_json(
            messages=[
                {
                    "role": "user",
                    "content": f"Summarise the following in at most {max_words} words.",
                },
                {"role": "user", "content": text},
            ],
            schema_hint=(
                '{"summary": string, "key_points": string[], "sentiment": '
                '"positive"|"neutral"|"negative"}'
            ),
        )
        return _as_dict(parsed)

    async def extract_entities(
        self, text: str, *, entity_types: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """Pull structured entities out of ``text``."""
        wanted = (
            ", ".join(entity_types)
            if entity_types
            else "people, organisations, locations, dates, monetary amounts"
        )
        parsed, _ = await self._for("extract_entities").chat_json(
            messages=[
                {
                    "role": "user",
                    "content": f"Extract {wanted} from the text. Omit any type with no matches.",
                },
                {"role": "user", "content": text},
            ],
            schema_hint='{"entities": [{"type": string, "name": string, "context": string}]}',
        )
        entities = _as_dict(parsed).get("entities", [])
        return entities if isinstance(entities, list) else []

    async def deep_audit(self, subject: str, *, criteria: Sequence[str] = ()) -> dict[str, Any]:
        """Run the frontier reasoning route and return a structured audit."""
        checks = (
            ", ".join(criteria) if criteria else "correctness, security, performance, compliance"
        )
        parsed, _ = await self._for("deep_audit").chat_json(
            messages=[
                {"role": "user", "content": f"Audit the following against: {checks}."},
                {"role": "user", "content": subject},
            ],
            schema_hint=(
                '{"findings": [{"severity": "critical"|"high"|"medium"|"low", '
                '"title": string, "detail": string, "recommendation": string}], '
                '"verdict": string}'
            ),
        )
        return _as_dict(parsed)

    # -- internals ---------------------------------------------------------
    def _for(self, task_type: TaskType) -> AIService:
        """Return a sibling service bound to ``task_type``, reusing the client.

        Lets ``AIService("chat").summarize(...)`` route to Nano instead of
        forcing every caller to construct a second object.
        """
        if task_type == self.task_type:
            return self
        return AIService(task_type=task_type, config=self._config, client=self._client)

    def _to_result(self, payload: dict[str, Any], *, started: float) -> AIResult:
        content = _first_content(payload)
        resolved = str(payload.get("model") or self.model)
        usage = MetricTokenUsage.from_payload(payload)
        metrics.record_token_usage(usage, model=resolved)
        latency_ms = int((time.perf_counter() - started) * 1000)

        # Field names deliberately avoid the substring "token": the redactor
        # masks any `*token*=` assignment, which would hide these very counts.
        logger.info(
            "ai task=%s tier=%s model=%s total=%d latency_ms=%d",
            self.task_type,
            self.route.tier,
            resolved,
            usage.total_tokens,
            latency_ms,
        )
        return AIResult(
            content=content,
            model=resolved,
            task_type=self.task_type,
            tier=self.route.tier,
            usage=usage,
            raw=payload,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_nemotron_client(
    task_type: str = "chat",
    *,
    config: Settings | None = None,
    client: NebiusClient | None = None,
) -> AIService:
    """Return an AI service bound to the model that suits ``task_type``.

    ``task_type`` accepts any :data:`KNOWN_TASKS` entry in any casing or
    separator style:

    >>> get_nemotron_client("deep_audit").model
    'nvidia/Nemotron-3-Ultra-550b-a55b'
    >>> get_nemotron_client("summarize").route.tier
    'fast'

    Raises :class:`UnknownTaskError` for anything unrecognised rather than
    silently falling back to an expensive frontier model.
    """
    return AIService(task_type=task_type, config=config, client=client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _with_system(messages: Sequence[dict[str, Any]], system: str | None) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with ``system`` hoisted to the front."""
    prompt = [dict(message) for message in messages]
    if system:
        prompt.insert(0, {"role": "system", "content": system})
    return prompt


def _json_system_prompt(system: str | None, schema_hint: str | None) -> str:
    """Compose the system prompt that makes JSON mode reliable.

    Token Factory rejects a JSON-mode request whose messages never mention
    JSON, so this instruction is mandatory rather than advisory.
    """
    parts = [
        system.strip() if system else "",
        "Respond with a single valid JSON value and nothing else.",
        "Do not wrap the JSON in markdown code fences and do not add commentary.",
    ]
    if schema_hint:
        parts.append(f"Use exactly this shape: {schema_hint}")
    return "\n\n".join(part for part in parts if part)


def _first_content(payload: dict[str, Any]) -> str:
    """Extract assistant text from an OpenAI-shaped completion body."""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Some providers return content parts instead of a bare string.
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _as_dict(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, dict):
        return parsed
    raise StructuredOutputError(
        f"Expected a JSON object for this task, received {type(parsed).__name__}."
    )


__all__ = [
    "JSON_MODE",
    "KNOWN_TASKS",
    "TASK_ROUTES",
    "AIResult",
    "AIService",
    "NebiusRateLimitError",
    "StructuredOutputError",
    "TaskRoute",
    "TaskType",
    "TokenUsage",
    "UnknownTaskError",
    "explain_routing",
    "extract_json_object",
    "get_nemotron_client",
    "normalise_task_type",
    "resolve_task_route",
    "route_for",
]
