"""Tests for request-lifecycle and token-consumption metrics."""

from __future__ import annotations

import pytest
from app.core.metrics import LATENCY_BUCKETS_MS, MetricsRegistry, TokenUsage


@pytest.fixture
def registry() -> MetricsRegistry:
    """A private registry, so tests never share or clobber the global one."""
    return MetricsRegistry()


# ---------------------------------------------------------------------------
# TokenUsage parsing
# ---------------------------------------------------------------------------
def test_from_payload_parses_openai_usage() -> None:
    usage = TokenUsage.from_payload(
        {"usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}}
    )

    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 45
    assert usage.total_tokens == 165


def test_from_payload_accepts_a_bare_usage_object() -> None:
    usage = TokenUsage.from_payload({"prompt_tokens": 10, "completion_tokens": 5})

    assert usage.total_tokens == 15


def test_from_payload_reads_cached_prompt_tokens() -> None:
    usage = TokenUsage.from_payload(
        {
            "prompt_tokens": 500,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 400},
        }
    )

    assert usage.cached_tokens == 400
    assert usage.billable_prompt_tokens == 100


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not-a-dict",
        [],
        {},
        {"usage": None},
        {"usage": "nope"},
        {"usage": {"prompt_tokens": None, "completion_tokens": "abc"}},
    ],
)
def test_from_payload_degrades_to_zeroes(payload: object) -> None:
    """A malformed provider payload must never raise into the request path."""
    usage = TokenUsage.from_payload(payload)

    assert usage.total_tokens == 0


def test_from_payload_never_yields_negative_counts() -> None:
    usage = TokenUsage.from_payload({"usage": {"prompt_tokens": -50}})

    assert usage.prompt_tokens == 0


def test_usage_sums_across_calls() -> None:
    total = TokenUsage(prompt_tokens=1, completion_tokens=2) + TokenUsage(
        prompt_tokens=10, completion_tokens=20
    )

    assert total.prompt_tokens == 11
    assert total.completion_tokens == 22
    assert total.total_tokens == 33


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------
def test_record_token_usage_accumulates_globally_and_per_model(registry: MetricsRegistry) -> None:
    registry.record_token_usage(TokenUsage(prompt_tokens=10, completion_tokens=5), model="fast")
    registry.record_token_usage(TokenUsage(prompt_tokens=1, completion_tokens=1), model="frontier")

    assert registry.tokens.total_tokens == 17
    snapshot = registry.snapshot()
    assert snapshot["tokens_by_model"]["fast"]["total_tokens"] == 15
    assert snapshot["tokens_by_model"]["fast"]["calls"] == 1
    assert snapshot["tokens_by_model"]["frontier"]["calls"] == 1


def test_record_token_usage_ignores_empty_calls(registry: MetricsRegistry) -> None:
    """A response without usage (e.g. a truncated stream) must not skew totals."""
    registry.record_token_usage(TokenUsage(), model="fast")

    assert registry.tokens.total_tokens == 0
    assert registry.snapshot()["tokens_by_model"] == {}


def test_unknown_model_is_bucketed(registry: MetricsRegistry) -> None:
    registry.record_token_usage(TokenUsage(prompt_tokens=2, completion_tokens=2))

    assert "unknown" in registry.snapshot()["tokens_by_model"]


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------
def test_request_finished_counts_and_buckets_latency(registry: MetricsRegistry) -> None:
    registry.request_started()
    registry.request_started()
    registry.request_finished(route="/api/v1/models", status_code=200, latency_ms=12.0)
    registry.request_finished(route="/api/v1/models", status_code=500, latency_ms=900.0)

    snapshot = registry.snapshot()
    assert snapshot["requests"] == {"total": 2, "failed": 1, "in_flight": 0}
    route = snapshot["routes"]["/api/v1/models"]
    assert route["count"] == 2
    assert route["errors"] == 1
    assert route["max_ms"] == 900.0
    assert route["avg_ms"] == 456.0


def test_latency_buckets_are_cumulative(registry: MetricsRegistry) -> None:
    registry.request_finished(route="/x", status_code=200, latency_ms=40.0)

    buckets = registry.snapshot()["routes"]["/x"]["latency_buckets"]
    # 40ms lands in every bucket at or above 40.
    assert buckets["le_10"] == 0
    assert buckets["le_50"] == 1
    assert buckets["le_5000"] == 1
    assert set(buckets) == {f"le_{bound:g}" for bound in LATENCY_BUCKETS_MS}


def test_in_flight_never_goes_negative(registry: MetricsRegistry) -> None:
    registry.request_finished(route="/x", status_code=200, latency_ms=1.0)

    assert registry.snapshot()["requests"]["in_flight"] == 0


def test_4xx_is_not_counted_as_a_failure(registry: MetricsRegistry) -> None:
    """Only 5xx indicates a server fault; 4xx is the client's problem."""
    registry.request_finished(route="/x", status_code=404, latency_ms=1.0)

    assert registry.snapshot()["requests"]["failed"] == 0


def test_reset_clears_every_counter(registry: MetricsRegistry) -> None:
    registry.request_started()
    registry.request_finished(route="/x", status_code=200, latency_ms=1.0)
    registry.record_token_usage(TokenUsage(prompt_tokens=1, completion_tokens=1), model="m")

    registry.reset()

    snapshot = registry.snapshot()
    assert snapshot["requests"]["total"] == 0
    assert snapshot["routes"] == {}
    assert snapshot["tokens"]["total_tokens"] == 0
    assert snapshot["tokens_by_model"] == {}


def test_snapshot_is_json_serialisable(registry: MetricsRegistry) -> None:
    import json

    registry.request_finished(route="/x", status_code=200, latency_ms=1.5)
    registry.record_token_usage(TokenUsage(prompt_tokens=3, completion_tokens=4), model="m")

    assert json.loads(json.dumps(registry.snapshot()))
