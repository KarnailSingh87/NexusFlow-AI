"""Schemas for the autonomous copilot agent endpoint."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AgentRunRequest(BaseModel):
    """Payload for ``POST /api/v1/agent/run``."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    document_text: str = Field(default="", max_length=200_000)
    max_steps: int = Field(default=8, ge=1, le=16)


__all__ = ["AgentRunRequest"]
