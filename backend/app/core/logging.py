"""Structured-ish application logging with secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

from app.core.config import settings

# Anything that looks like a bearer token, JWT, or provider key is masked
# before it can reach a log sink.
#
# Each pattern has at most one capture group — the prefix to keep — so the
# substitution below can rebuild `prefix + ***REDACTED***`.
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <token> / Basic <token> / "authorization": "<token>"
    re.compile(r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+|basic\s+)?[^\s,;'\"]+"),
    # any *key / *secret / *token / *password assignment, quoted or not:
    #   nebius_api_key=...   "api_key": "..."   secret: ...   X-Api-Token: ...
    re.compile(
        r"(?i)\b((?:[a-z0-9_-]*"
        r"(?:api[-_ ]?key|secret[-_ ]?key|access[-_ ]?token"
        r"|refresh[-_ ]?token|token|secret|password|passwd)"
        r"[a-z0-9_-]*)[\"']?\s*[:=]\s*[\"']?)[^\s,;'\"]+"
    ),
    # Bare JWT.
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    # Bare provider key.
    re.compile(r"\bnb_[A-Za-z0-9]{16,}\b"),
)


def _redact_match(match: re.Match[str]) -> str:
    """Keep any captured prefix, mask the credential that follows it."""
    prefix = match.group(1) if match.groups() else ""
    return f"{prefix}***REDACTED***"


def redact(value: str) -> str:
    """Mask credentials inside an arbitrary string."""
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(_redact_match, redacted)
    return redacted


class RedactingFilter(logging.Filter):
    """Defence-in-depth: scrub secrets from every formatted log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub credentials from every formatted record. Always returns True."""
        try:
            message = record.getMessage()
        except Exception:
            return True
        scrubbed = redact(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def configure_logging() -> None:
    """Configure root logging. Idempotent."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(level)
    stream.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    stream.addFilter(RedactingFilter())
    root.addHandler(stream)

    # Uvicorn ships its own handlers; let records propagate to ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    for noisy in ("httpx", "httpcore", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str) -> logging.Logger:
    """Return a logger guaranteed to have the redacting filter attached."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger


def log_extra(**kwargs: Any) -> dict[str, Any]:
    """Build a structured ``extra=`` payload for ``logger.info(..., extra=...)``."""
    return {"extra": {"nexusflow": kwargs}}
