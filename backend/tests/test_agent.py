"""Tests for the autonomous copilot loop (planner, tools, orchestrator, SSE)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from app.agent import AgentOrchestrator, MultiStepPlanner, PlannerError
from app.services.ai_service import AIService

from tests.conftest import build_mock_client

PLAN = {
    "steps": [
        {
            "step_index": 0,
            "name": "Summarise the contract",
            "tool": "DocumentSummarizer",
            "objective": "Produce a concise summary of the vendor contract.",
            "rationale": "Ground later steps in the document's content.",
        },
        {
            "step_index": 1,
            "name": "Extract key fields",
            "tool": "DataExtractor",
            "objective": "Extract parties, dates, and amounts.",
            "rationale": "Structured facts drive the compliance audit.",
        },
        {
            "step_index": 2,
            "name": "Audit compliance",
            "tool": "ComplianceChecker",
            "objective": "Check the contract against internal compliance rules.",
            "rationale": "Flag financial discrepancies before sign-off.",
        },
        {
            "step_index": 3,
            "name": "Draft sign-off email",
            "tool": "EmailDraftingTool",
            "objective": "Draft the approval sign-off email.",
            "rationale": "Deliverable for the requester.",
        },
    ]
}


def _completion(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-agent",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "mock",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _agentic_handler(request: httpx.Request) -> httpx.Response:
    """Route mock replies by inspecting the outgoing prompt text."""
    body = json.loads(request.content)
    prompt = json.dumps(body.get("messages", []))
    if "Decompose" in prompt:
        content = json.dumps(PLAN)
    elif "Summarise the source material" in prompt:
        content = "Summary: a vendor contract with a 12-month term."
    elif "Extract structured data" in prompt:
        content = '{"parties": ["Acme", "NexusFlow"], "total": "$120,000"}'
    elif "Audit the material" in prompt:
        content = '{"findings": [], "verdict": "compliant"}'
    elif "Draft a professional approval" in prompt:
        content = "Subject: Vendor contract approved — sign-off attached."
    elif "Using only the tool outputs" in prompt:
        content = "Final: contract is compliant; sign-off email drafted."
    else:
        content = "ok"
    return httpx.Response(200, json=_completion(content))


def _orchestrator() -> tuple[AgentOrchestrator, httpx.AsyncClient]:
    nebius, http = build_mock_client(_agentic_handler)
    return AgentOrchestrator(client=nebius), http


async def test_orchestrator_streams_full_trace() -> None:
    orchestrator, http = _orchestrator()
    try:
        events = [
            event
            async for event in orchestrator.run(
                "Audit this vendor contract.", document_text="Contract text..."
            )
        ]
    finally:
        await http.aclose()

    kinds = [event.kind for event in events]
    assert kinds[0] == "plan_created"
    assert kinds[-1] == "final_answer"
    assert kinds.count("step_started") == 4
    assert kinds.count("step_completed") == 4
    assert kinds.count("tool_result") == 4
    assert "thought" in kinds
    assert "plan_updated" in kinds

    final = events[-1]
    assert final.data["steps_completed"] == 4
    assert "Final: contract is compliant" in final.data["answer"]


async def test_orchestrator_emits_run_failed_when_plan_invalid() -> None:
    def bad_plan_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion('{"steps": []}'))

    nebius, http = build_mock_client(bad_plan_handler)
    orchestrator = AgentOrchestrator(client=nebius)
    try:
        events = [event async for event in orchestrator.run("Do something.")]
    finally:
        await http.aclose()

    assert [event.kind for event in events] == ["run_failed"]
    assert events[0].data["stage"] == "planning"


async def test_planner_rejects_unregistered_tool() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                json.dumps(
                    {
                        "steps": [
                            {
                                "step_index": 0,
                                "name": "x",
                                "tool": "NotATool",
                                "objective": "y",
                            }
                        ]
                    }
                )
            ),
        )

    nebius, http = build_mock_client(handler)
    try:
        planner = MultiStepPlanner(AIService(task_type="plan", client=nebius))
        with pytest.raises(PlannerError, match="unregistered tool"):
            await planner.plan("goal", available_tools={"DocumentSummarizer": "desc"})
    finally:
        await http.aclose()


async def test_agent_endpoint_streams_sse(client: httpx.AsyncClient, override_nebius: Any) -> None:
    nebius, http = build_mock_client(_agentic_handler)
    override_nebius(nebius)
    try:
        response = await client.post(
            "/api/v1/agent/run",
            json={"goal": "Audit this vendor contract.", "document_text": "...", "max_steps": 8},
        )
    finally:
        await http.aclose()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: plan_created" in response.text
    assert "event: final_answer" in response.text
