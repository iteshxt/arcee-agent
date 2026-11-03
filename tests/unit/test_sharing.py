"""Personal data to a cloud model: asked once per task, always, or never (decision 0017)."""

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from arcee.core.agent import SHARE_NO, SHARE_YES, Agent, Event, Sharing
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.policy import Policy
from arcee.models.base import Completion, ModelProfile
from arcee.models.fake import FakeAdapter
from arcee.tools.base import NoArgs, Tool, ToolContext, ToolResult, Usage
from arcee.tools.control import CONTROL_TOOLS
from arcee.tools.registry import Registry
from tests.unit.test_agent import User, call, say

CLOUD = ModelProfile(model="cloud", context_tokens=8192, cost_tier="free", call_mode="native")
LOCAL = CLOUD.model_copy(update={"model": "local", "cost_tier": "local"})


class Inbox(Tool[NoArgs]):
    """A stand-in for a mail tool (under fs.*, which a files request offers)."""

    name = "fs.inbox"
    read_only = False  # so a second look is not blocked as a repeat
    personal = "your emails"
    description = "The latest emails."
    usage = Usage(when="mail", example="fs.inbox()")
    args_model = NoArgs

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        self.runs += 1
        return ToolResult.ok("2 emails", data={"emails": ["Rent due", "Lunch?"]})


def agent(
    profiles: Sequence[ModelProfile],
    scripts: Sequence[Sequence[Completion]],
    user: User,
    sharing: Sharing,
    inbox: Inbox,
) -> tuple[Agent, list[FakeAdapter], list[Event]]:
    ladder = [
        FakeAdapter(script, profile) for profile, script in zip(profiles, scripts, strict=True)
    ]
    ladder[0].label = "Groq" if profiles[0].cost_tier != "local" else "this computer"
    for adapter in ladder[1:]:
        adapter.label = "Groq"
    events: list[Event] = []
    registry = Registry([inbox, *(tool() for tool in CONTROL_TOOLS)])
    built = Agent(
        ladder,
        registry,
        Executor(Gate(Policy(), user)),
        user,
        home=Path("/tmp"),
        on_event=events.append,
        clock=lambda: datetime(2026, 11, 4, 9, 0),
        sharing=sharing,
    )
    return built, ladder, events


REQUEST = "List the files in my inbox"


async def test_it_asks_once_per_task_before_mail_goes_to_the_cloud() -> None:
    user, inbox = User(SHARE_YES), Inbox()
    run, _, _ = agent(
        [CLOUD], [[call("fs_inbox"), call("fs_inbox"), say("Rent is due.")]], user, "ask", inbox
    )

    outcome = await run.run(REQUEST)

    assert user.questions == ["This needs your emails to go to Groq. Go ahead?"]
    assert inbox.runs == 2  # asked once, for the task
    assert outcome.reply == "Rent is due."


async def test_no_keeps_the_mail_from_the_cloud_model() -> None:
    user, inbox = User(SHARE_NO), Inbox()
    run, ladder, _ = agent(
        [CLOUD], [[call("fs_inbox"), say("I can't read them.")]], user, "ask", inbox
    )

    await run.run(REQUEST)

    assert inbox.runs == 0
    refusal = json.loads(ladder[0].requests[1].messages[-1].text)
    assert refusal["status"] == "refused"
    assert "said no" in refusal["error"]


async def test_always_and_off_do_not_ask() -> None:
    cases: tuple[tuple[Sharing, int], ...] = (("always", 1), ("off", 0))
    for sharing, runs in cases:
        user, inbox = User(), Inbox()
        run, _, _ = agent([CLOUD], [[call("fs_inbox"), say("Done.")]], user, sharing, inbox)

        await run.run(REQUEST)

        assert user.questions == [], sharing
        assert inbox.runs == runs, sharing


async def test_the_local_model_never_asks() -> None:
    user, inbox = User(), Inbox()
    run, _, _ = agent([LOCAL], [[call("fs_inbox"), say("Rent is due.")]], user, "ask", inbox)

    await run.run(REQUEST)

    assert user.questions == []
    assert inbox.runs == 1


async def test_mail_read_locally_goes_to_the_cloud_only_if_allowed() -> None:
    # Two bad answers on the local model would hand the task to the cloud tier, with the
    # emails it read: the user is asked first, and a no keeps it on this computer.
    user, inbox = User(SHARE_NO), Inbox()
    local = [
        call("fs_inbox"),
        say("Let me use fs_inbox again"),
        say("Let me use fs_inbox again"),
        say("Rent is due."),
    ]
    run, ladder, _ = agent([LOCAL, CLOUD], [local, [say("never asked")]], user, "ask", inbox)

    outcome = await run.run(REQUEST)

    assert user.questions == ["This needs what it read to go to Groq. Go ahead?"]
    assert ladder[1].requests == []  # nothing went to the cloud model
    assert outcome.reply == "Rent is due."
