"""Shared response envelopes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="Machine-readable error code.")
    message: str = Field(description="Human-readable explanation.")


class ErrorResponse(BaseModel):
    """Uniform error body returned by the global exception handlers."""

    error: ErrorDetail
    request_id: str | None = None


class HealthResponse(BaseModel):
    status: str = Field(description="`ok`, `degraded`, or `unavailable`.")
    version: str
    environment: str
    checks: dict[str, Any] = Field(default_factory=dict)
