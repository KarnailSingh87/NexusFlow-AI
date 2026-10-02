"""Agentic copilot endpoint: stream plan, thoughts, and answers as SSE."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse

from app.agent import AgentOrchestrator
from app.agent.events import EVENT_RUN_FAILED, AgentEvent
from app.api.deps import NebiusClientDep
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.agent import AgentRunRequest
from app.services.nebius import NebiusError

router = APIRouter(prefix="/agent", tags=["agent"])
logger = get_logger(__name__)

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post(
    "/run",
    summary="Run the autonomous copilot loop",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Server-sent events: plan_created, step_started, thought, "
            "tool_result, step_completed, plan_updated, final_answer.",
        },
        503: {"description": "Nebius client is not configured."},
    },
)
async def run_agent(payload: AgentRunRequest, nebius: NebiusClientDep) -> StreamingResponse:
    """Plan and execute a multi-step agent run, streaming every event.

    Events are framed as ``event: <kind>`` + ``data: {json}`` so the browser
    can render the plan, intermediate thoughts, and tool results live, then
    the synthesised final answer.
    """

    async def event_source() -> AsyncIterator[str]:
        orchestrator = AgentOrchestrator(
            client=nebius, config=settings, max_steps=payload.max_steps
        )
        try:
            async for event in orchestrator.run(payload.goal, document_text=payload.document_text):
                yield event.to_sse()
        except NebiusError as exc:
            # The HTTP status is already committed once streaming starts, so a
            # mid-stream provider failure is signalled as a terminal event.
            logger.warning("agent run aborted: %s", exc.message)
            yield AgentEvent(EVENT_RUN_FAILED, {"error": exc.message}).to_sse()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        status_code=status.HTTP_200_OK,
    )


__all__ = ["router"]
