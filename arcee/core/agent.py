"""The step loop (spec section 6) for native call mode, with the code guards.

One task is one conversation: the request, then for every step the model's tool
call and the tool's result. A plain-text reply ends the task. The guards catch
the ways small models fail (decision 0003) in code, before they cost a step:

- an invalid call is answered with the registry's error as the tool result;
- an exact repeat of an earlier call is not run again: the model is pointed at
  the result it already has;
- three empty searches in a row make the agent ask the user itself;
- repeated failures climb the escalation ladder, and a successful step drops
  back to the cheapest tier;
- step and wall-time limits end anything that still loops.

The Planner is not in this loop yet: the whole request is one step (roadmap 1.15).
"""

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from arcee.core.executor import Executor
from arcee.core.policy import Tier
from arcee.core.prompts import core_system, tool_message
from arcee.core.protocol import ExecutorResult
from arcee.models.base import Completion, Message, ModelAdapter, ToolRequest, WireCall
from arcee.tools.base import ToolContext
from arcee.tools.control import ASK_USER, DONE, REPLAN
from arcee.tools.registry import AnyTool, Registry, ToolCall, ToolCallError, native_tools

# Failures in a row on one tier before the next tier takes over.
STRIKES_TO_ESCALATE: Final = 2
EMPTY_LOOKUPS_BEFORE_ASKING: Final = 3
# Room for a reasoning trace on the thinking tier as well as the answer.
MAX_TOKENS: Final = 2048
SEARCH_TOOLS: Final = frozenset({"fs.search"})
CONTROL: Final = frozenset({DONE, ASK_USER, REPLAN})


class Asker(Protocol):
    """Puts a question to the user and returns the answer."""

    async def ask(self, question: str, options: Sequence[str] = ()) -> str: ...


@dataclass(frozen=True, slots=True)
class Limits:
    max_steps: int = 25
    max_seconds: float = 600.0


DEFAULT_LIMITS: Final = Limits()


# --- Events, for the CLI now and the D-Bus signals later --------------------------


@dataclass(frozen=True, slots=True)
class ModelTurn:
    step: int
    tier: int
    model: str
    thinking: str
    call: WireCall | None
    text: str
    seconds: float


@dataclass(frozen=True, slots=True)
class ToolRan:
    step: int
    result: ExecutorResult


@dataclass(frozen=True, slots=True)
class Guarded:
    """A guard stepped in; `what` says how, in one line."""

    step: int
    what: str


@dataclass(frozen=True, slots=True)
class Escalated:
    step: int
    tier: int
    model: str
    reason: str


type Event = ModelTurn | ToolRan | Guarded | Escalated


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Literal["done", "failed"]
    reply: str
    steps: int
    model_calls: int


@dataclass(slots=True)
class _State:
    messages: list[Message]
    tier: int = 0
    strikes: int = 0
    steps: int = 0
    model_calls: int = 0
    empty_lookups: list[str] = field(default_factory=list[str])
    seen: dict[str, str] = field(default_factory=dict[str, str])


class Agent:
    def __init__(
        self,
        ladder: Sequence[ModelAdapter],
        registry: Registry,
        executor: Executor,
        asker: Asker,
        *,
        home: Path,
        limits: Limits = DEFAULT_LIMITS,
        on_event: Callable[[Event], None] = lambda event: None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        if not ladder:
            raise ValueError("the escalation ladder needs at least one model")
        self._ladder = list(ladder)
        self._registry = registry
        self._executor = executor
        self._asker = asker
        self._home = home
        self._limits = limits
        self._emit = on_event
        self._clock = clock

    async def run(self, request: str, *, task_id: str = "t_1") -> Outcome:
        state = _State([Message("user", request)])
        try:
            async with asyncio.timeout(self._limits.max_seconds):
                return await self._loop(state, ToolContext(task_id=task_id, home=self._home))
        except TimeoutError:
            minutes = self._limits.max_seconds / 60
            return self._fail(state, f"I stopped after {minutes:g} minutes without finishing.")

    async def _loop(self, state: _State, ctx: ToolContext) -> Outcome:
        offered = self._registry.select()
        while state.steps < self._limits.max_steps:
            state.steps += 1
            completion = await self._turn(state, offered)
            call = completion.tool_calls[0] if completion.tool_calls else None
            if call is None:
                if reply := completion.text.strip():
                    return Outcome("done", reply, state.steps, state.model_calls)
                self._strike(state, "the model returned nothing")
                continue
            try:
                valid = self._registry.validate(call.name, call.args, offered)
            except ToolCallError as error:
                self._answer(state, call, json.dumps({"status": "error", "error": str(error)}))
                self._strike(state, f"invalid call: {error}")
                continue
            if valid.tool.name in CONTROL:
                if (outcome := await self._control(state, call, valid)) is not None:
                    return outcome
                continue
            key = _key(valid)
            if (previous := state.seen.get(key)) is not None:
                self._answer(state, call, _repeat_note(previous))
                self._emit(Guarded(state.steps, f"blocked a repeat of {valid.tool.name}"))
                self._strike(state, "repeated an earlier call")
                continue
            changes_things = self._executor.gate.tier(valid) is not Tier.AUTO
            result = await self._executor.execute(valid, state.steps, ctx)
            self._emit(ToolRan(state.steps, result))
            message = tool_message(result)
            # After anything that can change the system, looking again is verification.
            if changes_things:
                state.seen.clear()
            else:
                state.seen[key] = message
            self._answer(state, call, message)
            if result.status == "ok":
                self._settle(state)
            else:
                self._strike(state, f"{valid.tool.name} {result.status}")
            await self._empty_lookup_guard(state, valid, result)
        return self._fail(
            state, f"I stopped after {self._limits.max_steps} steps without finishing."
        )

    def core_request(
        self, messages: Sequence[Message], offered: Sequence[AnyTool] | None = None
    ) -> ToolRequest:
        """The exact request the Core gets for `messages`; the eval suite uses it too."""
        tools = self._registry.select() if offered is None else offered
        return ToolRequest(
            core_system(home=self._home, now=self._clock()),
            tuple(messages),
            native_tools(tools),
            max_tokens=MAX_TOKENS,
        )

    async def _turn(self, state: _State, offered: Sequence[AnyTool]) -> Completion:
        model = self._ladder[state.tier]
        started = time.monotonic()
        completion = await model.respond(self.core_request(state.messages, offered))
        state.model_calls += 1
        self._emit(
            ModelTurn(
                state.steps,
                state.tier,
                model.profile.model,
                completion.thinking,
                completion.tool_calls[0] if completion.tool_calls else None,
                completion.text,
                time.monotonic() - started,
            )
        )
        return completion

    async def _control(self, state: _State, call: WireCall, valid: ToolCall) -> Outcome | None:
        """Act on a control tool; return an outcome when it ends the task."""
        args: dict[str, Any] = valid.args.model_dump()
        if valid.tool.name == DONE:
            return Outcome("done", args["reply"], state.steps, state.model_calls)
        if valid.tool.name == REPLAN:
            # No Planner yet: a stuck report ends the task with the model's reason.
            return self._fail(state, f"I could not finish: {args['reason']}")
        answer = await self._asker.ask(args["question"], args["options"])
        self._answer(state, call, json.dumps({"status": "ok", "data": {"answer": answer}}))
        self._settle(state)
        return None

    async def _empty_lookup_guard(
        self, state: _State, valid: ToolCall, result: ExecutorResult
    ) -> None:
        if valid.tool.name not in SEARCH_TOOLS:
            return
        if result.status != "ok" or not result.data or result.data.get("count"):
            state.empty_lookups.clear()
            return
        state.empty_lookups.append(str(result.data.get("query", "")))
        if len(state.empty_lookups) < EMPTY_LOOKUPS_BEFORE_ASKING:
            return
        tried = ", ".join(repr(q) for q in state.empty_lookups)
        question = f"I searched for {tried} and found nothing. Where is it, or what is it called?"
        self._emit(Guarded(state.steps, "asked the user after 3 empty searches"))
        answer = await self._asker.ask(question)
        state.messages.append(Message("user", f"(You asked: {question})\n{answer}"))
        state.empty_lookups.clear()

    def _answer(self, state: _State, call: WireCall, content: str) -> None:
        state.messages += (
            Message("assistant", "", tool_calls=(call,)),
            Message("tool", content, tool_name=call.name),
        )

    def _strike(self, state: _State, reason: str) -> None:
        state.strikes += 1
        if state.strikes < STRIKES_TO_ESCALATE or state.tier + 1 >= len(self._ladder):
            return
        state.tier += 1
        state.strikes = 0
        self._emit(
            Escalated(state.steps, state.tier, self._ladder[state.tier].profile.model, reason)
        )

    def _settle(self, state: _State) -> None:
        """A step went well: the next one starts on the cheapest tier again."""
        state.strikes = 0
        state.tier = 0

    def _fail(self, state: _State, reply: str) -> Outcome:
        return Outcome("failed", reply, state.steps, state.model_calls)


def _key(call: ToolCall) -> str:
    return f"{call.tool.name} {call.args.model_dump_json()}"


def _repeat_note(previous: str) -> str:
    return json.dumps(
        {
            "status": "error",
            "error": "You already made this exact call. Its result is below; use it to answer "
            "or take a different next action.",
            "previous_result": json.loads(previous),
        }
    )
