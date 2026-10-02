"""Core autonomous copilot loop.

:class:`AgentOrchestrator` wires the planner and the toolkit together: it asks
the planner to decompose the goal, then executes each step against a tool,
streaming :class:`AgentEvent` records as it goes. Tool outputs accumulate in a
shared context so later steps (e.g. ComplianceChecker) can see earlier ones
(e.g. DocumentSummarizer). A final synthesis pass produces the answer the user
sees.

The loop is deliberately transport-agnostic: it exposes
:meth:`AgentOrchestrator.run` as an async iterator of events. The API layer
serialises those onto SSE; tests iterate them directly.

Every model call inside the loop goes through the shared NebiusClient, which
talks to the same Nebius Token Factory endpoints as direct completions — so the
agent inherits the pinned ``/v1`` base URL, ``Bearer`` auth, backoff/retry
policy, and typed error hierarchy with no second client to drift out of sync.
Long-running document steps are the same contract as Nebius Serverless Jobs:
the HTTP request only submits and streams; the queue worker drains the rest.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.agent.events import (
    EVENT_FINAL_ANSWER,
    EVENT_PLAN_CREATED,
    EVENT_PLAN_UPDATED,
    EVENT_RUN_FAILED,
    EVENT_STEP_COMPLETED,
    EVENT_STEP_FAILED,
    EVENT_STEP_STARTED,
    EVENT_THOUGHT,
    EVENT_TOOL_RESULT,
    AgentEvent,
)
from app.agent.planner import MultiStepPlanner, PlannerError
from app.agent.tools import AgentTool, default_tools
from app.core.config import Settings, settings
from app.core.logging import get_logger
from app.services.ai_service import AIService, StructuredOutputError
from app.services.nebius import NebiusClient, NebiusError

logger = get_logger(__name__)

#: Injectable sink for events (SSE encoder, test collector, job progress).
EventSink = Callable[[AgentEvent], Awaitable[None] | None]


class AgentOrchestrator:
    """Plans, executes, and streams an autonomous copilot run."""

    def __init__(
        self,
        *,
        client: NebiusClient | None = None,
        config: Settings | None = None,
        tools: dict[str, AgentTool] | None = None,
        max_steps: int = 8,
    ) -> None:
        self._config = config or settings
        self._client = client or NebiusClient(self._config)
        self._owns_client = client is None
        self._max_steps = max_steps
        self._service_factory = self._make_service
        self._tools = tools if tools is not None else default_tools(self._service_factory)

    # -- wiring ------------------------------------------------------------
    def _make_service(self, task_type: str) -> AIService:
        return AIService(task_type=task_type, config=self._config, client=self._client)

    @property
    def tools(self) -> dict[str, AgentTool]:
        """The tool registry this run will use."""
        return self._tools

    async def aclose(self) -> None:
        """Release the Nebius client only when this orchestrator created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- the loop -----------------------------------------------------------
    async def run(
        self,
        goal: str,
        *,
        document_text: str = "",
        sink: EventSink | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute the copilot loop, yielding an event per state change.

        The same events are also pushed to ``sink`` when provided, so a caller
        can both iterate live and record the full trace.
        """
        run_id = uuid.uuid4().hex[:12]
        context: dict[str, Any] = {}
        started = time.perf_counter()

        async def emit(kind: str, **data: Any) -> AgentEvent:
            event = AgentEvent(kind, {"run_id": run_id, **data})
            if sink is not None:
                maybe = sink(event)
                if isinstance(maybe, Awaitable):
                    await maybe
            return event

        planner = MultiStepPlanner(self._make_service("plan"), max_steps=self._max_steps)
        try:
            plan = await planner.plan(
                goal,
                available_tools={name: t.description for name, t in self._tools.items()},
                context=document_text,
            )
        except PlannerError as exc:
            yield await emit(EVENT_RUN_FAILED, stage="planning", error=str(exc))
            return

        yield await emit(
            EVENT_PLAN_CREATED,
            goal=goal,
            steps=plan.to_public_list(),
            model=self._make_service("plan").model,
        )

        completed = 0
        for step in plan.steps:
            yield await emit(
                EVENT_STEP_STARTED,
                step_index=step.step_index,
                name=step.name,
                tool=step.tool,
                objective=step.objective,
            )
            yield await emit(
                EVENT_THOUGHT,
                step_index=step.step_index,
                message=f"Running {step.tool}: {step.objective}",
            )

            tool = self._tools.get(step.tool)
            if tool is None:
                yield await emit(
                    EVENT_STEP_FAILED,
                    step_index=step.step_index,
                    name=step.name,
                    error=f"Tool {step.tool!r} is not registered.",
                )
                yield await emit(
                    EVENT_RUN_FAILED, stage="execution", error=f"Unknown tool {step.tool!r}."
                )
                return

            try:
                output = await tool.run(goal=goal, objective=step.objective, context=context)
            except (NebiusError, StructuredOutputError, ValueError) as exc:
                yield await emit(
                    EVENT_STEP_FAILED,
                    step_index=step.step_index,
                    name=step.name,
                    tool=step.tool,
                    error=str(exc),
                )
                yield await emit(EVENT_RUN_FAILED, stage="execution", error=str(exc))
                return

            context[step.tool] = output
            completed += 1
            yield await emit(
                EVENT_TOOL_RESULT,
                step_index=step.step_index,
                tool=step.tool,
                model=output.get("model"),
                tier=output.get("tier"),
                preview=str(output.get("output", ""))[:500],
                usage=output.get("usage"),
            )
            yield await emit(
                EVENT_STEP_COMPLETED,
                step_index=step.step_index,
                name=step.name,
                tool=step.tool,
                status="succeeded",
            )
            yield await emit(
                EVENT_PLAN_UPDATED,
                completed=completed,
                total=len(plan.steps),
                status="running",
            )

        final_answer = await self._synthesise(goal, context)
        latency_ms = int((time.perf_counter() - started) * 1000)
        yield await emit(
            EVENT_FINAL_ANSWER,
            answer=final_answer,
            steps_completed=completed,
            latency_ms=latency_ms,
            tools_used=list(context),
        )

    async def _synthesise(self, goal: str, context: dict[str, Any]) -> str:
        """Combine all tool outputs into the user-facing final answer."""
        service = self._make_service("reason")
        try:
            result = await service.chat(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Using only the tool outputs below, produce the final answer to the "
                            "user's goal. Be concise, cite the step each fact came from, and state "
                            "any discrepancies or blockers explicitly.\n\n"
                            f"Goal: {goal}\n\nTool outputs:\n" + _render(context)[:12000]
                        ),
                    }
                ]
            )
            return result.content
        except NebiusError as exc:
            logger.warning("final synthesis failed: %s", exc.message)
            return (
                "The individual steps completed, but final synthesis failed: "
                f"{exc.message}. Raw step outputs are available in the trace."
            )


def _render(context: dict[str, Any]) -> str:
    try:
        return json.dumps(context, default=str, indent=2)
    except (TypeError, ValueError):
        return str(context)


__all__ = ["AgentOrchestrator", "EventSink"]
