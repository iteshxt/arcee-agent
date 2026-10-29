from pathlib import Path

import pytest

from arcee.core.agent import Agent, Escalated
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.policy import Policy
from arcee.core.routing import start_tier
from arcee.models.base import Completion, ModelProfile, WireCall
from arcee.models.fake import FakeAdapter
from arcee.tools.builtin import builtin_registry
from tests.unit.test_agent import User

PROFILE = ModelProfile(model="small", context_tokens=8192, cost_tier="local", call_mode="native")


@pytest.mark.parametrize(
    ("request_text", "tier"),
    [
        ("How much free disk space do I have?", 0),
        ("Rename ~/a.txt to b.txt", 0),
        ("Look at my Gmail inbox and tell me the newest emails", 1),
        ("Search the web for the GNOME 50 release notes", 1),
        ("First find my CV, then my cover letter, then zip them both", 1),
    ],
)
def test_hard_requests_start_on_the_large_model(request_text: str, tier: int) -> None:
    assert start_tier(request_text) == tier


async def test_routed_tasks_start_and_settle_on_their_base_tier(tmp_path: Path) -> None:
    user = User()
    small = FakeAdapter([], PROFILE)
    large = FakeAdapter(
        [
            Completion("", tool_calls=(WireCall("fs_list", {"path": "~"}),)),
            Completion("done"),
        ],
        PROFILE.model_copy(update={"model": "large"}),
    )
    events: list[object] = []
    agent = Agent(
        [small, large],
        builtin_registry(shell_limits=False),
        Executor(Gate(Policy(), user)),
        user,
        home=tmp_path,
        start_tier=lambda request: 1,
        on_event=events.append,
    )

    outcome = await agent.run("hard request")

    assert outcome.reply == "done"
    assert small.requests == []  # never dropped back below the base tier
    assert not [e for e in events if isinstance(e, Escalated)]
