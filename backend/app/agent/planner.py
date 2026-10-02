"""Multi-step planner: decomposes a high-level goal into tool calls.

The planner runs on Nemotron 3 Ultra (the ``plan`` task route). It is given
the goal, the available tool names/descriptions, and a schema hint for its
output; the reply is verified to be a JSON array of steps that only names
registered tools. Anything else is a planner failure, not a silent
mis-execution.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.services.ai_service import AIService, StructuredOutputError


class PlannerError(Exception):
    """Raised when the planner cannot produce a usable plan."""


class PlanStep(BaseModel):
    """One ordered step of an agent plan."""

    step_index: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=200)
    tool: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1)
    rationale: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        """Shape streamed to the client; drops internal bookkeeping."""
        return {
            "step_index": self.step_index,
            "name": self.name,
            "tool": self.tool,
            "objective": self.objective,
            "rationale": self.rationale,
        }


class Plan(BaseModel):
    """The full ordered plan for a goal."""

    goal: str
    steps: list[PlanStep]

    def to_public_list(self) -> list[dict[str, Any]]:
        """Serialise every step for the ``plan_created`` event."""
        return [step.to_public_dict() for step in self.steps]


_PLAN_SCHEMA_HINT = (
    '{"steps": [{"step_index": int, "name": string, "tool": string, '
    '"objective": string, "rationale": string}]}'
)


class MultiStepPlanner:
    """Breaks a goal into validated, sequential tool steps."""

    def __init__(self, service: AIService, *, max_steps: int = 8) -> None:
        self._service = service
        self._max_steps = max_steps

    async def plan(
        self,
        goal: str,
        *,
        available_tools: dict[str, str],
        context: str = "",
    ) -> Plan:
        """Return a validated plan for ``goal``.

        ``available_tools`` maps tool name → one-line description; the planner
        is told it must choose from this set only. Raises
        :class:`PlannerError` when the model's reply cannot be coerced into a
        valid, tool-referencing plan.
        """
        if not goal.strip():
            raise PlannerError("Goal must be a non-empty string.")
        if not available_tools:
            raise PlannerError("At least one tool must be registered.")

        tool_lines = "\n".join(
            f"- {name}: {desc}" for name, desc in sorted(available_tools.items())
        )
        context_block = (
            f"\n\nSource material available to the agent:\n{context[:8000]}" if context else ""
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Decompose the following goal into an ordered list of at most "
                    f"{self._max_steps} steps. Each step must name exactly one tool from "
                    f"the available list. Steps run sequentially; each step may rely on "
                    f"the outputs of earlier steps. Do not invent tools.\n\n"
                    f"Available tools:\n{tool_lines}\n\nGoal: {goal}{context_block}"
                ),
            }
        ]
        try:
            parsed, _result = await self._service.chat_json(
                messages=messages,
                schema_hint=_PLAN_SCHEMA_HINT,
            )
        except StructuredOutputError as exc:
            raise PlannerError(f"Planner did not return valid JSON: {exc.message}") from exc

        return self._validate(parsed, goal=goal, available_tools=available_tools)

    def _validate(
        self,
        parsed: Any,
        *,
        goal: str,
        available_tools: dict[str, str],
    ) -> Plan:
        steps_raw: Any
        if isinstance(parsed, dict):
            steps_raw = parsed.get("steps")
        elif isinstance(parsed, list):
            steps_raw = parsed
        else:
            raise PlannerError(f"Planner returned {type(parsed).__name__}, expected an object.")

        if not isinstance(steps_raw, list) or not steps_raw:
            raise PlannerError("Planner returned no steps.")
        if len(steps_raw) > self._max_steps:
            steps_raw = steps_raw[: self._max_steps]

        validated: list[PlanStep] = []
        for position, raw in enumerate(steps_raw):
            if not isinstance(raw, dict):
                raise PlannerError(f"Step {position} is not an object.")
            raw = dict(raw)
            # Models routinely return 1-based indices; normalise before
            # validating so a harmless off-by-one does not fail the whole plan.
            raw["step_index"] = position
            if not isinstance(raw.get("tool"), str) or raw["tool"] not in available_tools:
                raise PlannerError(
                    f"Step {position} names an unregistered tool: {raw.get('tool')!r}. "
                    f"Valid tools: {sorted(available_tools)}"
                )
            try:
                validated.append(PlanStep(**raw))
            except ValidationError as exc:
                raise PlannerError(f"Step {position} failed validation: {exc}") from exc

        return Plan(goal=goal, steps=validated)


__all__ = ["MultiStepPlanner", "Plan", "PlanStep", "PlannerError"]
