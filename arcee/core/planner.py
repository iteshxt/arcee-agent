"""The Planner (spec sections 4 and 7.1): one call that turns a request into steps.

It runs in schema call mode: the reply is a `PlannerOutput` JSON object that
Ollama's `format` grammar keeps well-formed and the adapter validates and
retries. The Planner sees tool names and one-line descriptions only; the usage
cards are for the Core, which picks the actual calls.
"""

import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from arcee.core.protocol import PlannerOutput, ReplanContext
from arcee.models.base import Message, ModelAdapter, ModelRequest
from arcee.tools.registry import AnyTool, Registry

# Words that signal another phase of work.
_MULTI_STEP: Final = re.compile(
    r"\b(then|after that|afterwards|and also|as well as|followed by|finally|first|next)\b|;",
    re.IGNORECASE,
)
LONG_REQUEST_WORDS: Final = 30
# gpt-oss reasons before it writes the plan, and both count as output: a five-step plan
# took 1,013 tokens, and at the old 1,024 longer plans were cut off and lost their steps.
PLANNER_MAX_TOKENS: Final = 4096


def needs_plan(request: str) -> bool:
    """Whether a request is long enough to be worth a plan.

    Measured on the reference machine with LFM3 1.7B, a two-part request ("find
    the biggest file, then tell me the free space") took 20 s and 3 model calls
    without a plan and 98-137 s and 8-9 calls with one, because the small Planner
    splits work into too many steps. So only long or clearly multi-phase requests
    are planned; `--plan always` and `--plan never` override this.
    """
    sentences = [s for s in re.split(r"[.?!]+", request) if len(s.split()) >= 3]
    return (
        len(request.split()) >= LONG_REQUEST_WORDS
        or len(sentences) >= 3
        or len(_MULTI_STEP.findall(request)) >= 2
    )


def planner_system(tools: Sequence[AnyTool], *, home: Path, now: datetime) -> str:
    catalog = "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)
    return f"""\
You plan tasks for Arcee, an agent on the user's Ubuntu Linux desktop.
Now: {now:%A %Y-%m-%d %H:%M}. Home folder: {home} (written ~).
You act on this computer and in the user's own Google Chrome, which is already signed \
in to their accounts (Gmail, Google Calendar and others): use the browser tools for \
those. Never ask for passwords, API keys or credentials.

Tools the agent has:
{catalog}

Turn the user's request into a short plan:
- goal: the request restated precisely. interpretation: assumptions you made.
- steps: as few as possible, but the plan covers the WHOLE request to the end. A simple \
question or one action is ONE step. Each phase of a longer request is its own step: \
finding items, reading them, and acting somewhere else (another site, app or folder) \
are separate steps, because each step sees only the notes the earlier ones left. A \
step is tool work (look something up, act on something); never a step only for \
thinking, comparing or choosing, which happens inside the step that needs it.
- Each step: id (1, 2, ...), do (one instruction), tools (the tool names it needs, or \
"fs.*" for a group), success (what shows the step is done), judgement (false only for \
mechanical steps such as opening a page or typing given text: a smaller model runs them).
- The last step ends with answering the user's whole request.
- constraints: limits the user stated or implied, e.g. "do not delete anything".
- clarify: almost always null. A question for the user ONLY when the request cannot be \
understood at all. Never ask what a tool can find out. Conditions in the request ("if \
it needs my attention", "the important ones") are judgements the agent makes while \
doing the work: plan steps that open and read each item and decide for each one, \
and put your reading of the condition in interpretation.
- Never plan an action the tools cannot do; say so in a one-step plan instead."""


class Planner:
    def __init__(
        self,
        model: ModelAdapter,
        registry: Registry,
        *,
        home: Path,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        if model.profile.call_mode != "schema":
            raise ValueError("the Planner needs a model profile in schema call mode")
        self.model = model
        self._registry = registry
        self._home = home
        self._clock = clock

    async def plan(
        self,
        request: str,
        *,
        answers: Sequence[tuple[str, str]] = (),
        replan: ReplanContext | None = None,
    ) -> PlannerOutput:
        """Plan `request`. `answers` are earlier clarify questions with the user's replies."""
        text = f"Request: {request}"
        for question, answer in answers:
            text += f"\nYou asked: {question}\nThe user answered: {answer}"
        if replan is not None:
            tried = "; ".join(replan.tried) or "nothing recorded"
            text += (
                f"\n\nThe previous plan failed at step {replan.failed_step} "
                f"(attempt {replan.replan_count}). What happened: {replan.history_summary}. "
                f"Already tried: {tried}. Make a different plan."
            )
        system = planner_system(list(self._registry), home=self._home, now=self._clock())
        response = await self.model.complete(
            ModelRequest(
                system=system,
                messages=[Message("user", text)],
                schema=PlannerOutput,
                max_tokens=PLANNER_MAX_TOKENS,
            )
        )
        return response.parsed
