"""The Core's own reach into memory (spec section 11): look something up, forget a fact,
and what was done on a day or in a week.

Recall already puts what matters into every request (decision 0014); these are for
when the user asks about it ("what do you know about me?", "forget my old address",
"what did I ask you yesterday?"). Saving stays `control.note`.
"""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, ClassVar, Final, Protocol

from pydantic import Field

from arcee.context.periods import midnight, span, task_line
from arcee.context.store import DoneRow, PeriodRow
from arcee.core.policy import Policy, Tier
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage

# memory.history: at most this many requests listed for a day, and this much in all.
DAY_LINES: Final = 30
DAY_CHARS: Final = 700
HISTORY_CHARS: Final = 4000


class Memory(Protocol):
    """The store, as these tools see it."""

    async def recall_items(
        self, query: str, exclude_task: str | None
    ) -> list[tuple[str, str, str]]: ...  # (id, text, date)
    async def forget_memory(self, memory_id: str) -> bool: ...
    async def done_between(
        self, start: float, end: float, *, exclude: str | None = None
    ) -> list[DoneRow]: ...
    async def periods_between(self, kind: str, start: float, end: float) -> list[PeriodRow]: ...


class RecallArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=300, description="What to look up, in words")


class MemoryRecall(Tool[RecallArgs]):
    name = "memory.recall"
    description = "Look up what Arcee remembers: facts about the user and past tasks."
    usage = Usage(
        when='The user asks what you know or remember ("what is my home station?", '
        '"what did we do about my visa?"), and the remembered facts shown do not answer it.',
        avoid="Facts about the world: those are knowledge or web.search.",
        example='memory.recall(query="home train station")',
    )
    args_model = RecallArgs
    read_only: ClassVar[bool] = True

    def __init__(self, memory: Memory, seen: dict[str, str]) -> None:
        self._memory = memory
        self._seen = seen

    async def run(self, args: RecallArgs, ctx: ToolContext) -> ToolResult:
        items = await self._memory.recall_items(args.query, ctx.task_id)
        if not items:
            return ToolResult.ok("Nothing remembered about that", data={"memory": []})
        self._seen.update({item_id: text for item_id, text, _ in items})
        lines = [f"{item_id}: {text} ({date})" for item_id, text, date in items]
        return ToolResult.ok(f"Remembered {len(items)} things", data={"memory": lines})


class ForgetArgs(ToolArgs):
    id: str = Field(pattern=r"^m_[0-9a-f]+$", description="The m_... id of the fact")


class MemoryForget(Tool[ForgetArgs]):
    name = "memory.forget"
    description = "Forget one remembered fact about the user, by its m_... id."
    usage = Usage(
        when='The user asks to forget something ("forget my old address"). Find its id '
        "with memory.recall first.",
        example='memory.forget(id="m_3fa1c2d0")',
    )
    args_model = ForgetArgs

    def __init__(self, memory: Memory, seen: dict[str, str]) -> None:
        self._memory = memory
        self._seen = seen  # the words of facts memory.recall showed, for the question

    def tier_for(self, args: ForgetArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM  # forgetting cannot be undone

    def sentence(self, args: ForgetArgs) -> str:
        return f"Forget “{self._seen.get(args.id, args.id)}”?"

    async def run(self, args: ForgetArgs, ctx: ToolContext) -> ToolResult:
        if not await self._memory.forget_memory(args.id):
            return ToolResult.fail(f"no remembered fact has the id {args.id}")
        text = self._seen.pop(args.id, args.id)
        data: dict[str, Any] = {"forgot": text}
        return ToolResult.ok(f"Forgot: {text}", data=data)


class HistoryArgs(ToolArgs):
    when: str = Field(
        min_length=1,
        max_length=60,
        description='The day or days, in the user\'s words: "today", "yesterday", "last '
        'week", "this week", "Monday", "3 days ago", "the last 7 days", "2026-11-07", '
        '"7 November".',
    )


class MemoryHistory(Tool[HistoryArgs]):
    name = "memory.history"
    description = "What the user asked Arcee, and what happened, on a day or in a week."
    usage = Usage(
        when='"What did I ask you yesterday?", "what did we do last week?", "what was I '
        'working on on Monday?": pass the time in their words; the dates are worked out '
        "for you.",
        avoid="Looking for one thing by its subject (memory.recall), or files changed "
        "recently (fs tools).",
        example='memory.history(when="yesterday")',
    )
    args_model = HistoryArgs
    read_only: ClassVar[bool] = True

    def __init__(self, memory: Memory, today: Callable[[], date] = date.today) -> None:
        self._memory = memory
        self._today = today

    async def run(self, args: HistoryArgs, ctx: ToolContext) -> ToolResult:
        days = span(args.when, self._today())
        if days is None:
            return ToolResult.fail(
                f"“{args.when}” is not a time memory.history knows: say today, yesterday, "
                "this week, last week, a weekday, N days ago, the last N days, or a date "
                "(2026-11-07)"
            )
        start, end = midnight(days.first), midnight(days.last + timedelta(days=1))
        rows = await self._memory.done_between(start, end, exclude=ctx.task_id)
        data: dict[str, Any] = {
            "when": days.label,
            "from": days.first.isoformat(),
            "to": days.last.isoformat(),
            "requests": len(rows),
        }
        if not rows:
            return ToolResult.ok(f"Nothing was asked {days.label}", data=data)
        summaries = {
            row.start: row.text for row in await self._memory.periods_between("day", start, end)
        }
        if days.days == 1:
            if text := summaries.get(start):
                data["summary"] = text
            lines = [task_line(row) for row in rows]
            if len(lines) > DAY_LINES:
                data["earlier_not_listed"] = len(lines) - DAY_LINES
            data["asked"] = lines[-DAY_LINES:]
        else:
            weeks = [
                f"week of {w.id.split(':', 1)[1]}: {w.text}"
                for w in await self._memory.periods_between("week", start, end)
                if w.end <= end
            ]
            if weeks:
                data["weeks"] = weeks
            data["days"] = _by_day(rows, summaries)
        return ToolResult.ok(f"{len(rows)} requests {days.label}", data=data)


def _by_day(rows: list[DoneRow], summaries: dict[float, str]) -> list[str]:
    """One line a day: its summary, or its requests; the newest days kept within
    HISTORY_CHARS."""
    grouped: dict[date, list[DoneRow]] = {}
    for row in rows:
        grouped.setdefault(date.fromtimestamp(row.started), []).append(row)
    lines: list[str] = []
    for day, done in grouped.items():
        text = summaries.get(midnight(day)) or "; ".join(task_line(row) for row in done)
        if len(text) > DAY_CHARS:
            text = text[: DAY_CHARS - 1] + "…"
        lines.append(f"{day:%a %Y-%m-%d} ({len(done)} requests): {text}")
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > HISTORY_CHARS:
            break
        kept.insert(0, line)
        used += len(line)
    return kept


def memory_tools(memory: Memory) -> list[Tool[Any]]:
    seen: dict[str, str] = {}
    return [MemoryRecall(memory, seen), MemoryForget(memory, seen), MemoryHistory(memory)]
