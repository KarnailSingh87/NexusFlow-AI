"""Tests for the task-routed AI service layer."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from app.core.config import Settings, get_settings
from app.core.metrics import metrics
from app.services.ai_service import (
    KNOWN_TASKS,
    TASK_ROUTES,
    AIService,
    StructuredOutputError,
    UnknownTaskError,
    explain_routing,
    extract_json_object,
    get_nemotron_client,
    normalise_task_type,
    resolve_task_route,
)
from app.services.nebius import NebiusRateLimitError, NebiusUpstreamError

from tests.conftest import build_mock_client


def _completion(content: str, *, model: str = "routed", total: int = 20) -> dict[str, Any]:
    """Build an OpenAI-shaped completion body with the given assistant text."""
    return {
        "id": "chatcmpl-ai",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": total - 5,
            "completion_tokens": 5,
            "total_tokens": total,
        },
    }


def _service(handler: Any, task_type: str = "chat") -> tuple[AIService, httpx.AsyncClient]:
    nebius, http = build_mock_client(handler)
    return AIService(task_type=task_type, client=nebius), http


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------
def test_every_known_task_has_a_route() -> None:
    assert set(KNOWN_TASKS) == set(TASK_ROUTES)


@pytest.mark.parametrize(
    ("task", "expected_setting"),
    [
        # Cheap, high-volume, schema-bound work stays on Nano.
        ("summarize", "nemotron_nano_model"),
        ("extract_entities", "nemotron_nano_model"),
        ("classify", "nemotron_nano_model"),
        # Multi-step reasoning is Super.
        ("code", "nemotron_balanced_model"),
        ("reason", "nemotron_balanced_model"),
        # Explicitly hard work escalates to Ultra.
        ("deep_audit", "nemotron_frontier_model"),
        # Interactive work uses the latency-tuned Lightning.
        ("draft", "nemotron_fast_model"),
    ],
)
def test_task_routes_to_the_expected_model_setting(task: str, expected_setting: str) -> None:
    assert resolve_task_route(task).setting_name == expected_setting


def test_deep_audit_uses_a_weaker_model_than_summarize() -> None:
    """The central routing invariant: complexity must buy a stronger model."""
    assert get_nemotron_client("deep_audit").route.tier == "frontier"
    assert get_nemotron_client("summarize").route.tier == "fast"
    assert get_nemotron_client("summarize").model != get_nemotron_client("deep_audit").model


def test_routing_resolves_through_settings_so_it_can_be_overridden() -> None:
    config = Settings(
        NEMOTRON_NANO_MODEL="nvidia/custom-cheap",  # type: ignore[call-arg]
        MODEL_ALLOWLIST="nvidia/custom-cheap",  # type: ignore[call-arg]
    )
    service = get_nemotron_client("summarize", config=config)

    assert service.model == "nvidia/custom-cheap"


def test_settings_override_is_not_cached_at_import_time() -> None:
    """Routes must read Settings on every resolve, not snapshot it on import."""
    config = Settings(NEMOTRON_FRONTIER_MODEL="nvidia/custom-frontier")  # type: ignore[call-arg]
    route = resolve_task_route("deep_audit")

    assert route.resolve(config) == "nvidia/custom-frontier"
    assert route.resolve(Settings()) != "nvidia/custom-frontier"


@pytest.mark.parametrize(
    "spelling",
    ["deep_audit", "DEEP_AUDIT", "Deep Audit", "deep-audit", "  deep_audit  "],
)
def test_task_type_matching_tolerates_human_input(spelling: str) -> None:
    assert normalise_task_type(spelling) == "deep_audit"
    assert get_nemotron_client(spelling).model == get_nemotron_client("deep_audit").model


def test_unknown_task_raises_instead_of_defaulting_to_frontier() -> None:
    """Silently promoting a typo to a 550B model would be a billing incident."""
    with pytest.raises(UnknownTaskError) as excinfo:
        get_nemotron_client("reasonsing")

    assert "reasonsing" in str(excinfo.value)


def test_every_route_carries_a_rationale() -> None:
    for task, route in TASK_ROUTES.items():
        assert route.rationale.strip(), f"{task} has no routing rationale"


def test_explain_routing_reports_every_task_with_a_model() -> None:
    explained = explain_routing()

    assert [row["task_type"] for row in explained] == list(KNOWN_TASKS)
    assert all(row["model"] for row in explained)
    assert all(row["rationale"] for row in explained)


def test_empty_model_setting_is_rejected_loudly() -> None:
    config = Settings(NEMOTRON_NANO_MODEL="")  # type: ignore[call-arg]
    service = get_nemotron_client("summarize", config=config)
    with pytest.raises(ValueError, match="NEMOTRON_NANO_MODEL"):
        _ = service.model


# ---------------------------------------------------------------------------
# JSON-mode output contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "raw", "expected"),
    [
        ("bare", '{"a": 1}', {"a": 1}),
        ("fenced_json", '```json\n{"a": 1}\n```', {"a": 1}),
        ("fenced_bare", '```\n{"a": 1}\n```', {"a": 1}),
        ("prose_before", 'Sure! Here you go:\n{"a": 1}', {"a": 1}),
        ("prose_after", '{"a": 1}\nLet me know if you need more detail.', {"a": 1}),
        # The reasoning trace itself contains a decoy object.
        ("reasoning_trace", '<think>wants {"x": 1} so...</think>\n{"a": 1}', {"a": 1}),
        ("whitespace", '  \n {"a": 1} \n ', {"a": 1}),
        ("nested", '{"a": {"b": [1, 2, {"c": true}]}}', {"a": {"b": [1, 2, {"c": True}]}}),
        ("empty_object", "{}", {}),
    ],
)
def test_json_is_extracted_from_realistic_model_output(
    label: str, raw: str, expected: dict[str, Any]
) -> None:
    assert extract_json_object(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "   ", "I cannot help with that.", "[1, 2, 3]", "not json at all"]
)
def test_unparseable_output_raises_structured_output_error(raw: str) -> None:
    with pytest.raises(StructuredOutputError):
        extract_json_object(raw)


async def test_chat_json_requests_json_object_response_format() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion('{"ok": true}'))

    service, http = _service(handler, "extract_entities")
    try:
        await service.chat_json(messages=[{"role": "user", "content": "extract"}])
    finally:
        await http.aclose()

    assert captured["body"]["response_format"] == {"type": "json_object"}


async def test_chat_json_injects_a_json_instruction_and_schema_hint() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion('{"entities": []}'))

    service, http = _service(handler, "extract_entities")
    try:
        await service.chat_json(
            messages=[{"role": "user", "content": "extract"}],
            schema_hint='{"entities": []}',
        )
    finally:
        await http.aclose()

    system = captured["body"]["messages"][0]
    assert system["role"] == "system"
    # Token Factory rejects JSON mode unless the prompt mentions JSON.
    assert "JSON" in system["content"]
    assert '{"entities": []}' in system["content"]


async def test_chat_json_recovers_from_a_reasoning_trace_and_fence() -> None:
    """The realistic failure: a well-formed object wrapped in prose and a fence."""
    messy = '<think>reasoning with {"brace": 1}</think>\n```json\n{"ok": true}\n```'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(messy))

    service, http = _service(handler)
    try:
        parsed, result = await service.chat_json(messages=[{"role": "user", "content": "x"}])
    finally:
        await http.aclose()

    assert parsed == {"ok": True}
    assert result.model == "routed"


async def test_chat_json_repairs_one_unparseable_reply() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        # First reply is unusable prose; the repair reply is valid JSON.
        content = "Sure thing, here are your results!" if len(calls) == 1 else '{"ok": 1}'
        return httpx.Response(200, json=_completion(content))

    service, http = _service(handler)
    try:
        parsed, _ = await service.chat_json(messages=[{"role": "user", "content": "x"}])
    finally:
        await http.aclose()

    assert parsed == {"ok": 1}
    assert len(calls) == 2
    # The correction must show the model what it actually produced.
    assert calls[1]["messages"][-2]["role"] == "assistant"
    assert "not valid JSON" in calls[1]["messages"][-1]["content"]


async def test_chat_json_gives_up_after_exhausting_repairs() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_completion("still not json"))

    service, http = _service(handler)
    try:
        with pytest.raises(StructuredOutputError, match="could not be coerced"):
            await service.chat_json(messages=[{"role": "user", "content": "x"}])
    finally:
        await http.aclose()

    assert len(calls) == 2  # original + one repair


async def test_chat_json_can_disable_repair() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_completion("nope"))

    service, http = _service(handler)
    try:
        with pytest.raises(StructuredOutputError):
            await service.chat_json(messages=[{"role": "user", "content": "x"}], max_repairs=0)
    finally:
        await http.aclose()

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Request shaping, accounting, and transport behaviour
# ---------------------------------------------------------------------------
async def test_chat_sends_the_routed_model_and_route_defaults() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion("done", model=captured["body"]["model"]))

    service, http = _service(handler, "deep_audit")
    try:
        result = await service.chat(messages=[{"role": "user", "content": "audit this"}])
    finally:
        await http.aclose()

    assert captured["body"]["model"] == get_nemotron_client("deep_audit").model
    # Structured routes use a low temperature; this one pins its own.
    assert captured["body"]["temperature"] == TASK_ROUTES["deep_audit"].temperature
    assert captured["body"]["max_tokens"] == TASK_ROUTES["deep_audit"].max_tokens
    assert result.tier == "frontier"


async def test_chat_records_token_usage_against_the_routed_model() -> None:
    metrics.reset()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=_completion("ok", model=body["model"], total=42))

    service, http = _service(handler, "summarize")
    try:
        result = await service.chat(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()

    assert result.usage.total_tokens == 42
    assert metrics.tokens.total_tokens == 42
    per_model = metrics.snapshot()["tokens_by_model"]
    assert service.model in per_model
    assert per_model[service.model]["total_tokens"] == 42
    assert per_model[service.model]["calls"] == 1


async def test_chat_raises_when_the_model_violates_the_allowlist() -> None:
    """Routing must not become a way to bypass MODEL_ALLOWLIST."""
    config = Settings(
        MODEL_ALLOWLIST="nvidia/Nemotron-3_5-Lightning",  # type: ignore[call-arg]
        NEMOTRON_NANO_MODEL="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    )

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("allowlist violation must not reach the network")

    nebius, http = build_mock_client(handler, config=config)
    service = AIService(task_type="summarize", config=config, client=nebius)
    try:
        with pytest.raises(Exception, match="MODEL_ALLOWLIST"):
            await service.chat(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()


async def test_rate_limit_surfaces_retry_after_to_the_caller() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "slow down"})

    service, http = _service(handler)
    try:
        with pytest.raises(NebiusRateLimitError) as excinfo:
            await service.chat(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()

    assert excinfo.value.retry_after == 7.0
    # Still a NebiusError, so existing FastAPI handlers need no new wiring.
    assert excinfo.value.status_code == 429


async def test_upstream_5xx_is_retried_then_recovers() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        # Fail the first attempt only; the retry must succeed.
        if len(attempts) == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=_completion("recovered"))

    service, http = _service(handler)
    try:
        result = await service.chat(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()

    assert len(attempts) == 2
    assert result.content == "recovered"


async def test_upstream_5xx_raises_once_retries_are_exhausted() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, json={"error": "unavailable"})

    service, http = _service(handler)
    try:
        with pytest.raises(NebiusUpstreamError):
            await service.chat(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()

    # Every configured retry was actually spent.
    assert len(attempts) == get_settings().nebius_max_retries + 1


# ---------------------------------------------------------------------------
# Convenience helpers and route hopping
# ---------------------------------------------------------------------------
async def test_summarize_reroutes_to_nano_even_from_a_chat_service() -> None:
    """``AIService("chat").summarize(...)`` must not stay on the default model."""
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        models.append(body["model"])
        return httpx.Response(
            200, json=_completion('{"summary": "short", "key_points": [], "sentiment": "neutral"}')
        )

    service, http = _service(handler, "chat")
    try:
        summary = await service.summarize("a long document ...")
    finally:
        await http.aclose()

    assert summary["summary"] == "short"
    assert models == [get_nemotron_client("summarize").model]


async def test_extract_entities_returns_only_the_entities_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                '{"entities": [{"type": "person", "name": "Ada", "context": "engineer"}]}'
            ),
        )

    service, http = _service(handler)
    try:
        entities = await service.extract_entities("Ada is an engineer.")
    finally:
        await http.aclose()

    assert entities == [{"type": "person", "name": "Ada", "context": "engineer"}]


async def test_deep_audit_runs_on_the_frontier_model() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_completion('{"findings": [], "verdict": "clean"}'),
        )

    service, http = _service(handler)
    try:
        audit = await service.deep_audit("the payment service")
    finally:
        await http.aclose()

    assert audit == {"findings": [], "verdict": "clean"}
    assert captured["body"]["model"] == get_nemotron_client("deep_audit").model


async def test_convenience_helper_rejects_a_non_object_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("[1, 2, 3]"))

    service, http = _service(handler)
    try:
        with pytest.raises(StructuredOutputError):
            await service.summarize("text")
    finally:
        await http.aclose()


async def test_embed_uses_the_encoder_model_and_returns_vectors() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
            },
        )

    service, http = _service(handler, "embed")
    try:
        vectors = await service.embed(["a", "b"])
    finally:
        await http.aclose()

    assert captured["body"]["model"] == get_nemotron_client("embed").model
    # Ordered by the upstream `index`, not arrival order.
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


async def test_chat_route_cannot_embed() -> None:
    service, http = _service(lambda request: httpx.Response(200, json={}), "chat")
    try:
        with pytest.raises(ValueError, match="not an embedding route"):
            await service.embed(["a"])
    finally:
        await http.aclose()


async def test_content_parts_are_joined_when_the_provider_returns_them() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "hello "},
                                {"type": "text", "text": "world"},
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
        )

    service, http = _service(handler)
    try:
        result = await service.chat(messages=[{"role": "user", "content": "hi"}])
    finally:
        await http.aclose()

    assert result.content == "hello world"


async def test_service_does_not_close_an_injected_client() -> None:
    """Shared transports must outlive the short-lived service that borrowed them."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("ok"))

    service, http = _service(handler)
    try:
        await service.aclose()
        # Still usable, proving ownership was respected.
        assert (await service.chat(messages=[{"role": "user", "content": "x"}])).content == "ok"
    finally:
        await http.aclose()
