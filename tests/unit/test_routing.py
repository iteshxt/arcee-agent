from pathlib import Path

from arcee.core.agent import Agent, Escalated
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.policy import Policy
from arcee.models.base import Completion, ModelProfile, WireCall
from arcee.models.fake import FakeAdapter
from arcee.tools.builtin import builtin_registry
from tests.unit.test_agent import User

PROFILE = ModelProfile(model="small", context_tokens=8192, cost_tier="local", call_mode="native")


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
