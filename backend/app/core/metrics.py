"""In-process metrics for request lifecycles and token consumption.

Two families of counters live here:

* **Request lifecycle** — counts, error counts, and latency distribution per
  route, recorded by the middleware in :mod:`app.main`.
* **Token consumption** — prompt / completion / cached / total tokens, both
  globally and broken down per model, recorded by the inference endpoints.

Everything is plain ``logging`` plus these counters: no metrics client is
required, and the same numbers are both logged as text/JSON lines and exposed
through :meth:`MetricsRegistry.snapshot` for a ``/metrics`` endpoint later.

The registry is a process-global singleton, which is the correct scope for a
single-process deployment. Under ``uvicorn --workers N`` each worker keeps its
own totals; aggregating them needs a real metrics backend (OpenTelemetry, per
the project roadmap).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Upper bounds (ms) of the latency buckets reported in a snapshot.
LATENCY_BUCKETS_MS: tuple[float, ...] = (10.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 5000.0)


def _coerce_int(value: Any) -> int:
    """Best-effort cast to a non-negative int, for untrusted upstream JSON."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Normalised token accounting for one inference call.

    Token Factory (and any OpenAI-compatible endpoint) reports
    ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``, sometimes with
    a ``prompt_tokens_details.cached_tokens`` sub-object. Providers disagree on
    whether ``total_tokens`` is present, so it is derived when missing.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def __post_init__(self) -> None:
        if self.total_tokens == 0 and (self.prompt_tokens or self.completion_tokens):
            object.__setattr__(self, "total_tokens", self.prompt_tokens + self.completion_tokens)

    @property
    def billable_prompt_tokens(self) -> int:
        """Prompt tokens not served from the provider's prompt cache."""
        return max(0, self.prompt_tokens - self.cached_tokens)

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Sum two usage records (used when merging per-model totals)."""
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        """Return the usage record as a plain mapping for logs and snapshots."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> TokenUsage:
        """Parse an OpenAI-shaped ``usage`` object.

        Accepts either the ``usage`` sub-object itself or a whole response body
        containing one. Never raises: a malformed provider payload degrades to
        zeroes rather than failing the request that produced it.
        """
        if not isinstance(payload, Mapping):
            return cls()

        usage = payload.get("usage", payload)
        if not isinstance(usage, Mapping):
            return cls()

        details = usage.get("prompt_tokens_details")
        cached = 0
        if isinstance(details, Mapping):
            cached = _coerce_int(details.get("cached_tokens"))

        return cls(
            prompt_tokens=_coerce_int(usage.get("prompt_tokens")),
            completion_tokens=_coerce_int(usage.get("completion_tokens")),
            total_tokens=_coerce_int(usage.get("total_tokens")),
            cached_tokens=cached,
        )


@dataclass(slots=True)
class _RouteStat:
    """Per-route request counters and latency distribution."""

    count: int = 0
    errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    buckets: dict[str, int] = field(default_factory=dict)

    def record(self, latency_ms: float, *, failed: bool) -> None:
        """Fold one completed request into this route's statistics."""
        self.count += 1
        self.errors += int(failed)
        self.total_ms += latency_ms
        self.max_ms = max(self.max_ms, latency_ms)
        # Cumulative buckets: latency <= bound. Emitted for every bound so the
        # shape of the distribution stays stable between snapshots.
        for bound in LATENCY_BUCKETS_MS:
            if latency_ms <= bound:
                self.buckets[f"le_{bound:g}"] = self.buckets.get(f"le_{bound:g}", 0) + 1

    @property
    def avg_ms(self) -> float:
        """Mean latency across recorded requests."""
        return self.total_ms / self.count if self.count else 0.0

    def as_dict(self) -> dict[str, Any]:
        """Serialise this route's statistics."""
        return {
            "count": self.count,
            "errors": self.errors,
            "avg_ms": round(self.avg_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "latency_buckets": {
                f"le_{bound:g}": self.buckets.get(f"le_{bound:g}", 0)
                for bound in LATENCY_BUCKETS_MS
            },
        }


class MetricsRegistry:
    """Thread-safe accumulator for request and token metrics."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self.requests_total = 0
        self.requests_failed = 0
        self.requests_in_flight = 0
        self._routes: dict[str, _RouteStat] = {}
        self._tokens = TokenUsage()
        self._tokens_by_model: dict[str, TokenUsage] = {}
        self._calls_by_model: dict[str, int] = {}
        self.jobs_total = 0
        self.jobs_completed = 0
        self.jobs_failed = 0
        self.jobs_duration_ms = 0
        self._job_tokens = TokenUsage()
        self._jobs_by_type: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # Request lifecycle
    # ------------------------------------------------------------------
    def request_started(self) -> None:
        """Mark a request as in flight."""
        with self._lock:
            self.requests_in_flight += 1

    def request_finished(self, *, route: str, status_code: int, latency_ms: float) -> None:
        """Record a completed request against its route."""
        failed = status_code >= 500
        with self._lock:
            self.requests_in_flight = max(0, self.requests_in_flight - 1)
            self.requests_total += 1
            if failed:
                self.requests_failed += 1
            stat = self._routes.setdefault(route, _RouteStat())
            stat.record(latency_ms, failed=failed)

    # ------------------------------------------------------------------
    # Token consumption
    # ------------------------------------------------------------------
    def record_token_usage(self, usage: TokenUsage, *, model: str | None = None) -> TokenUsage:
        """Add one inference call's token usage to the global and per-model totals.

        Returns the running total so callers can log the cumulative figure.
        """
        if usage.total_tokens == 0:
            return self._tokens
        key = model or "unknown"
        with self._lock:
            self._tokens = self._tokens + usage
            self._tokens_by_model[key] = self._tokens_by_model.get(key, TokenUsage()) + usage
            self._calls_by_model[key] = self._calls_by_model.get(key, 0) + 1
            total = self._tokens
        logger.info(
            "tokens model=%s prompt=%d completion=%d cached=%d total=%d cumulative=%d",
            key,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cached_tokens,
            usage.total_tokens,
            total.total_tokens,
        )
        return total

    # ------------------------------------------------------------------
    # Background jobs
    # ------------------------------------------------------------------
    def record_job(
        self,
        *,
        state: str,
        job_type: str,
        duration_ms: int,
        usage: TokenUsage | None = None,
    ) -> None:
        """Record one job attempt's outcome.

        Job token usage is summed separately from request usage: a background
        job bills the provider just as a user request does, but rolling it into
        the request totals would make "requests that cost tokens" meaningless
        when a single job may embed thousands of chunks.
        """
        with self._lock:
            self.jobs_total += 1
            if state == "completed":
                self.jobs_completed += 1
            elif state in ("failed", "cancelled"):
                self.jobs_failed += 1
            self.jobs_duration_ms += max(0, duration_ms)
            stat = self._jobs_by_type.setdefault(
                job_type, {"total": 0, "completed": 0, "failed": 0}
            )
            stat["total"] += 1
            if state == "completed":
                stat["completed"] += 1
            elif state in ("failed", "cancelled"):
                stat["failed"] += 1
            if usage is not None and usage.total_tokens:
                self._job_tokens = self._job_tokens + usage

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    @property
    def tokens(self) -> TokenUsage:
        """Cumulative token usage across every recorded call."""
        with self._lock:
            return self._tokens

    @property
    def job_tokens(self) -> TokenUsage:
        """Cumulative token usage billed by background jobs."""
        with self._lock:
            return self._job_tokens

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of every counter.

        Used by tests and by the (future) ``/metrics`` endpoint.
        """
        with self._lock:
            return {
                "requests": {
                    "total": self.requests_total,
                    "failed": self.requests_failed,
                    "in_flight": self.requests_in_flight,
                },
                "routes": {route: stat.as_dict() for route, stat in sorted(self._routes.items())},
                "tokens": self._tokens.as_dict(),
                "tokens_by_model": {
                    model: {**usage.as_dict(), "calls": self._calls_by_model.get(model, 0)}
                    for model, usage in sorted(self._tokens_by_model.items())
                },
                "jobs": {
                    "total": self.jobs_total,
                    "completed": self.jobs_completed,
                    "failed": self.jobs_failed,
                    "tokens": self._job_tokens.as_dict(),
                    "duration_ms_total": self.jobs_duration_ms,
                    "duration_ms_avg": (
                        self.jobs_duration_ms // self.jobs_total if self.jobs_total else 0
                    ),
                    "by_type": {
                        name: dict(stat) for name, stat in sorted(self._jobs_by_type.items())
                    },
                },
            }

    def reset(self) -> None:
        """Clear every counter. Intended for test isolation."""
        with self._lock:
            self.requests_total = 0
            self.requests_failed = 0
            self.requests_in_flight = 0
            self._routes.clear()
            self._tokens = TokenUsage()
            self._tokens_by_model.clear()
            self._calls_by_model.clear()
            self.jobs_total = 0
            self.jobs_completed = 0
            self.jobs_failed = 0
            self.jobs_duration_ms = 0
            self._job_tokens = TokenUsage()
            self._jobs_by_type.clear()


#: Process-wide registry. Import ``metrics`` (never construct your own) so the
#: middleware and the endpoints share one set of totals.
metrics = MetricsRegistry()
