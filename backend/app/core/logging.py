"""Structured-ish application logging with secret redaction.

Two formatters are available, selected by ``LOG_FORMAT``:

* ``text`` (default) — human-readable single lines for a terminal.
* ``json`` — one JSON object per line, for log aggregators.

Both run every record through :class:`RedactingFilter`, so credentials are
masked no matter where the output is shipped.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings

#: Correlation ID for the request currently being served. Set by the middleware
#: in :mod:`app.main` so that every log line emitted while handling a request —
#: including lines from deep inside the inference client — carries the same
#: ``request_id``. A :class:`contextvars.ContextVar` is used rather than a
#: thread-local because the work happens in a single event loop with interleaved
#: coroutines, and only context propagation gives per-task isolation.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nexusflow_request_id", default=None
)

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
    if settings.log_format == "json":
        stream.setFormatter(JsonFormatter())
    else:
        stream.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | "
                "[req=%(request_id)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
    stream.addFilter(RedactingFilter())
    stream.addFilter(RequestIdFilter())
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


def bind_request_id(request_id: str | None = None) -> str:
    """Bind ``request_id`` to the current context and return the effective value.

    Call this from the request middleware. Every subsequent log record is
    annotated with the id, which is what makes a single request traceable across
    the middleware, the endpoint, and the provider client.
    """
    resolved = request_id or uuid.uuid4().hex
    request_id_var.set(resolved)
    return resolved


def current_request_id() -> str | None:
    """Return the request id bound to this context, if any."""
    return request_id_var.get()


@contextmanager
def request_scope(request_id: str | None = None) -> Iterator[str]:
    """Bind ``request_id`` for the duration of the block.

    Resets the context variable on exit, so a completed request cannot leak its
    id into later lifecycle logging (the app-shutdown lines, for example).
    """
    resolved = request_id or uuid.uuid4().hex
    token = request_id_var.set(resolved)
    try:
        yield resolved
    finally:
        request_id_var.reset(token)


class RequestIdFilter(logging.Filter):
    """Attach the context's request id to every record.

    The text formatter interpolates ``%(request_id)s``, so the attribute must
    exist even for records emitted outside a request (start-up, background jobs);
    those get ``"-"``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Set ``record.request_id`` and always keep the record."""
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object.

    Extra fields can be attached per-record with ``extra={"nexusflow": {...}}``
    (see :func:`log_extra`); they are merged into the top-level object so
    aggregators can index them without parsing the message.
    """

    #: Keys ``logging`` always sets on a record; anything else came from ``extra``.
    _RESERVED = frozenset(
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "message",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "taskName",
            "thread",
            "threadName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record`` as JSON, never raising on unserialisable extras."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": current_request_id(),
        }

        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value

        structured = getattr(record, "nexusflow", None)
        if isinstance(structured, dict):
            payload.update(structured)

        if record.exc_info:
            # `formatException` already ends with a newline; JSONL needs one
            # record per line.
            payload["exception"] = self.formatException(record.exc_info).rstrip("\n")

        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # Never let a log sink take down a request.
            fallback = {
                "timestamp": payload["timestamp"],
                "level": record.levelname,
                "logger": record.name,
                "message": redact(str(record.msg)),
                "request_id": payload["request_id"],
                "serialization_error": True,
            }
            return json.dumps(fallback, ensure_ascii=False)
