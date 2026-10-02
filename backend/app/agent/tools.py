"""Execution tools the agent can call dynamically.

Each tool is a small, single-purpose unit bound to a task-routed
:class:`AIService`. The orchestrator treats tools as opaque callables with a
``name``, a ``description`` (shown to the planner), and an async ``run`` that
returns a JSON-safe result. Adding a new tool means adding one class here and
one registry entry — the planner and orchestrator need no changes.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from app.services.ai_service import AIService


class AgentTool(Protocol):
    """Structural contract every tool satisfies."""

    name: str
    description: str

    async def run(
        self,
        *,
        goal: str,
        objective: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute the step. ``context`` holds prior step outputs, keyed by tool."""
        ...


def _render_context(context: dict[str, Any], *, max_chars: int = 6000) -> str:
    """Serialise prior step outputs for inclusion in a prompt."""
    if not context:
        return "(no earlier step outputs)"
    try:
        rendered = json.dumps(context, default=str, indent=2)
    except (TypeError, ValueError):
        rendered = str(context)
    return rendered[:max_chars]


class _AITool:
    """Base for tools that are a single routed completion."""

    name: str = ""
    description: str = ""
    #: Task route from the AI service routing table.
    task_type: str = "chat"
    #: Instruction prepended to the user objective.
    instruction: str = ""

    def __init__(self, service_factory: Any) -> None:
        self._service_factory = service_factory

    def _service(self) -> AIService:
        return self._service_factory(self.task_type)

    async def run(
        self,
        *,
        goal: str,
        objective: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the tool's completion and wrap the output with metadata."""
        service = self._service()
        messages = [
            {
                "role": "user",
                "content": (
                    f"{self.instruction}\n\n"
                    f"Overall goal: {goal}\n"
                    f"This step's objective: {objective}\n\n"
                    f"Prior step outputs:\n{_render_context(context)}"
                ),
            }
        ]
        result = await service.chat(messages=messages)
        return {
            "tool": self.name,
            "model": result.model,
            "tier": result.tier,
            "output": result.content,
            "usage": result.usage.as_dict(),
        }


class DocumentSummarizer(_AITool):
    """Condense a document into a structured summary: routed to Nano."""

    name = "DocumentSummarizer"
    description = "Condense the source document into a concise structured summary."
    task_type = "summarize"
    instruction = (
        "Summarise the source material in the context below. Produce a short summary, "
        "a list of key points, and flag anything ambiguous or missing."
    )


class ComplianceChecker(_AITool):
    """Audit content against compliance rules: routed to Ultra (deep_audit)."""

    name = "ComplianceChecker"
    description = "Audit the document against internal compliance rules and flag violations."
    task_type = "deep_audit"
    instruction = (
        "Audit the material in the context against standard internal compliance rules: "
        "contract terms, financial consistency, data handling, jurisdiction, and "
        "liability exposure. List findings with severity, evidence, and a recommendation. "
        "Flag any financial discrepancies explicitly."
    )


class DataExtractor(_AITool):
    """Pull structured entities out of a document: routed to Nano."""

    name = "DataExtractor"
    description = "Extract structured fields (parties, dates, amounts, clauses) from the document."
    task_type = "extract_entities"
    instruction = (
        "Extract structured data from the material in the context: named parties, "
        "key dates, monetary amounts, governing clauses, and obligations. Return a "
        "compact structured list; omit fields that are absent rather than inventing them."
    )


class EmailDraftingTool(_AITool):
    """Draft an approval sign-off email: routed to Lightning (draft)."""

    name = "EmailDraftingTool"
    description = "Draft a concise approval sign-off email summarising the run's findings."
    task_type = "draft"
    instruction = (
        "Draft a professional approval sign-off email. It must reference the key findings "
        "and any flagged discrepancies from the prior steps, state a clear recommendation "
        "(approve / approve with conditions / reject), and be ready to send."
    )


#: Name → tool instance for the default toolkit.
def default_tools(service_factory: Any) -> dict[str, AgentTool]:
    """Build the default toolkit bound to ``service_factory``.

    ``service_factory`` is a callable ``(task_type) -> AIService``; the
    orchestrator supplies one backed by its shared Nebius client.
    """
    tools: list[AgentTool] = [
        DocumentSummarizer(service_factory),
        ComplianceChecker(service_factory),
        DataExtractor(service_factory),
        EmailDraftingTool(service_factory),
    ]
    return {tool.name: tool for tool in tools}


__all__ = [
    "AgentTool",
    "ComplianceChecker",
    "DataExtractor",
    "DocumentSummarizer",
    "EmailDraftingTool",
    "default_tools",
]
