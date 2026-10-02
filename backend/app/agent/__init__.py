"""Autonomous copilot engine: planner, tools, and orchestration loop."""

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
from app.agent.orchestrator import AgentOrchestrator, EventSink
from app.agent.planner import MultiStepPlanner, Plan, PlannerError, PlanStep
from app.agent.tools import (
    AgentTool,
    ComplianceChecker,
    DataExtractor,
    DocumentSummarizer,
    EmailDraftingTool,
    default_tools,
)

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
    "AgentEvent",
    "AgentOrchestrator",
    "AgentTool",
    "ComplianceChecker",
    "DataExtractor",
    "DocumentSummarizer",
    "EmailDraftingTool",
    "EventSink",
    "MultiStepPlanner",
    "Plan",
    "PlanStep",
    "PlannerError",
    "default_tools",
]
