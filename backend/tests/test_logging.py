"""Tests for log redaction and formatting — security-relevant, so keep these exhaustive."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import pytest
from app.core.logging import (
    JsonFormatter,
    RedactingFilter,
    bind_request_id,
    configure_logging,
    current_request_id,
    get_logger,
    log_extra,
    redact,
)


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("Authorization: Bearer nb_abcdef1234567890abcdef", "nb_abcdef1234567890abcdef"),
        ("authorization=Bearer sk-proj-1234567890abcdef", "sk-proj-1234567890abcdef"),
        ("NEBIUS_API_KEY=nb_abc123def456ghi789", "nb_abc123def456ghi789"),
        ("nebius_api_key: nb_abc123def456ghi789", "nb_abc123def456ghi789"),
        ('{"api_key": "sk-xyz-1234567890"}', "sk-xyz-1234567890"),
        ('{"password": "hunter2"}', "hunter2"),
        ("SECRET_KEY=super-secret-value", "super-secret-value"),
        ("X-Api-Token: abcdef1234567890", "abcdef1234567890"),
        (
            "raw jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.aBcDeFgHiJkLmNoPqRsT",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
    ],
)
def test_redact_masks_credentials(raw: str, must_not_contain: str) -> None:
    scrubbed = redact(raw)

    assert must_not_contain not in scrubbed, f"{must_not_contain!r} leaked"
    assert "***REDACTED***" in scrubbed


@pytest.mark.parametrize(
    "benign",
    [
        "GET /api/v1/models -> 200 in 12.3ms",
        "chat.completions model=nvidia/Nemotron-3_5-Lightning messages=3",
        "SECRET_KEY is still a placeholder",
        "NEBIUS_API_KEY is required",
        "data: [DONE]",
        "Database readiness check failed: connection refused",
    ],
)
def test_redact_leaves_operational_logs_intact(benign: str) -> None:
    assert redact(benign) == benign


def test_redacting_filter_scrubs_formatted_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = get_logger("test.redaction")
    with caplog.at_level(logging.INFO):
        logger.info("calling provider with nebius_api_key=nb_leakedkey1234567890")

    scrubbed = "\n".join(record.getMessage() for record in caplog.records)
    assert "nb_leakedkey1234567890" not in scrubbed


def test_redacting_filter_never_raises_on_bad_records() -> None:
    filt = RedactingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="value is %s",
        args=(),
        exc_info=None,
    )

    assert filt.filter(record) is True


def test_redacting_filter_interpolates_arguments() -> None:
    filt = RedactingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="key=%s",
        args=("nb_shouldbehidden12345",),
        exc_info=None,
    )

    assert filt.filter(record) is True
    assert "nb_shouldbehidden12345" not in record.getMessage()


# ---------------------------------------------------------------------------
# Request correlation
# ---------------------------------------------------------------------------
def test_bind_request_id_exposes_the_id_to_later_records() -> None:
    assert current_request_id() is None

    bound = bind_request_id("abc123")

    assert bound == "abc123"
    assert current_request_id() == "abc123"


def test_bind_request_id_generates_one_when_absent() -> None:
    generated = bind_request_id()

    assert isinstance(generated, str)
    assert generated
    assert current_request_id() == generated


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------
def _record(
    msg: str = "hello",
    *,
    level: int = logging.INFO,
    exc_info: object = None,
    **extra: object,
) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.json",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,  # type: ignore[arg-type]
        **extra,  # type: ignore[arg-type]
    )


def test_json_formatter_emits_one_object_per_record() -> None:
    payload = json.loads(JsonFormatter().format(_record("hello")))

    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.json"
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_carries_the_bound_request_id() -> None:
    bind_request_id("req-42")

    payload = json.loads(JsonFormatter().format(_record("hello")))

    assert payload["request_id"] == "req-42"


def _capture(emit: Callable[[logging.Logger], None]) -> logging.LogRecord:
    """Run ``emit`` against a real logger and return the record it produced.

    ``extra=`` is only flattened onto the record by ``Logger._log``, so a
    hand-built ``LogRecord`` cannot exercise the structured-extra path.
    """
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("test.structured")
    logger.handlers.clear()
    logger.propagate = False
    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        emit(logger)
    finally:
        logger.removeHandler(handler)
    return records[0]


def test_json_formatter_merges_structured_extras() -> None:
    record = _capture(lambda log: log.info("tokens", **log_extra(prompt_tokens=10, model="fast")))

    payload = json.loads(JsonFormatter().format(record))

    assert payload["prompt_tokens"] == 10
    assert payload["model"] == "fast"


def test_json_formatter_includes_the_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        payload = json.loads(JsonFormatter().format(_record("failed", exc_info=sys.exc_info())))

    assert "ValueError: boom" in payload["exception"]


def test_json_formatter_survives_unserialisable_extras() -> None:
    record = _capture(lambda log: log.info("weird", **log_extra(bad=object())))

    line = JsonFormatter().format(record)

    # Either it serialises via `default=str`, or it degrades to a safe stub —
    # but it must never raise.
    assert "weird" in line
    json.loads(line)


def test_json_formatter_output_stays_on_one_line() -> None:
    assert "\n" not in JsonFormatter().format(_record("line one\nline two"))


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    configure_logging()

    assert len(logging.getLogger().handlers) == 1


@pytest.mark.parametrize(
    "line",
    [
        "ai task=summarize tier=fast model=nvidia/Nano total=128 latency_ms=41",
        "prompt=37 completion=5 cached=0 total=42",
    ],
)
def test_token_counts_survive_redaction(line: str) -> None:
    """Counting must not look like credentialing.

    The redactor masks any ``*token*=`` assignment, so a log field literally named
    ``tokens=42`` is scrubbed and the count silently disappears — which defeats
    the point of logging token consumption in the first place. Field names for
    counts therefore avoid the substring entirely.
    """
    assert redact(line) == line


def test_token_counts_would_be_redacted_if_named_tokens() -> None:
    """Documents the collision that the naming convention above exists to avoid."""
    assert "42" not in redact("tokens=42")
