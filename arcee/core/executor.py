"""The executor (spec section 6, steps 4-6): gate, run, and report every tool call.

A tool can fail, time out or crash; none of that reaches the loop as an
exception. The loop always gets an `ExecutorResult` it can show the model.
"""

import asyncio
import time

from arcee.core.gate import Gate
from arcee.core.protocol import ExecutorResult
from arcee.tools.base import ToolContext
from arcee.tools.registry import ToolCall


class Executor:
    def __init__(self, gate: Gate) -> None:
        self.gate = gate

    async def execute(
        self, call: ToolCall, n: int, ctx: ToolContext, *, model_asked: bool = False
    ) -> ExecutorResult:
        tool = call.tool
        verdict = await self.gate.check(call, ctx.task_id, model_asked=model_asked)
        if not verdict.allowed:
            return ExecutorResult(
                n=n,
                tool=tool.name,
                status="refused",
                summary=f"Refused: {verdict.reason}",
                error=verdict.reason,
                duration_ms=0,
            )
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(tool.run(call.args, ctx), tool.timeout_s)
        except TimeoutError:
            return ExecutorResult(
                n=n,
                tool=tool.name,
                status="timeout",
                summary=f"{tool.name} timed out after {tool.timeout_s:g}s",
                error="timed out",
                duration_ms=_ms(started),
            )
        except Exception as exc:  # a crashing tool must not take the task down with it
            return ExecutorResult(
                n=n,
                tool=tool.name,
                status="error",
                summary=f"{tool.name} failed: {type(exc).__name__}",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=_ms(started),
            )
        return ExecutorResult(
            n=n,
            tool=tool.name,
            status=result.status,
            summary=result.summary,
            data=result.data,
            output=result.output,
            error=result.error,
            duration_ms=_ms(started),
            screen_changed=result.screen_changed,
        )


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
