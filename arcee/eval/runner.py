"""Runs eval cases through the exact Core request the agent sends (`Agent.core_request`).

Each case is replayed as a conversation: the request, then every earlier call
as an assistant tool call with its result as a `tool` message, rendered by the
same code the live loop uses. One model turn is taken and judged.
"""

import json
import statistics
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from arcee.context.examples import ExampleLibrary
from arcee.core.agent import Agent
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.guards import review_reply
from arcee.core.policy import Policy
from arcee.core.prompts import tool_message
from arcee.core.protocol import ConfirmRequest, ExecutorResult
from arcee.eval.cases import Case, Decision, PriorUser, check
from arcee.models.base import Completion, Message, ModelAdapter, WireCall
from arcee.models.errors import MalformedReplyError
from arcee.tools.control import ASK_USER, DONE
from arcee.tools.registry import Registry, ToolCallError, wire_name

# Fixed so prompts, and therefore results, do not drift with the date.
EVAL_HOME: Final = Path("/home/user")
EVAL_NOW: Final = datetime(2026, 10, 29, 10, 0)


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: Case
    decision: Decision
    problems: tuple[str, ...]
    seconds: float
    # Set when the reply guards sent the first answer back, as the live loop would.
    guarded: str = ""

    @property
    def passed(self) -> bool:
        return not self.problems


class _NoUser:
    """Eval cases take one decision; nothing is executed and nobody is asked."""

    async def confirm(self, request: ConfirmRequest) -> bool:
        return False

    async def ask(self, question: str, options: Sequence[str] = ()) -> str:
        return ""


def conversation(case: Case) -> list[Message]:
    messages = [Message("user", case.request)]
    for n, prior in enumerate(case.history, start=1):
        if isinstance(prior, PriorUser):
            messages.append(Message("user", prior.text))
            continue
        call = WireCall(wire_name(prior.tool), dict(prior.args))
        result = ExecutorResult(
            n=n,
            tool=prior.tool,
            status=prior.status,
            summary="",
            data=dict(prior.data) if prior.data is not None else None,
            error=prior.error,
            duration_ms=0,
        )
        messages += (
            Message("assistant", "", tool_calls=(call,)),
            Message("tool", tool_message(result), tool_name=call.name),
        )
    return messages


def decide(completion: Completion, registry: Registry) -> Decision:
    if not completion.tool_calls:
        text = completion.text.strip()
        return Decision("reply", text=text) if text else Decision("invalid", error="empty reply")
    wire = completion.tool_calls[0]
    try:
        call = registry.validate(wire.name, wire.args)
    except ToolCallError as error:
        return Decision("invalid", tool=wire.name, error=str(error))
    args = call.args.model_dump()
    if call.tool.name == DONE:
        return Decision("reply", tool=DONE, args=args, text=args["reply"])
    if call.tool.name == ASK_USER:
        return Decision("ask", tool=ASK_USER, args=args, text=args["question"])
    return Decision("tool", tool=call.tool.name, args=args)


async def run_cases(
    model: ModelAdapter,
    registry: Registry,
    cases: Sequence[Case],
    *,
    repeat: int = 1,
    guards: bool = True,
    examples: ExampleLibrary | None = None,
    on_result: Callable[[CaseResult], None] = lambda result: None,
) -> list[CaseResult]:
    """Judge one decision per case.

    With `guards`, a final answer that fails the live loop's reply review gets
    the same nudge and one more turn, and the second decision is judged: the
    suite then measures the harness. Without, it measures the raw model.
    """
    user = _NoUser()
    agent = Agent(
        [model],
        registry,
        Executor(Gate(Policy(), user)),
        user,
        home=EVAL_HOME,
        examples=examples,
        clock=lambda: EVAL_NOW,
    )
    results: list[CaseResult] = []
    for _ in range(repeat):
        for case in cases:
            started = time.monotonic()
            messages = conversation(case)
            decision, completion = await _decide(model, agent, registry, messages)
            guarded = ""
            if guards and decision.kind == "reply" and completion is not None:
                names = [wire_name(tool.name) for tool in registry]
                if (review := review_reply(decision.text, messages, names)) is not None:
                    guarded = review.detail
                    messages += _sent_back(completion, review.nudge)
                    decision, _ = await _decide(model, agent, registry, messages)
            result = CaseResult(
                case,
                decision,
                tuple(check(case.expect, decision)),
                time.monotonic() - started,
                guarded,
            )
            results.append(result)
            on_result(result)
    return results


async def _decide(
    model: ModelAdapter, agent: Agent, registry: Registry, messages: list[Message]
) -> tuple[Decision, Completion | None]:
    try:
        completion = await model.respond(agent.core_request(messages))
    except MalformedReplyError as error:
        return Decision("invalid", error=f"malformed reply: {error}"), None
    return decide(completion, registry), completion


def _sent_back(completion: Completion, nudge: str) -> list[Message]:
    """How the live loop returns a rejected answer: as text, or as control_done's result."""
    if completion.tool_calls:
        call = completion.tool_calls[0]
        return [
            Message("assistant", "", tool_calls=(call,)),
            Message("tool", json.dumps({"status": "error", "error": nudge}), tool_name=call.name),
        ]
    return [Message("assistant", completion.text), Message("user", nudge)]


def summary(results: Sequence[CaseResult]) -> str:
    """Pass rate per scenario and overall, valid-action rate and latency."""
    if not results:
        return "no cases run"
    by_scenario: dict[str, list[CaseResult]] = defaultdict(list)
    for result in results:
        by_scenario[result.case.scenario].append(result)
    width = max(len(name) for name in by_scenario)
    lines = [f"{name:<{width}}  {_rate(group)}" for name, group in sorted(by_scenario.items())]
    valid = sum(r.decision.kind != "invalid" for r in results)
    guarded = sum(bool(r.guarded) for r in results)
    times = sorted(r.seconds for r in results)
    lines += [
        "",
        f"{'overall':<{width}}  {_rate(results)}",
        f"valid reply: {valid}/{len(results)} ({valid / len(results):.0%})",
        f"answers sent back by the reply guards: {guarded}",
        f"latency: median {statistics.median(times):.2f}s, "
        f"p90 {times[int(0.9 * (len(times) - 1))]:.2f}s, max {times[-1]:.2f}s",
    ]
    return "\n".join(lines)


def _rate(results: Sequence[CaseResult]) -> str:
    passed = sum(r.passed for r in results)
    return f"{passed:>3}/{len(results):<3} {passed / len(results):>4.0%}"
