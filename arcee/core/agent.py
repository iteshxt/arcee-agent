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

With a Planner, the task runs plan step by plan step: the plan sits in the
system prompt, each step offers only its tools (plus the control tools), a
plain-text reply ends the step, and the last step's reply is the answer. A
stuck report asks the Planner for a new plan, at most `MAX_REPLANS` times.
Without a Planner the whole request is one step with every tool.
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
from arcee.core.guards import review_reply
from arcee.core.planner import Planner
from arcee.core.policy import Tier
from arcee.core.prompts import core_system, tool_message
from arcee.core.protocol import ExecutorResult, PlannerOutput, PlanStep, ReplanContext
from arcee.models.base import Completion, Message, ModelAdapter, ToolRequest, WireCall
from arcee.models.errors import MalformedReplyError
from arcee.tools.base import Notes, ToolContext
from arcee.tools.control import ASK_USER, DONE, REPLAN
from arcee.tools.registry import (
    AnyTool,
    Registry,
    ToolCall,
    ToolCallError,
    native_tools,
    wire_name,
)

# Failures in a row on one tier before the next tier takes over.
STRIKES_TO_ESCALATE: Final = 2
EMPTY_LOOKUPS_BEFORE_ASKING: Final = 3
# Room for a reasoning trace on the thinking tier as well as the answer.
MAX_TOKENS: Final = 2048
SEARCH_TOOLS: Final = frozenset({"fs.search"})
CONTROL: Final = frozenset({DONE, ASK_USER, REPLAN})
MAX_REPLANS: Final = 2
# After this many, an answer is accepted even if a fact in it was not seen: the user
# gets a reply rather than a loop.
MAX_GROUNDING_NUDGES: Final = 2
BROKEN_CALL_NUDGE: Final = (
    "Your last reply was a broken tool call. Reply again with one valid tool call, "
    "or answer in plain text."
)
MAX_CLARIFY: Final = 2


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


@dataclass(frozen=True, slots=True)
class Planned:
    plan: PlannerOutput
    seconds: float
    attempt: int  # 0 for the first plan, n for the n-th replan


@dataclass(frozen=True, slots=True)
class StepStarted:
    step: int
    total: int
    do: str


type Event = ModelTurn | ToolRan | Guarded | Escalated | Planned | StepStarted


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Literal["done", "failed"]
    reply: str
    steps: int
    model_calls: int


@dataclass(frozen=True, slots=True)
class _StepDone:
    text: str


@dataclass(frozen=True, slots=True)
class _Stuck:
    reason: str


@dataclass(slots=True)
class _State:
    messages: list[Message]
    plan: PlannerOutput | None = None
    index: int = 0
    tried: list[str] = field(default_factory=list[str])
    tier: int = 0
    strikes: int = 0
    steps: int = 0
    model_calls: int = 0
    empty_lookups: list[str] = field(default_factory=list[str])
    seen: dict[str, str] = field(default_factory=dict[str, str])
    grounding_nudges: int = 0


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
        planner: Planner | None = None,
        notes: Notes | None = None,
        plan_if: Callable[[str], bool] = lambda request: True,
        on_event: Callable[[Event], None] = lambda event: None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        if not ladder:
            raise ValueError("the escalation ladder needs at least one model")
        self._ladder = list(ladder)
        self._planner = planner
        self._notes = notes
        self._plan_if = plan_if
        self._registry = registry
        self._executor = executor
        self._asker = asker
        self._home = home
        self._limits = limits
        self._emit = on_event
        self._clock = clock

    async def run(self, request: str, *, task_id: str = "t_1") -> Outcome:
        state = _State([Message("user", request)])
        ctx = ToolContext(task_id=task_id, home=self._home, notes=self._notes)
        try:
            async with asyncio.timeout(self._limits.max_seconds):
                planned = self._planner is not None and self._plan_if(request)
                if planned and (failed := await self._plan(state, request)):
                    return failed
                return await self._run_plan(state, ctx, request)
        except TimeoutError:
            minutes = self._limits.max_seconds / 60
            return self._fail(state, f"I stopped after {minutes:g} minutes without finishing.")

    async def _plan(
        self, state: _State, request: str, replan: ReplanContext | None = None
    ) -> Outcome | None:
        """Get a plan, asking the user when the Planner needs clarification."""
        assert self._planner is not None
        answers: list[tuple[str, str]] = []
        while True:
            started = time.monotonic()
            plan = await self._planner.plan(request, answers=answers, replan=replan)
            state.model_calls += 1
            if plan.clarify is None:
                state.plan, state.index = plan, 0
                attempt = replan.replan_count if replan else 0
                self._emit(Planned(plan, time.monotonic() - started, attempt))
                return None
            if len(answers) >= MAX_CLARIFY:
                return self._fail(state, plan.clarify)
            answer = await self._asker.ask(plan.clarify)
            answers.append((plan.clarify, answer))
            state.messages.append(Message("user", f"(You asked: {plan.clarify})\n{answer}"))

    async def _run_plan(self, state: _State, ctx: ToolContext, request: str) -> Outcome:
        replans = 0
        while True:
            step = state.plan.steps[state.index] if state.plan else None
            if state.plan and step:
                self._emit(StepStarted(step.id, len(state.plan.steps), step.do))
            end = await self._run_step(state, ctx, step)
            if isinstance(end, Outcome):
                return end
            if isinstance(end, _StepDone):
                if state.plan is None or state.index + 1 >= len(state.plan.steps):
                    return Outcome("done", end.text, state.steps, state.model_calls)
                state.messages.append(Message("assistant", end.text))
                state.index += 1
                upcoming = state.plan.steps[state.index]
                state.messages.append(Message("user", f"Now step {upcoming.id}: {upcoming.do}"))
                continue
            if self._planner is None or replans >= MAX_REPLANS:
                return self._fail(state, f"I could not finish: {end.reason}")
            replans += 1
            context = ReplanContext(
                failed_step=step.id if step else 1,
                history_summary=end.reason,
                tried=tuple(state.tried[-6:]),
                replan_count=replans,
            )
            if failed := await self._plan(state, request, context):
                return failed
            assert state.plan is not None
            first = state.plan.steps[0]
            state.messages.append(Message("user", f"New plan. Step 1: {first.do}"))

    def _offered(self, state: _State, step: PlanStep | None) -> list[AnyTool]:
        """The step's tools plus the control tools; everything when the filter matches nothing."""
        if step is None:
            return self._registry.select()
        chosen = self._registry.select(step.tools)
        if not chosen:
            self._emit(Guarded(state.steps, "plan step named no known tools; offering all"))
            return self._registry.select()
        return [*chosen, *(t for t in self._registry.select(sorted(CONTROL)) if t not in chosen)]

    async def _run_step(
        self, state: _State, ctx: ToolContext, step: PlanStep | None
    ) -> Outcome | _StepDone | _Stuck:
        offered = self._offered(state, step)
        while state.steps < self._limits.max_steps:
            state.steps += 1
            try:
                completion = await self._turn(state, offered)
            except MalformedReplyError as error:
                self._emit(Guarded(state.steps, "the model wrote a broken tool call; asking again"))
                state.messages.append(Message("user", BROKEN_CALL_NUDGE))
                await self._strike(state, str(error))
                continue
            call = completion.tool_calls[0] if completion.tool_calls else None
            if call is None:
                if reply := completion.text.strip():
                    if (nudge := self._check_reply(state, reply)) is None:
                        return _StepDone(reply)
                    state.messages += (Message("assistant", reply), Message("user", nudge))
                    await self._strike(state, "the reply failed a check")
                    continue
                await self._strike(state, "the model returned nothing")
                continue
            try:
                valid = self._registry.validate(call.name, call.args, offered)
            except ToolCallError as error:
                self._answer(state, call, json.dumps({"status": "error", "error": str(error)}))
                await self._strike(state, f"invalid call: {error}")
                continue
            if valid.tool.name in CONTROL:
                if (end := await self._control(state, call, valid)) is not None:
                    return end
                continue
            key = _key(valid)
            if (previous := state.seen.get(key)) is not None:
                self._answer(state, call, _repeat_note(previous))
                self._emit(Guarded(state.steps, f"blocked a repeat of {valid.tool.name}"))
                await self._strike(state, "repeated an earlier call")
                continue
            changes_things = self._executor.gate.tier(valid) is not Tier.AUTO
            result = await self._executor.execute(valid, state.steps, ctx)
            self._emit(ToolRan(state.steps, result))
            state.tried.append(result.summary)
            message = tool_message(result)
            # After anything that can change the system, looking again is verification.
            if changes_things:
                state.seen.clear()
            else:
                state.seen[key] = message
            self._answer(state, call, message)
            if result.status == "ok":
                await self._settle(state)
            else:
                await self._strike(state, f"{valid.tool.name} {result.status}")
            await self._empty_lookup_guard(state, valid, result)
        return self._fail(
            state, f"I stopped after {self._limits.max_steps} steps without finishing."
        )

    def core_request(
        self,
        messages: Sequence[Message],
        offered: Sequence[AnyTool] | None = None,
        *,
        plan: PlannerOutput | None = None,
        index: int = 0,
    ) -> ToolRequest:
        """The exact request the Core gets for `messages`; the eval suite uses it too."""
        tools = self._registry.select() if offered is None else offered
        return ToolRequest(
            core_system(home=self._home, now=self._clock(), plan=plan_brief(plan, index)),
            tuple(messages),
            native_tools(tools),
            max_tokens=MAX_TOKENS,
        )

    async def _turn(self, state: _State, offered: Sequence[AnyTool]) -> Completion:
        model = self._ladder[state.tier]
        started = time.monotonic()
        state.model_calls += 1
        completion = await model.respond(
            self.core_request(state.messages, offered, plan=state.plan, index=state.index)
        )
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

    async def _control(
        self, state: _State, call: WireCall, valid: ToolCall
    ) -> Outcome | _Stuck | None:
        """Act on a control tool; return how it ends the step, if it does."""
        args: dict[str, Any] = valid.args.model_dump()
        if valid.tool.name == DONE:
            if (nudge := self._check_reply(state, args["reply"])) is None:
                return Outcome("done", args["reply"], state.steps, state.model_calls)
            self._answer(state, call, json.dumps({"status": "error", "error": nudge}))
            await self._strike(state, "the reply failed a check")
            return None
        if valid.tool.name == REPLAN:
            return _Stuck(args["reason"])
        answer = await self._asker.ask(args["question"], args["options"])
        self._answer(state, call, json.dumps({"status": "ok", "data": {"answer": answer}}))
        await self._settle(state)
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

    def _check_reply(self, state: _State, reply: str) -> str | None:
        """The nudge for a would-be answer that fails a guard; None when it passes."""
        review = review_reply(
            reply,
            state.messages,
            (wire_name(tool.name) for tool in self._registry),
            grounding=state.grounding_nudges < MAX_GROUNDING_NUDGES,
        )
        if review is None:
            return None
        if review.kind == "grounding":
            state.grounding_nudges += 1
        self._emit(Guarded(state.steps, f"{review.detail}; nudged"))
        return review.nudge

    async def _strike(self, state: _State, reason: str) -> None:
        state.strikes += 1
        if state.strikes < STRIKES_TO_ESCALATE or state.tier + 1 >= len(self._ladder):
            return
        await self._switch(state, state.tier + 1)
        state.strikes = 0
        self._emit(
            Escalated(state.steps, state.tier, self._ladder[state.tier].profile.model, reason)
        )

    async def _settle(self, state: _State) -> None:
        """A step went well: the next one starts on the cheapest tier again."""
        state.strikes = 0
        await self._switch(state, 0)

    async def _switch(self, state: _State, tier: int) -> None:
        """Move to `tier`, unloading the current model when the new tier is another model.

        On 4 GB of VRAM two models rarely fit next to the desktop, so the old one
        is unloaded at once instead of waiting for Ollama to evict it.
        """
        current = self._ladder[state.tier]
        if tier != state.tier and current.profile.model != self._ladder[tier].profile.model:
            await current.unload()
        state.tier = tier

    def _fail(self, state: _State, reply: str) -> Outcome:
        return Outcome("failed", reply, state.steps, state.model_calls)


def plan_brief(plan: PlannerOutput | None, index: int) -> str:
    """The plan as the Core sees it; empty for a one-step plan without constraints."""
    if plan is None or (len(plan.steps) == 1 and not plan.constraints):
        return ""
    lines = ["Plan:"]
    for i, step in enumerate(plan.steps):
        mark = "done" if i < index else "current" if i == index else "next"
        lines.append(f"{step.id}. [{mark}] {step.do}")
    current = plan.steps[index]
    lines.append(f"Current step: {current.do} Done when: {current.success}")
    if plan.constraints:
        lines.append("Constraints: " + "; ".join(plan.constraints))
    last = index + 1 >= len(plan.steps)
    lines.append(
        "When the current step is done, reply in plain text with what you found or did. "
        + (
            "That reply is your answer to the user: answer their whole request, using "
            "what the earlier steps found."
            if last
            else "The next step follows."
        )
    )
    return "\n".join(lines)


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
