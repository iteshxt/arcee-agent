"""The executor (spec section 6, steps 4-6): gate, run, and report every tool call.

A tool can fail, time out or crash; none of that reaches the loop as an
exception. The loop always gets an `ExecutorResult` it can show the model.
Every call that reaches the executor, refused ones included, is also handed to
the optional `record` hook with its arguments, tier and approver: that is what
the action log and the store are built from.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from arcee.core.gate import Gate, Verdict
from arcee.core.policy import Tier
from arcee.core.protocol import ExecutorResult
from arcee.tools.base import ToolContext
from arcee.tools.registry import ToolCall


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """One call as the action log stores it (spec section 12, "Action log")."""

    ts: float
    task_id: str
    args: dict[str, Any]
    tier: Tier
    approved_by: Literal["policy", "user"] | None
    result: ExecutorResult


type Recorder = Callable[[ActionRecord], Awaitable[None]]


class Executor:
    def __init__(self, gate: Gate, *, record: Recorder | None = None) -> None:
        self.gate = gate
        self._record = record

    async def execute(
        self, call: ToolCall, n: int, ctx: ToolContext, *, model_asked: bool = False
    ) -> ExecutorResult:
        started = time.time()
        verdict = await self.gate.check(call, ctx.task_id, model_asked=model_asked)
        result = await self._run(call, n, ctx, verdict)
        if self._record is not None:
            record = ActionRecord(
                ts=started,
                task_id=ctx.task_id,
                args=call.args.model_dump(mode="json"),
                tier=verdict.tier,
                approved_by=verdict.approved_by,
                result=result,
            )
            await self._record(record)
        return result

    async def _run(
        self, call: ToolCall, n: int, ctx: ToolContext, verdict: Verdict
    ) -> ExecutorResult:
        tool = call.tool
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
