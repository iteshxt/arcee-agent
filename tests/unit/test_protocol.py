import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from arcee.core.protocol import (
    THOUGHT_MAX_CHARS,
    ConfirmRequest,
    CoreAction,
    CoreInput,
    ExecutorResult,
    NoObservation,
    PlannerOutput,
    TextObservation,
)

# The examples from spec section 7, minus `task_id` on the Planner output, which the
# orchestrator attaches rather than the model.
PLANNER_EXAMPLE: dict[str, Any] = {
    "v": 1,
    "goal": "Find the cheapest train from Abu Road to Jaipur on 22 Oct and tell me",
    "interpretation": "User wants prices only, no booking. Date is tomorrow.",
    "clarify": None,
    "steps": [
        {
            "id": 1,
            "do": "Open the IRCTC train search page",
            "tools": ["browser.*"],
            "success": "search form visible",
        },
        {
            "id": 2,
            "do": "Fill from=Abu Road, to=Jaipur, date=2026-10-22 and search",
            "tools": ["browser.*"],
            "success": "results list visible",
        },
        {
            "id": 3,
            "do": "Read the results and report the cheapest option",
            "tools": ["browser.read_text", "control.done"],
            "success": "reply sent",
        },
    ],
    "constraints": ["do not log in", "do not pay", "stay on irctc.co.in"],
    "needs": {"browser": True, "desktop_gui": False, "shell": False},
    "memory_used": ["m_8812"],
    "estimated_steps": 8,
}

CORE_ACTION_EXAMPLE: dict[str, Any] = {
    "v": 1,
    "thought": "The To field is empty; fill it before searching.",
    "action": {"tool": "browser.fill", "args": {"target": "el_4", "text": "Jaipur"}},
    "step_status": "in_progress",
    "confidence": 0.9,
    "needs_confirmation": False,
}


def core_action(**overrides: Any) -> dict[str, Any]:
    return CORE_ACTION_EXAMPLE | overrides


class TestPlannerOutput:
    def test_parses_spec_example(self) -> None:
        plan = PlannerOutput.model_validate(PLANNER_EXAMPLE)

        assert [step.id for step in plan.steps] == [1, 2, 3]
        assert plan.steps[2].tools == ("browser.read_text", "control.done")
        assert plan.needs.browser

    def test_ignores_unknown_keys_from_the_model(self) -> None:
        plan = PlannerOutput.model_validate(PLANNER_EXAMPLE | {"task_id": "t_1", "notes": "x"})

        assert "task_id" not in plan.model_dump()

    def test_clarify_allows_an_empty_plan(self) -> None:
        plan = PlannerOutput.model_validate(
            {"goal": "Send the file", "interpretation": "", "clarify": "Which file?"}
        )

        assert plan.steps == ()

    def test_rejects_empty_plan_without_clarify(self) -> None:
        with pytest.raises(ValidationError, match="steps must not be empty"):
            PlannerOutput.model_validate(PLANNER_EXAMPLE | {"steps": []})

    @pytest.mark.parametrize("ids", [[0, 1, 2], [1, 3, 2], [1, 1, 2]])
    def test_rejects_step_ids_out_of_order(self, ids: list[int]) -> None:
        steps = [step | {"id": i} for step, i in zip(PLANNER_EXAMPLE["steps"], ids, strict=True)]

        with pytest.raises(ValidationError):
            PlannerOutput.model_validate(PLANNER_EXAMPLE | {"steps": steps})

    @pytest.mark.parametrize("tool_filter", ["*", "fs.*", "fs.read", "mcp.github.*"])
    def test_accepts_tool_filters(self, tool_filter: str) -> None:
        step = PLANNER_EXAMPLE["steps"][0] | {"tools": [tool_filter]}

        PlannerOutput.model_validate(PLANNER_EXAMPLE | {"steps": [step]})

    @pytest.mark.parametrize("tool_filter", ["", "FS.read", "fs.", "*.read", "fs read"])
    def test_rejects_malformed_tool_filters(self, tool_filter: str) -> None:
        step = PLANNER_EXAMPLE["steps"][0] | {"tools": [tool_filter]}

        with pytest.raises(ValidationError):
            PlannerOutput.model_validate(PLANNER_EXAMPLE | {"steps": [step]})

    def test_rejects_blank_goal(self) -> None:
        with pytest.raises(ValidationError, match="must not be blank"):
            PlannerOutput.model_validate(PLANNER_EXAMPLE | {"goal": "  "})


class TestCoreAction:
    def test_parses_spec_example(self) -> None:
        action = CoreAction.model_validate(CORE_ACTION_EXAMPLE)

        assert action.action.tool == "browser.fill"
        assert action.action.args == {"target": "el_4", "text": "Jaipur"}

    def test_clips_long_thought_instead_of_failing(self) -> None:
        action = CoreAction.model_validate(core_action(thought="x" * 1000))

        assert len(action.thought) == THOUGHT_MAX_CHARS

    @pytest.mark.parametrize(
        ("tool", "expected"), [("control.done", "task_done"), ("control.replan", "stuck")]
    )
    def test_control_tool_sets_step_status(self, tool: str, expected: str) -> None:
        action = CoreAction.model_validate(
            core_action(action={"tool": tool, "args": {}}, step_status="in_progress")
        )

        assert action.step_status == expected

    @pytest.mark.parametrize("status", ["task_done", "stuck"])
    def test_rejects_terminal_status_without_matching_tool(self, status: str) -> None:
        with pytest.raises(ValidationError, match="requires tool"):
            CoreAction.model_validate(core_action(step_status=status))

    def test_defaults_cover_minimal_reply(self) -> None:
        action = CoreAction.model_validate({"action": {"tool": "fs.list"}})

        assert action.step_status == "in_progress"
        assert action.action.args == {}
        assert not action.needs_confirmation

    @pytest.mark.parametrize("tool", ["fs", "FS.read", "fs..read", "fs.read()", ""])
    def test_rejects_malformed_tool_names(self, tool: str) -> None:
        with pytest.raises(ValidationError):
            CoreAction.model_validate(core_action(action={"tool": tool}))

    @pytest.mark.parametrize("confidence", [-0.1, 1.5])
    def test_rejects_confidence_outside_unit_range(self, confidence: float) -> None:
        with pytest.raises(ValidationError):
            CoreAction.model_validate(core_action(confidence=confidence))

    def test_rejects_other_protocol_versions(self) -> None:
        with pytest.raises(ValidationError):
            CoreAction.model_validate(core_action(v=2))

    def test_is_immutable(self) -> None:
        action = CoreAction.model_validate(CORE_ACTION_EXAMPLE)

        with pytest.raises(ValidationError):
            action.thought = "changed"  # pyright: ignore[reportAttributeAccessIssue]


class TestHarnessModels:
    def test_core_input_selects_observation_by_kind(self) -> None:
        core_input = CoreInput.model_validate(
            {
                "task": {"goal": "List my downloads"},
                "plan": [{"id": 1, "do": "List ~/Downloads", "state": "current"}],
                "step": {"id": 1, "do": "List ~/Downloads", "success": "listing shown"},
                "observation": {"kind": "shell_output", "text": "a.pdf\nb.zip"},
                "tools": ["fs.list", "control.done"],
            }
        )

        assert isinstance(core_input.observation, TextObservation)
        assert core_input.observation.text == "a.pdf\nb.zip"

    def test_core_input_defaults_to_no_observation(self) -> None:
        core_input = CoreInput(
            task={"goal": "g"},  # pyright: ignore[reportArgumentType]
            plan=(),
            step={"id": 1, "do": "d", "success": "s"},  # pyright: ignore[reportArgumentType]
            tools=(),
        )

        assert core_input.observation == NoObservation()

    def test_harness_models_reject_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            ExecutorResult.model_validate(
                {
                    "n": 1,
                    "tool": "fs.list",
                    "status": "ok",
                    "summary": "ok",
                    "duration_ms": 3,
                    "extra": 1,
                }
            )

    def test_executor_result_carries_structured_data(self) -> None:
        data = {"entries": [{"name": "a.deb"}], "count": 1}
        result = ExecutorResult(
            n=1, tool="fs.list", status="ok", summary="1 entry", data=data, duration_ms=3
        )

        assert ExecutorResult.model_validate_json(result.model_dump_json()).data == data

    def test_confirm_request_round_trips_through_json(self) -> None:
        request = ConfirmRequest(
            request_id="c_552",
            task_id="t_01JAX9K2",
            tool="fs.trash",
            sentence="Move 3 files from ~/Downloads to the trash?",
            risk="medium",
            args_preview=("a.pdf", "b.pdf", "c.zip"),
        )

        assert ConfirmRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("model", [PlannerOutput, CoreAction])
def test_model_reply_schemas_serialise_to_json(model: type[BaseModel]) -> None:
    schema = model.model_json_schema()

    assert schema["type"] == "object"
    json.dumps(schema)
