import asyncio
from pathlib import Path

from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.policy import Policy
from arcee.core.protocol import ConfirmRequest
from arcee.tools.base import NoArgs, Tool, ToolContext, ToolResult, Usage
from arcee.tools.registry import Registry

CTX = ToolContext(task_id="t_1", home=Path("/home/u"))


class Never:
    async def confirm(self, request: ConfirmRequest) -> bool:
        raise AssertionError("auto tools must not ask")


class Fine(Tool[NoArgs]):
    name = "test.fine"
    description = "d"
    usage = Usage(when="w")
    args_model = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("fine", data={"x": 1})


class Slow(Fine):
    name = "test.slow"
    timeout_s = 0.01

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult.ok("late")


class Crashes(Fine):
    name = "test.crash"

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        raise OSError("disk on fire")


TOOLS = Registry([Fine(), Slow(), Crashes()])
EXECUTOR = Executor(Gate(Policy(), Never()))


async def test_reports_the_tools_result() -> None:
    result = await EXECUTOR.execute(TOOLS.validate("test.fine", {}), 3, CTX)

    assert (result.n, result.tool, result.status, result.summary) == (3, "test.fine", "ok", "fine")
    assert result.data == {"x": 1}


async def test_timeout_becomes_a_result() -> None:
    result = await EXECUTOR.execute(TOOLS.validate("test.slow", {}), 1, CTX)

    assert (result.status, result.summary) == ("timeout", "test.slow timed out after 0.01s")


async def test_crash_becomes_an_error_result() -> None:
    result = await EXECUTOR.execute(TOOLS.validate("test.crash", {}), 1, CTX)

    assert (result.status, result.error) == ("error", "OSError: disk on fire")
