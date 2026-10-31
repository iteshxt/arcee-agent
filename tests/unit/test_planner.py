import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from arcee.core.agent import Agent, Event, Guarded, Planned, StepStarted
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.planner import Planner, needs_plan
from arcee.core.policy import Policy
from arcee.core.protocol import ConfirmRequest, ReplanContext
from arcee.models.base import Completion, ModelProfile, WireCall
from arcee.models.errors import QuotaExhaustedError
from arcee.models.fake import FakeAdapter
from arcee.tools.builtin import builtin_registry

NOW = datetime(2026, 10, 29, 9, 30)
CORE = ModelProfile(model="core", context_tokens=8192, cost_tier="local", call_mode="native")
PLANNER = ModelProfile(
    model="planner",
    context_tokens=8192,
    cost_tier="local",
    call_mode="schema",
    json_mode="schema",
)
TOOLS = builtin_registry(shell_limits=False)


def plan(*steps: tuple[str, list[str]], clarify: str | None = None, **extra: Any) -> str:
    body: dict[str, Any] = {
        "goal": "g",
        "interpretation": "",
        "clarify": clarify,
        "steps": [
            {"id": i, "do": do, "tools": tools, "success": "s"}
            for i, (do, tools) in enumerate(steps, start=1)
        ],
        **extra,
    }
    return json.dumps(body)


def call(name: str, **args: Any) -> Completion:
    return Completion("", tool_calls=(WireCall(name, args),))


class User:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []

    async def ask(self, question: str, options: Sequence[str] = ()) -> str:
        self.questions.append(question)
        return self.answers.pop(0)

    async def confirm(self, request: ConfirmRequest) -> bool:
        return False


def make(
    plans: Sequence[str | Exception],
    core: Sequence[Completion],
    user: User | None = None,
    home: Path = Path("/tmp"),
    plan_if: Callable[[str], bool] = lambda request: True,
) -> tuple[Agent, FakeAdapter, FakeAdapter, list[Event], User]:
    user = user or User()
    planner_model = FakeAdapter(plans, PLANNER)
    core_model = FakeAdapter(core, CORE)
    events: list[Event] = []
    agent = Agent(
        [core_model],
        TOOLS,
        Executor(Gate(Policy(), user)),
        user,
        home=home,
        planner=Planner(planner_model, TOOLS, home=home, clock=lambda: NOW),
        plan_if=plan_if,
        on_event=events.append,
        clock=lambda: NOW,
    )
    return agent, planner_model, core_model, events, user


class TestPlanner:
    async def test_prompt_lists_tools_and_request(self) -> None:
        model = FakeAdapter([plan(("answer", ["control.done"]))], PLANNER)

        result = await Planner(model, TOOLS, home=Path("/home/u"), clock=lambda: NOW).plan("hi")

        sent = model.requests[0]
        assert "- fs.list: List a folder" in sent.system
        assert "- shell.run: Run one Linux command" in sent.system
        assert "/home/u" in sent.system
        assert sent.messages[0].text == "Request: hi"
        assert sent.schema is not None
        assert sent.max_tokens == 4096  # room for gpt-oss's reasoning and a long plan
        assert result.steps[0].do == "answer"

    async def test_includes_answers_and_replan_context(self) -> None:
        model = FakeAdapter([plan(("x", ["*"]))], PLANNER)
        planner = Planner(model, TOOLS, home=Path("/h"))
        context = ReplanContext(
            failed_step=2, history_summary="no network", tried=("fs.list ~",), replan_count=1
        )

        await planner.plan("do it", answers=[("Which one?", "the first")], replan=context)

        text = model.requests[0].messages[0].text
        assert "You asked: Which one?\nThe user answered: the first" in text
        assert "failed at step 2 (attempt 1). What happened: no network." in text
        assert "Already tried: fs.list ~." in text

    def test_needs_schema_mode(self) -> None:
        with pytest.raises(ValueError, match="schema call mode"):
            Planner(FakeAdapter([], CORE), TOOLS, home=Path("/h"))


class TestPlannedTasks:
    async def test_runs_steps_in_order_with_their_tools(self, tmp_path: Path) -> None:
        agent, _, core, events, _ = make(
            [plan(("List the folder", ["fs.list"]), ("Report the count", ["control.done"]))],
            [call("fs_list", path="~"), Completion("Found 0 files."), Completion("There are 0.")],
            home=tmp_path,
        )

        outcome = await agent.run("how many files in ~?")

        assert (outcome.status, outcome.reply) == ("done", "There are 0.")
        first, _, third = core.requests
        names = [t["function"]["name"] for t in first.tools]
        assert names[0] == "fs_list"  # the step's own tools come first
        assert {"control_done", "control_ask_user", "control_replan"} <= set(names)
        assert "1. [current] List the folder" in first.system
        assert "2. [next] Report the count" in first.system
        assert "2. [current] Report the count" in third.system
        assert "answer their whole request" in third.system
        # Step 2 starts fresh: the request, step 1's note, its own turn; no step 1 calls.
        assert [m.text for m in third.messages] == [
            "how many files in ~?",
            "Done so far (earlier tool results are not shown again):\n"
            "Step 1: List the folder\nResult: Found 0 files.",
            "Now step 2: Report the count",
        ]
        assert [e for e in events if isinstance(e, StepStarted)] == [
            StepStarted(1, 2, "List the folder"),
            StepStarted(2, 2, "Report the count"),
        ]
        assert outcome.model_calls == 4  # one plan, three Core turns

    async def test_a_handoff_note_may_name_tools(self) -> None:
        note = "Opened the inbox; browser_get_elements lists the rows next."
        agent, _, core, events, _ = make(
            [plan(("Open Gmail", ["browser.*"]), ("Report", ["control.done"]))],
            [Completion(note), Completion("Nothing unread.")],
        )

        outcome = await agent.run("check my gmail")

        assert (outcome.status, outcome.reply) == ("done", "Nothing unread.")
        assert not [e for e in events if isinstance(e, Guarded)]
        assert len(core.requests) == 2

    async def test_one_step_plans_keep_the_plain_prompt(self) -> None:
        agent, _, core, events, _ = make([plan(("Say hello", ["*"]))], [Completion("Hello!")])

        await agent.run("hi")

        assert "Plan:" not in core.requests[0].system
        assert isinstance(events[0], Planned)

    async def test_constraints_reach_the_core(self) -> None:
        agent, _, core, _, _ = make(
            [plan(("Look", ["fs.*"]), constraints=["do not delete anything"])],
            [Completion("ok")],
        )

        await agent.run("look around")

        assert "Constraints: do not delete anything" in core.requests[0].system

    async def test_clarify_asks_then_replans(self) -> None:
        user = User("the PDF one")
        agent, planner, _, _, _ = make(
            [plan(clarify="Which report?"), plan(("Open it", ["*"]))],
            [Completion("Opened.")],
            user=user,
        )

        outcome = await agent.run("open the report")

        assert outcome.reply == "Opened."
        assert user.questions == ["Which report?"]
        assert "The user answered: the PDF one" in planner.requests[1].messages[0].text

    async def test_endless_clarify_fails_with_the_question(self) -> None:
        agent, _, _, _, _ = make([plan(clarify="Which?")] * 3, [], user=User("a", "b"))

        outcome = await agent.run("do the thing")

        assert (outcome.status, outcome.reply) == ("failed", "Which?")

    async def test_stuck_core_gets_a_new_plan(self) -> None:
        agent, planner, core, events, _ = make(
            [plan(("Try A", ["*"])), plan(("Try B", ["*"]))],
            [call("control_replan", reason="A is impossible"), Completion("Done with B.")],
        )

        outcome = await agent.run("task")

        assert outcome.reply == "Done with B."
        assert "What happened: A is impossible" in planner.requests[1].messages[0].text
        assert [e.attempt for e in events if isinstance(e, Planned)] == [0, 1]
        assert core.requests[1].messages[-1].text == "New plan. Step 1: Try B"

    async def test_replans_are_limited(self) -> None:
        stuck = call("control_replan", reason="nope")
        agent, _, _, _, _ = make([plan(("Try", ["*"]))] * 3, [stuck] * 3)

        outcome = await agent.run("task")

        assert (outcome.status, outcome.reply) == ("failed", "I could not finish: nope")

    async def test_control_only_steps_offer_only_control_tools(self) -> None:
        agent, _, core, events, _ = make([plan(("Greet", ["control.*"]))], [Completion("Hi!")])

        await agent.run("hi")

        assert [t["function"]["name"] for t in core.requests[0].tools] == [
            "control_wait",
            "control_speak",
            "control_note",
            "control_done",
            "control_ask_user",
            "control_replan",
        ]
        assert not [e for e in events if isinstance(e, Guarded)]

    async def test_simple_requests_skip_the_planner(self) -> None:
        agent, planner, _, _, _ = make([], [Completion("Hello!")], plan_if=needs_plan)

        outcome = await agent.run("Hi Arcee!")

        assert outcome.reply == "Hello!"
        assert planner.requests == []
        assert outcome.model_calls == 1

    async def test_unknown_step_tools_fall_back_to_the_requests_tools(self) -> None:
        agent, _, core, events, _ = make([plan(("Do it", ["calendar.*"]))], [Completion("ok")])

        await agent.run("list the files in my home folder")

        names = {t["function"]["name"] for t in core.requests[0].tools}
        assert "fs_list" in names
        assert not [e for e in events if isinstance(e, Guarded)]


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("Hi Arcee!", False),
        ("How much free disk space do I have?", False),
        ("Find the biggest file in Downloads, then tell me the free disk space", False),
        ("List my Documents. Delete the zip files there.", False),
        ("First find my CV, then my cover letter, then zip them both", True),
        ("Check the logs. Find the errors. Summarise what broke.", True),
        (" ".join(["word"] * 30), True),
    ],
)
def test_needs_plan(request_text: str, expected: bool) -> None:
    assert needs_plan(request_text) is expected


async def test_a_failing_planner_does_not_end_the_task() -> None:
    from arcee.models.errors import ModelUnavailableError

    agent, _, _, events, _ = make(
        [ModelUnavailableError("HTTP 400")], [Completion("Done without a plan.")]
    )

    outcome = await agent.run("a long request that would be planned")

    assert outcome.reply == "Done without a plan."
    assert any(isinstance(e, Guarded) and "running unplanned" in e.what for e in events)


def two_models(
    plans: Sequence[str],
    small: Sequence[Completion | Exception],
    large: Sequence[Completion | Exception],
) -> tuple[Agent, FakeAdapter, FakeAdapter, list[Event]]:
    """An agent on a small and a large model, with hard requests routed to the large one."""
    user = User()
    small_model = FakeAdapter(small, CORE.model_copy(update={"model": "small"}))
    large_model = FakeAdapter(large, CORE.model_copy(update={"model": "large"}))
    events: list[Event] = []
    agent = Agent(
        [small_model, large_model],
        TOOLS,
        Executor(Gate(Policy(), user)),
        user,
        home=Path("/tmp"),
        planner=Planner(FakeAdapter(plans, PLANNER), TOOLS, home=Path("/tmp"), clock=lambda: NOW),
        start_tier=lambda request: 1,
        on_event=events.append,
        clock=lambda: NOW,
    )
    return agent, small_model, large_model, events


def steps_plan(*steps: tuple[str, bool]) -> str:
    return json.dumps(
        {
            "goal": "g",
            "interpretation": "",
            "steps": [
                {"id": i, "do": do, "tools": ["*"], "success": "s", "judgement": judgement}
                for i, (do, judgement) in enumerate(steps, start=1)
            ],
        }
    )


class TestStepModels:
    async def test_mechanical_steps_run_on_the_small_model(self) -> None:
        plan_text = steps_plan(("Open Gmail", False), ("Read the unread mail", True))
        agent, small, large, _ = two_models(
            [plan_text], [Completion("Opened.")], [Completion("Two need you.")]
        )

        outcome = await agent.run("check my gmail and tell me what needs me")

        assert outcome.reply == "Two need you."
        assert len(small.requests) == 1
        assert "Now step 2" in large.requests[0].messages[-1].text

    async def test_a_spent_model_hands_the_task_to_the_other(self) -> None:
        plan_text = steps_plan(("Read the unread mail", True))
        spent = QuotaExhaustedError("large: its tokens for the day are used up")
        agent, small, _, events = two_models(
            [plan_text], [Completion("Nothing needs you.")], [spent]
        )

        outcome = await agent.run("check my gmail")

        assert (outcome.status, outcome.reply) == ("done", "Nothing needs you.")
        assert len(small.requests) == 1
        assert any(isinstance(e, Guarded) and "continuing on small" in e.what for e in events)

    async def test_when_every_model_is_spent_the_error_stands(self) -> None:
        spent = QuotaExhaustedError("used up")
        agent, _, _, _ = two_models([steps_plan(("Read", True))], [spent], [spent])

        with pytest.raises(QuotaExhaustedError):
            await agent.run("check my gmail")
