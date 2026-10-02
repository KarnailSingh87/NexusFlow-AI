"""Tests for log redaction — security-relevant, so keep these exhaustive."""

from __future__ import annotations

import logging

import pytest
from app.core.logging import RedactingFilter, get_logger, redact


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
