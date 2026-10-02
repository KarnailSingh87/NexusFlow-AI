"""Streaming event records emitted by the agent loop.

Every state change the orchestrator undergoes — a new plan, a step starting,
an intermediate model thought, a tool result, the final answer — is turned
into an :class:`AgentEvent`. The API layer serialises these onto an SSE wire;
tests can consume the same async iterator directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Final

#: Event kinds the frontend renders. Kept as an explicit vocabulary so a typo
#: in the orchestrator fails loudly in the SSE ``event:`` line rather than as
#: a UI that silently never updates.
EVENT_PLAN_CREATED: Final = "plan_created"
EVENT_STEP_STARTED: Final = "step_started"
EVENT_THOUGHT: Final = "thought"
EVENT_TOOL_RESULT: Final = "tool_result"
EVENT_STEP_COMPLETED: Final = "step_completed"
EVENT_STEP_FAILED: Final = "step_failed"
EVENT_PLAN_UPDATED: Final = "plan_updated"
EVENT_FINAL_ANSWER: Final = "final_answer"
EVENT_RUN_FAILED: Final = "run_failed"

KNOWN_EVENTS: Final[frozenset[str]] = frozenset(
    {
        EVENT_PLAN_CREATED,
        EVENT_STEP_STARTED,
        EVENT_THOUGHT,
        EVENT_TOOL_RESULT,
        EVENT_STEP_COMPLETED,
        EVENT_STEP_FAILED,
        EVENT_PLAN_UPDATED,
        EVENT_FINAL_ANSWER,
        EVENT_RUN_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One unit of agent progress, ready for SSE serialisation."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        """Render as a single ``text/event-stream`` frame."""
        payload = {"type": self.kind, "timestamp": self.timestamp, **self.data}
        return f"event: {self.kind}\ndata: {json.dumps(payload, default=str)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view used by tests and the final ledger."""
        return {"type": self.kind, "timestamp": self.timestamp, **self.data}


__all__ = [
    "EVENT_FINAL_ANSWER",
    "EVENT_PLAN_CREATED",
    "EVENT_PLAN_UPDATED",
    "EVENT_RUN_FAILED",
    "EVENT_STEP_COMPLETED",
    "EVENT_STEP_FAILED",
    "EVENT_STEP_STARTED",
    "EVENT_THOUGHT",
    "EVENT_TOOL_RESULT",
    "KNOWN_EVENTS",
    "AgentEvent",
]
