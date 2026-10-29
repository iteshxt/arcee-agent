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

Tools the agent has:
{catalog}

Turn the user's request into a short plan:
- goal: the request restated precisely. interpretation: assumptions you made.
- steps: as few as possible. A simple question or one action is ONE step. A step is \
tool work (look something up, act on something); never a step only for thinking, \
comparing or choosing, which happens inside the step that needs it.
- Each step: id (1, 2, ...), do (one instruction), tools (the tool names it needs, or \
"fs.*" for a group), success (what shows the step is done).
- The last step ends with answering the user's whole request.
- constraints: limits the user stated or implied, e.g. "do not delete anything".
- clarify: a question for the user ONLY when the request cannot be understood at all; \
otherwise null. Never ask what a tool can find out.
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
            ModelRequest(system=system, messages=[Message("user", text)], schema=PlannerOutput)
        )
        return response.parsed
