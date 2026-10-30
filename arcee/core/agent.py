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

from arcee.context.examples import ExampleLibrary
from arcee.context.examples import render as render_examples
from arcee.core.context import KEEP_WHOLE, Handoff, core_view
from arcee.core.executor import Executor
from arcee.core.guards import review_reply
from arcee.core.planner import Planner
from arcee.core.policy import Tier
from arcee.core.prompts import core_system, tool_message
from arcee.core.protocol import ExecutorResult, PlannerOutput, PlanStep, ReplanContext
from arcee.core.toolsets import filters_for
from arcee.models.base import Completion, Message, ModelAdapter, ToolRequest, WireCall
from arcee.models.errors import MalformedReplyError, ModelError
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
# How much of the latest result joins the request in the example search.
EXAMPLE_QUERY_CHARS: Final = 300
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
    # Of `seconds`, the time spent waiting on rate limits before the model was asked.
    waited: float = 0.0


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
    # The tier a step starts on: 0, or higher for requests routed as hard.
    base_tier: int = 0
    strikes: int = 0
    steps: int = 0
    model_calls: int = 0
    empty_lookups: list[str] = field(default_factory=list[str])
    # Read calls since the last change: their key -> (index of the result, the result).
    seen: dict[str, tuple[int, str]] = field(default_factory=dict[str, tuple[int, str]])
    grounding_nudges: int = 0
    # Questions already put to the user in this task, with their answers.
    asked: dict[str, str] = field(default_factory=dict[str, str])
    # Set when the last result held instruction-like outside text: the next action is
    # confirmed by the user whatever its tier (spec section 12).
    confirm_next: bool = False
    # Where the turns before the first step end, and where the current step's begin.
    prelude: int = 1
    start: int = 1
    handoffs: list[Handoff] = field(default_factory=list[Handoff])


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
        examples: ExampleLibrary | None = None,
        plan_if: Callable[[str], bool] = lambda request: True,
        start_tier: Callable[[str], int] = lambda request: 0,
        on_event: Callable[[Event], None] = lambda event: None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        if not ladder:
            raise ValueError("the escalation ladder needs at least one model")
        self._ladder = list(ladder)
        self._planner = planner
        self._notes = notes
        self._examples = examples
        self._plan_if = plan_if
        self._start_tier = start_tier
        self._registry = registry
        self._executor = executor
        self._asker = asker
        self._home = home
        self._limits = limits
        self._emit = on_event
        self._clock = clock

    async def run(self, request: str, *, task_id: str = "t_1") -> Outcome:
        state = _State([Message("user", request)])
        state.base_tier = state.tier = min(self._start_tier(request), len(self._ladder) - 1)
        ctx = ToolContext(task_id=task_id, home=self._home, notes=self._notes, request=request)
        try:
            async with asyncio.timeout(self._limits.max_seconds):
                planned = self._planner is not None and self._plan_if(request)
                if planned and (failed := await self._plan(state, request)):
                    return failed
                state.prelude = state.start = len(state.messages)
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
            try:
                plan = await self._planner.plan(request, answers=answers, replan=replan)
            except ModelError as error:
                # Planning is an aid: without a plan the whole request is one step.
                self._emit(Guarded(state.steps, f"planner failed ({error}); running unplanned"))
                return None
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
                state.handoffs.append(
                    Handoff(step.id if step else 0, step.do if step else "", end.text)
                )
                state.index += 1
                upcoming = state.plan.steps[state.index]
                state.start = len(state.messages)
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
            state.start = len(state.messages)
            if failed := await self._plan(state, request, context):
                return failed
            assert state.plan is not None
            first = state.plan.steps[0]
            state.messages.append(Message("user", f"New plan. Step 1: {first.do}"))

    def _offered(self, state: _State, step: PlanStep | None) -> list[AnyTool]:
        """The step's tools, the request's tool groups and the control tools."""
        request_tools = self._registry.select(filters_for(state.messages))
        if step is None:
            return request_tools
        # A step's tools are the Planner's hint, not a limit: the groups the request
        # itself needs come too, so a narrow plan cannot leave the Core unable to act.
        chosen = self._registry.select(step.tools)
        chosen += [tool for tool in request_tools if tool not in chosen]
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
                    if (nudge := self._check_reply(state, reply, final=self._last(state))) is None:
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
                index, earlier = previous
                # Resend the result only when the Core no longer sees it whole.
                visible = _shown_whole(state, index)
                self._answer(state, call, _repeat_note(None if visible else earlier))
                self._emit(Guarded(state.steps, f"blocked a repeat of {valid.tool.name}"))
                await self._strike(state, "repeated an earlier call")
                continue
            changes_things = valid.tool.changes_things(valid.args) or (
                self._executor.gate.tier(valid, model_asked=state.confirm_next) is not Tier.AUTO
            )
            result = await self._executor.execute(
                valid, state.steps, ctx, model_asked=state.confirm_next
            )
            state.confirm_next = bool(result.suspicious)
            self._emit(ToolRan(state.steps, result))
            if result.suspicious:
                self._emit(
                    Guarded(
                        state.steps,
                        "outside content had instruction-like text; next action needs confirmation",
                    )
                )
            state.tried.append(result.summary)
            message = tool_message(result)
            self._answer(state, call, message, summary=result.summary)
            # After anything that can change the system or the page, looking again is
            # verification, not a repeat.
            if changes_things:
                state.seen.clear()
            else:
                state.seen[key] = (len(state.messages) - 1, message)
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
        tools = self._registry.select(filters_for(messages)) if offered is None else offered
        guidance = "\n\n".join(
            block for block in (plan_brief(plan, index), self._examples_for(messages)) if block
        )
        return ToolRequest(
            core_system(home=self._home, now=self._clock(), plan=guidance),
            tuple(messages),
            native_tools(tools, compact=self._ladder[0].profile.tool_detail == "compact"),
            max_tokens=MAX_TOKENS,
        )

    def _examples_for(self, messages: Sequence[Message]) -> str:
        """The worked examples closest to the request and the latest result, if any."""
        if self._examples is None or not messages:
            return ""
        latest = [m.text[:EXAMPLE_QUERY_CHARS] for m in messages[1:] if m.role != "assistant"]
        query = " ".join([messages[0].text, *latest[-1:]])
        return render_examples(self._examples.search(query))

    async def _turn(self, state: _State, offered: Sequence[AnyTool]) -> Completion:
        model = self._ladder[state.tier]
        started = time.monotonic()
        state.model_calls += 1
        view = core_view(
            state.messages,
            prelude=state.prelude,
            start=state.start,
            handoffs=state.handoffs,
            asked=state.asked,
        )
        completion = await model.respond(
            self.core_request(view, offered, plan=state.plan, index=state.index)
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
                model.last_wait_s,
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
        question = " ".join(str(args["question"]).lower().split())
        if question in state.asked:
            # Asking again gets the same answer; it is a loop, not progress.
            note = (
                f"You already asked this; the user answered {state.asked[question]!r}. "
                "Do not ask again: act on the request."
            )
            self._answer(state, call, json.dumps({"status": "error", "error": note}))
            self._emit(Guarded(state.steps, "blocked a repeated question to the user"))
            await self._strike(state, "asked the user the same question again")
            return None
        answer = await self._asker.ask(args["question"], args["options"])
        state.asked[question] = answer
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

    def _answer(self, state: _State, call: WireCall, content: str, summary: str = "") -> None:
        state.messages += (
            Message("assistant", "", tool_calls=(call,)),
            Message("tool", content, tool_name=call.name, summary=summary),
        )

    @staticmethod
    def _last(state: _State) -> bool:
        """Whether a plain reply now is the answer to the user, not a step's handoff note."""
        return state.plan is None or state.index + 1 >= len(state.plan.steps)

    def _check_reply(self, state: _State, reply: str, *, final: bool = True) -> str | None:
        """The nudge for a would-be answer that fails a guard; None when it passes."""
        review = review_reply(
            reply,
            state.messages,
            (wire_name(tool.name) for tool in self._registry),
            grounding=state.grounding_nudges < MAX_GROUNDING_NUDGES,
            narration=final,
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
        """A step went well: the next one starts on the task's base tier again."""
        state.strikes = 0
        await self._switch(state, state.base_tier)

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
            else "The next steps see only that reply, not your tool results: include every "
            "fact they need (names, subjects, dates, links)."
        )
    )
    return "\n".join(lines)


def _key(call: ToolCall) -> str:
    return f"{call.tool.name} {call.args.model_dump_json()}"


def _shown_whole(state: _State, index: int) -> bool:
    """Whether the result at `index` is still in the Core's view, not shortened."""
    later = sum(1 for m in state.messages[index + 1 :] if m.role == "tool")
    return index >= state.start and later < KEEP_WHOLE


def _repeat_note(previous: str | None) -> str:
    """The answer to an exact repeat: a pointer when the result is in view, else the result."""
    if previous is None:
        return json.dumps(
            {
                "status": "error",
                "error": "You already made this exact call and nothing has changed since; its "
                "result is above. Use it, or take a different next action.",
            }
        )
    return json.dumps(
        {
            "status": "error",
            "error": "You already made this exact call. Its result is below; use it to answer "
            "or take a different next action.",
            "previous_result": json.loads(previous),
        }
    )
