import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from arcee.core.agent import Agent, Escalated, Event, Guarded, Limits, ToolRan
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.policy import Policy
from arcee.core.protocol import ConfirmRequest
from arcee.models.base import Completion, Message, ModelProfile, ToolRequest, WireCall
from arcee.models.errors import MalformedReplyError
from arcee.models.fake import FakeAdapter
from arcee.tools.control import CONTROL_TOOLS
from arcee.tools.files import FsList, FsSearch, FsTrash
from arcee.tools.registry import Registry
from arcee.tools.shell import ShellRun

TEN_STEPS = Limits(max_steps=10)
PROFILE = ModelProfile(model="tier0", context_tokens=8192, cost_tier="local", call_mode="native")


def call(name: str, **args: Any) -> Completion:
    return Completion("", tool_calls=(WireCall(name, args),))


def say(text: str) -> Completion:
    return Completion(text)


class User:
    def __init__(self, *answers: str, approve: bool = True) -> None:
        self.answers = list(answers)
        self.questions: list[str] = []
        self.confirmations: list[ConfirmRequest] = []
        self.approve = approve

    async def ask(self, question: str, options: Sequence[str] = ()) -> str:
        self.questions.append(question)
        return self.answers.pop(0)

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.confirmations.append(request)
        return self.approve


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "Downloads").mkdir()
    for name in ("a.deb", "b.deb", "notes.txt"):
        (tmp_path / "Downloads" / name).write_text(name)
    (tmp_path / ".trash").mkdir()
    return tmp_path


def make(
    home: Path,
    *scripts: Sequence[Completion | Exception],
    user: User | None = None,
    limits: Limits = TEN_STEPS,
) -> tuple[Agent, list[FakeAdapter], list[Event], User]:
    user = user or User()
    ladder = [
        FakeAdapter(script, PROFILE.model_copy(update={"model": f"tier{i}"}))
        for i, script in enumerate(scripts)
    ]
    registry = Registry(
        [
            FsList(),
            FsSearch(),
            FsTrash(command=("mv", "-t", str(home / ".trash"), "--")),
            ShellRun(limits=False),
            *(tool() for tool in CONTROL_TOOLS),
        ]
    )
    events: list[Event] = []
    agent = Agent(
        ladder,
        registry,
        Executor(Gate(Policy(), user)),
        user,
        home=home,
        limits=limits,
        on_event=events.append,
        clock=lambda: datetime(2026, 10, 29, 14, 5),
    )
    return agent, ladder, events, user


def tool_results(adapter: FakeAdapter, request: int = -1) -> list[dict[str, Any]]:
    return [json.loads(m.text) for m in adapter.requests[request].messages if m.role == "tool"]


async def test_looks_then_answers_in_plain_text(home: Path) -> None:
    agent, [model], events, _ = make(
        home, [call("fs_list", path="~/Downloads", glob="*.deb"), say("There are 2 .deb files.")]
    )

    outcome = await agent.run("How many installers are in Downloads?")

    assert (outcome.status, outcome.reply, outcome.steps, outcome.model_calls) == (
        "done",
        "There are 2 .deb files.",
        2,
        2,
    )
    [result] = tool_results(model)
    assert result["data"]["count"] == 2
    assert [type(e) for e in events if not isinstance(e, ToolRan)].count(Guarded) == 0


async def test_sends_system_prompt_and_native_tools(home: Path) -> None:
    agent, [model], _, _ = make(home, [say("hi")])

    await agent.run("list the files in my home folder")

    sent = model.requests[0]
    assert str(home) in sent.system
    assert "Thursday 2026-10-29 14:05" in sent.system
    names = [tool["function"]["name"] for tool in sent.tools]
    assert names[:2] == ["fs_list", "fs_search"]
    assert "control_done" in names


async def test_invalid_call_is_answered_with_the_registry_error(home: Path) -> None:
    agent, [model], _, _ = make(
        home, [call("fs_search", path="~", query="deb"), call("fs_list", path="~"), say("ok")]
    )

    await agent.run("find debs")

    [error, _] = tool_results(model)
    assert error["status"] == "error"
    assert "unknown argument 'path'" in error["error"]
    assert "arguments are query, root?, by?" in error["error"]


async def test_exact_repeat_is_not_run_again(home: Path) -> None:
    agent, [model], events, _ = make(
        home,
        [call("fs_list", path="~/Downloads"), call("fs_list", path="~/Downloads"), say("3 files")],
    )

    outcome = await agent.run("what is in Downloads")

    assert outcome.status == "done"
    assert sum(isinstance(e, ToolRan) for e in events) == 1
    assert Guarded(2, "blocked a repeat of fs.list") in events
    repeat = tool_results(model)[1]
    assert "already made this exact call" in repeat["error"]
    assert repeat["previous_result"]["data"]["count"] == 3


async def test_looking_again_after_a_change_is_allowed(home: Path) -> None:
    agent, _, events, user = make(
        home,
        [
            call("fs_list", path="~/Downloads"),
            call("fs_trash", paths=["~/Downloads/a.deb"]),
            call("fs_list", path="~/Downloads"),
            say("done"),
        ],
    )

    await agent.run("trash a.deb")

    assert [e.result.tool for e in events if isinstance(e, ToolRan)] == [
        "fs.list",
        "fs.trash",
        "fs.list",
    ]
    assert user.confirmations[0].args_preview == ("~/Downloads/a.deb",)
    assert not (home / "Downloads" / "a.deb").exists()


async def test_declined_confirmation_reaches_the_model_as_refused(home: Path) -> None:
    agent, [model], _, _ = make(
        home,
        [call("fs_trash", paths=["~/Downloads/a.deb"]), say("ok, kept it")],
        user=User(approve=False),
    )

    await agent.run("trash a.deb")

    [refused] = tool_results(model)
    assert refused["status"] == "refused"
    assert refused["error"] == "the user declined"
    assert "Do not retry" in refused["note"]
    assert (home / "Downloads" / "a.deb").exists()


async def test_never_commands_are_refused_without_asking(home: Path) -> None:
    agent, [model], _, user = make(home, [call("shell_run", cmd="sudo rm -rf /"), say("no")])

    await agent.run("wipe it")

    [refused] = tool_results(model)
    assert refused["status"] == "refused"
    assert user.confirmations == []


async def test_failures_climb_the_ladder_and_success_drops_back(home: Path) -> None:
    agent, [cheap, strong], events, _ = make(
        home,
        [call("fs_nope"), call("fs_nope"), say("cheap again")],
        [call("fs_list", path="~/Downloads")],
    )

    outcome = await agent.run("list downloads")

    assert outcome.reply == "cheap again"
    [escalated] = [e for e in events if isinstance(e, Escalated)]
    assert (escalated.step, escalated.tier, escalated.model) == (2, 1, "tier1")
    assert escalated.reason.startswith("invalid call: unknown tool 'fs_nope'; tools are fs_list")
    assert len(strong.requests) == 1
    assert len(cheap.requests) == 3


async def test_broken_tool_calls_are_retried_with_a_nudge(home: Path) -> None:
    broken = MalformedReplyError("lfm3 wrote an unparseable reply: XML syntax error")
    agent, [_, strong], events, _ = make(home, [broken, broken], [say("Hello!")])

    outcome = await agent.run("Hi Arcee!")

    assert (outcome.status, outcome.reply) == ("done", "Hello!")
    assert Guarded(1, "the model wrote a broken tool call; asking again") in events
    assert any(isinstance(e, Escalated) for e in events)
    assert strong.requests[0].messages[-1].text.startswith("Your last reply was a broken tool call")
    assert outcome.model_calls == 3


async def test_narration_naming_tools_is_not_an_answer(home: Path) -> None:
    musing = say("Let me use fs_search to look for it, or maybe shell_run.")
    agent, [model], events, _ = make(home, [musing, say("It's in ~/Documents.")])

    outcome = await agent.run("where is my cv?")

    assert outcome.reply == "It's in ~/Documents."
    assert Guarded(1, "reply was narration naming fs_search; nudged") in events
    assert model.requests[1].messages[-1].text.startswith("That was a note to yourself")


async def test_switching_models_unloads_the_previous_one(home: Path) -> None:
    agent, [cheap, strong], _, _ = make(
        home,
        [call("fs_nope"), call("fs_nope"), say("done")],
        [call("fs_list", path="~/Downloads")],
    )

    await agent.run("list downloads")

    # Escalation unloaded the cheap model; success unloaded the strong one again.
    assert (cheap.unloads, strong.unloads) == (1, 1)


async def test_same_model_tiers_are_not_unloaded(home: Path) -> None:
    agent, [fast, thinking], _, _ = make(
        home, [call("fs_nope"), call("fs_nope"), say("ok")], [call("fs_list", path="~")]
    )
    thinking.profile = fast.profile.model_copy(update={"thinking": True})

    await agent.run("list home")

    assert (fast.unloads, thinking.unloads) == (0, 0)


async def test_answers_with_unseen_facts_are_sent_back(home: Path) -> None:
    agent, [model], events, _ = make(
        home,
        [
            call("fs_list", path="~/Downloads"),
            say("The installer is ~/Downloads/c.deb."),
            call("control_done", reply="The installer is z.deb."),
            say("The installers are a.deb and b.deb."),
        ],
    )

    outcome = await agent.run("which installers are in Downloads?")

    assert outcome.reply == "The installers are a.deb and b.deb."
    guards = [e.what for e in events if isinstance(e, Guarded)]
    assert guards == [
        "reply stated ~/Downloads/c.deb, c.deb unseen; nudged",
        "reply stated z.deb unseen; nudged",
    ]
    assert "c.deb" in model.requests[2].messages[-1].text


async def test_grounding_gives_up_after_two_nudges(home: Path) -> None:
    agent, _, _, _ = make(
        home,
        [call("fs_list", path="~/Downloads"), say("x.iso"), say("y.iso"), say("z.iso")],
    )

    outcome = await agent.run("which isos?")

    assert outcome.reply == "z.iso"


async def test_top_tier_keeps_going_until_the_step_limit(home: Path) -> None:
    agent, _, _, _ = make(home, [say("")] * 3, limits=Limits(max_steps=3))

    outcome = await agent.run("x")

    assert (outcome.status, outcome.steps) == ("failed", 3)
    assert outcome.reply == "I stopped after 3 steps without finishing."


async def test_three_empty_searches_make_the_agent_ask(home: Path) -> None:
    user = User("It's called itr-2026.pdf, in ~/Documents")
    agent, [model], events, _ = make(
        home,
        [
            call("fs_search", query="tax"),
            call("fs_search", query="itr"),
            call("fs_search", query="income", by="content"),
            say("thanks"),
        ],
        user=user,
    )

    await agent.run("open my tax return")

    assert user.questions == [
        "I searched for 'tax', 'itr', 'income' and found nothing. "
        "Where is it, or what is it called?"
    ]
    assert Guarded(3, "asked the user after 3 empty searches") in events
    last = model.requests[-1].messages[-1]
    assert last == Message(
        "user", f"(You asked: {user.questions[0]})\nIt's called itr-2026.pdf, in ~/Documents"
    )


async def test_a_search_with_results_resets_the_count(home: Path) -> None:
    user = User()
    agent, _, _, _ = make(
        home,
        [
            call("fs_search", query="tax"),
            call("fs_search", query="deb"),
            call("fs_search", query="itr"),
            call("fs_search", query="xyz"),
            say("none"),
        ],
        user=user,
    )

    await agent.run("search")

    assert user.questions == []


async def test_model_can_ask_the_user(home: Path) -> None:
    user = User("the newest one")
    agent, [model], _, _ = make(
        home, [call("control_ask_user", question="Which file?"), say("ok")], user=user
    )

    await agent.run("open the file")

    assert user.questions == ["Which file?"]
    assert tool_results(model)[0] == {"status": "ok", "data": {"answer": "the newest one"}}


async def test_a_repeated_question_is_not_asked_again(home: Path) -> None:
    user = User("")
    question = call("control_ask_user", question="Do you want me to rename it?")
    agent, [_, strong], events, _ = make(
        home, [question, question, question], [say("Renamed.")], user=user
    )

    outcome = await agent.run("rename a.txt to b.txt")

    assert user.questions == ["Do you want me to rename it?"]
    assert Guarded(2, "blocked a repeated question to the user") in events
    assert outcome.reply == "Renamed."  # two blocked repeats were two strikes: escalated
    assert len(strong.requests) == 1


async def test_done_tool_and_replan_end_the_task(home: Path) -> None:
    done, _, _, _ = make(home, [call("control_done", reply="All set.")])
    stuck, _, _, _ = make(home, [call("control_replan", reason="no network")])

    assert (await done.run("x")).reply == "All set."
    outcome = await stuck.run("x")
    assert (outcome.status, outcome.reply) == ("failed", "I could not finish: no network")


async def test_wall_clock_limit(home: Path) -> None:
    class Slow(FakeAdapter):
        async def respond(self, request: ToolRequest) -> Completion:
            await asyncio.sleep(1)
            return say("late")

    user = User()
    agent = Agent(
        [Slow([], PROFILE)],
        Registry(),
        Executor(Gate(Policy(), user)),
        user,
        home=home,
        limits=Limits(max_seconds=0.05),
    )

    outcome = await agent.run("x")

    assert outcome.status == "failed"
    assert "stopped after" in outcome.reply


def test_needs_a_model() -> None:
    with pytest.raises(ValueError, match="at least one model"):
        Agent([], Registry(), Executor(Gate(Policy(), User())), User(), home=Path("/tmp"))
