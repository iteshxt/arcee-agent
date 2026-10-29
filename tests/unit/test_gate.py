import asyncio

import pytest

from arcee.core.gate import Gate
from arcee.core.policy import Policy, Tier
from arcee.core.protocol import ConfirmRequest
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.registry import Registry, ToolCall


class ReadArgs(ToolArgs):
    path: str


class Read(Tool[ReadArgs]):
    name = "fs.read"
    description = "Read a file."
    usage = Usage(when="w")
    args_model = ReadArgs

    async def run(self, args: ReadArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("read")


class TrashArgs(ToolArgs):
    paths: list[str]


class Trash(Tool[TrashArgs]):
    name = "fs.trash"
    description = "Trash files."
    usage = Usage(when="w")
    args_model = TrashArgs
    tier = Tier.CONFIRM
    risk = "medium"

    def sentence(self, args: TrashArgs) -> str:
        return f"Move {len(args.paths)} item(s) to the trash?"

    def preview(self, args: TrashArgs) -> tuple[str, ...]:
        return tuple(args.paths)

    async def run(self, args: TrashArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("trashed")


class RunArgs(ToolArgs):
    cmd: str


class Run(Tool[RunArgs]):
    name = "shell.run"
    description = "Run a command."
    usage = Usage(when="w")
    args_model = RunArgs

    def tier_for(self, args: RunArgs, policy: Policy) -> Tier:
        return policy.classify_shell(args.cmd).tier

    def refusal(self, args: RunArgs, policy: Policy) -> str:
        return policy.classify_shell(args.cmd).reason

    async def run(self, args: RunArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("ran")


TOOLS = Registry([Read(), Trash(), Run()])


def call(name: str, **args: object) -> ToolCall:
    return TOOLS.validate(name, args)


class Answers:
    def __init__(self, *answers: bool) -> None:
        self._answers = list(answers)
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        return self._answers.pop(0)


class Silent:
    async def confirm(self, request: ConfirmRequest) -> bool:
        await asyncio.sleep(10)
        return True


async def test_auto_tools_run_without_asking() -> None:
    answers = Answers()

    verdict = await Gate(Policy(), answers).check(call("fs.read", path="~/a"), "t_1")

    assert (verdict.allowed, verdict.tier, verdict.approved_by) == (True, Tier.AUTO, "policy")
    assert answers.requests == []


async def test_confirm_tools_ask_with_sentence_and_targets() -> None:
    answers = Answers(True)

    verdict = await Gate(Policy(), answers).check(call("fs.trash", paths=["~/a", "~/b"]), "t_1")

    assert (verdict.allowed, verdict.approved_by) == (True, "user")
    request = answers.requests[0]
    assert (request.request_id, request.task_id, request.tool) == ("c_1", "t_1", "fs.trash")
    assert request.sentence == "Move 2 item(s) to the trash?"
    assert request.args_preview == ("~/a", "~/b")
    assert request.risk == "medium"


async def test_declined_confirmation_refuses() -> None:
    verdict = await Gate(Policy(), Answers(False)).check(call("fs.trash", paths=["~/a"]), "t_1")

    assert (verdict.allowed, verdict.reason) == (False, "the user declined")


async def test_unanswered_confirmation_is_denied() -> None:
    gate = Gate(Policy(), Silent(), timeout_s=0.01)

    verdict = await gate.check(call("fs.trash", paths=["~/a"]), "t_1")

    assert (verdict.allowed, verdict.reason) == (False, "the user did not answer in time")


async def test_never_is_refused_with_the_tools_reason() -> None:
    answers = Answers()

    verdict = await Gate(Policy(), answers).check(call("shell.run", cmd="sudo ls"), "t_1")

    assert (verdict.allowed, verdict.tier) == (False, Tier.NEVER)
    assert verdict.reason == "'sudo' is never run by Arcee"
    assert answers.requests == []


@pytest.mark.parametrize(
    ("policy", "model_asked", "expected"),
    [
        (Policy(), False, Tier.AUTO),
        (Policy(), True, Tier.CONFIRM),
        (Policy(confirm_all=True), False, Tier.CONFIRM),
        (Policy(tier_overrides={"fs.read": Tier.CONFIRM}), False, Tier.CONFIRM),
    ],
)
def test_auto_can_only_be_raised(policy: Policy, model_asked: bool, expected: Tier) -> None:
    assert (
        Gate(policy, Answers()).tier(call("fs.read", path="~"), model_asked=model_asked) is expected
    )


def test_overrides_can_lower_confirm_but_never_lift_never() -> None:
    policy = Policy(tier_overrides={"fs.trash": Tier.AUTO, "shell.run": Tier.AUTO})
    gate = Gate(policy, Answers())

    assert gate.tier(call("fs.trash", paths=["~/a"])) is Tier.AUTO
    assert gate.tier(call("shell.run", cmd="rm -rf ~")) is Tier.NEVER


def test_policy_can_ban_a_tool() -> None:
    gate = Gate(Policy(tier_overrides={"fs.read": Tier.NEVER}), Answers())

    assert gate.tier(call("fs.read", path="~")) is Tier.NEVER
