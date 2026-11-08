"""Days and weeks: what the user did with Arcee, a few sentences each (spec section 8).

Each finished task already has its summary (decision 0014). After a day is over, arceed
writes that day's in at most five sentences, from its tasks' summaries; after a week
(Monday to Sunday) is over, the week's, from its days. A day of one or two requests needs
no model: its tasks' summaries are the day's. They answer "what did I ask you
yesterday?" and "what did we do last week?" (`memory.history`), where a list of fifty
requests would not.

`span()` turns the words people use for a time ("yesterday", "last week", "on Monday",
"3 days ago", "2026-11-07") into dates in code, so the model never counts days itself.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final, Protocol

from pydantic import BaseModel, Field

from arcee.context.store import DoneRow, PeriodRow
from arcee.models.base import Message, ModelAdapter, ModelRequest

log = logging.getLogger(__name__)

# How far back arceed fills in days and weeks it has not summarised (the computer off,
# arceed not running): older ones stay as their tasks' summaries.
DAYS_BACK: Final = 14
WEEKS_BACK: Final = 8
# A day with this many requests or fewer is its requests' summaries, without a model.
PLAIN_DAY: Final = 2
# At most this many tasks go into a day's prompt (the newest kept), each line this long.
DAY_TASKS: Final = 40
LINE_CHARS: Final = 220
# gpt-oss reasons before it answers, inside this budget: at 512 Groq cut the JSON off
# ("Failed to validate JSON", or a summary ending mid-sentence).
PERIOD_MAX_TOKENS: Final = 2048

_WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS: Final = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_NUMBERS: Final = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "ten": 10,
    "fourteen": 14,
}


@dataclass(frozen=True, slots=True)
class Span:
    """Whole days, from `first` to `last` (both included), and how to say them."""

    label: str
    first: date
    last: date

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1


def midnight(day: date) -> float:
    """The local midnight starting `day`, in epoch seconds."""
    return datetime(day.year, day.month, day.day).timestamp()


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _count(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    return _NUMBERS.get(word)


def _named_date(text: str, today: date) -> date | None:
    """ "2026-11-07", "7 November", "Nov 7", "7/11" (day first, as in India): the latest
    such date not after today when the year is left out."""
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    day = month = None
    if m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?(?: of)? ([a-z]+)\.?", text):
        day, month = int(m[1]), m[2]
    elif m := re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)?", text):
        day, month = int(m[2]), m[1]
    elif m := re.fullmatch(r"(\d{1,2})/(\d{1,2})", text):
        day, month = int(m[1]), int(m[2])
    if day is None or month is None:
        return None
    if isinstance(month, str):
        if month[:3] not in _MONTHS:
            return None
        month = _MONTHS.index(month[:3]) + 1
    for year in (today.year, today.year - 1):
        try:
            found = date(year, month, day)
        except ValueError:
            return None
        if found <= today:
            return found
    return None


def span(words: str, today: date) -> Span | None:
    """The days `words` mean, seen from `today`; None when they are not a time it knows."""
    text = re.sub(r"\s+", " ", words.lower().strip().strip(".?!")).removeprefix("on ")
    text = text.removeprefix("the ")
    if text in ("today", "earlier today", "this morning", "this afternoon", "this evening"):
        return Span("today", today, today)
    if text in ("yesterday", "last night", "yesterday evening", "yesterday morning"):
        day = today - timedelta(days=1)
        return Span("yesterday", day, day)
    if text in ("day before yesterday", "the day before yesterday"):
        day = today - timedelta(days=2)
        return Span("the day before yesterday", day, day)
    if text in ("this week", "so far this week"):
        return Span("this week", monday_of(today), today)
    if text in ("last week", "previous week", "the previous week", "past week"):
        monday = monday_of(today) - timedelta(days=7)
        return Span("last week", monday, monday + timedelta(days=6))
    if text in ("this month", "so far this month"):
        return Span("this month", today.replace(day=1), today)
    if text in ("last month", "previous month"):
        last = today.replace(day=1) - timedelta(days=1)
        return Span("last month", last.replace(day=1), last)
    if m := re.fullmatch(r"(\w+) (day|week)s? ago", text):
        n = _count(m[1])
        if n is None or n < 1 or n > 365:
            return None
        if m[2] == "day":
            day = today - timedelta(days=n)
            return Span(f"{n} days ago" if n > 1 else "yesterday", day, day)
        monday = monday_of(today) - timedelta(days=7 * n)
        return Span(f"{n} weeks ago", monday, monday + timedelta(days=6))
    if m := re.fullmatch(r"(?:the )?(?:last|past) (\w+) (day|week)s?", text):
        n = _count(m[1])
        if n is None or n < 1 or n > 365:
            return None
        days = n * (7 if m[2] == "week" else 1)
        return Span(f"the last {days} days", today - timedelta(days=days - 1), today)
    weekday = text.removeprefix("last ").removeprefix("this past ")
    if weekday in _WEEKDAYS:
        back = (today.weekday() - _WEEKDAYS.index(weekday)) % 7 or 7  # never today
        day = today - timedelta(days=back)
        return Span(f"{weekday.title()} {day.isoformat()}", day, day)
    if (day := _named_date(text, today)) is not None:
        return Span(day.isoformat(), day, day)
    return None


class PeriodDigest(BaseModel):
    summary: str = Field(max_length=1500)


DAY_SYSTEM: Final = """\
You keep the memory of Arcee, an agent on the user's Ubuntu desktop. Given what the \
user asked Arcee on one day, each with what happened, write what they did that day in \
at most 5 sentences (150 words): the things they worked on, grouped, with the names, \
places, sites, files and people someone would search for later, and what was left \
unfinished or failed. Plain sentences about the user ("You asked ...", "You ..."), no \
list, nothing that is not in the requests.\
"""

WEEK_SYSTEM: Final = """\
You keep the memory of Arcee, an agent on the user's Ubuntu desktop. Given what the \
user did with Arcee on each day of one week, write the week in at most 5 sentences \
(150 words): the main things they worked on, grouped, with the names, places, sites \
and files someone would search for later, and what is still unfinished. Plain \
sentences about the user ("You ..."), no list, nothing that is not in the days.\
"""


def task_line(row: DoneRow) -> str:
    """One request as a line: its time, its words and what happened."""
    clock = datetime.fromtimestamp(row.started).strftime("%H:%M")
    what = (row.summary or "").strip()
    status = "" if row.status == "done" else f" ({row.status})"
    line = f"{clock} {row.request.strip()}{status}" + (f" → {what}" if what else "")
    return line if len(line) <= LINE_CHARS else line[: LINE_CHARS - 1] + "…"


def plain_day(rows: list[DoneRow]) -> str:
    """A day of few requests, told without a model."""
    parts: list[str] = []
    for row in rows:
        if row.summary:
            parts.append(row.summary.strip())
        else:
            status = "" if row.status == "done" else f" ({row.status})"
            parts.append(f"You asked: {row.request.strip()}{status}.")
    return " ".join(parts)


class Store(Protocol):
    async def done_between(
        self, start: float, end: float, *, exclude: str | None = None
    ) -> list[DoneRow]: ...
    async def periods_between(self, kind: str, start: float, end: float) -> list[PeriodRow]: ...
    async def save_period(self, row: PeriodRow) -> None: ...


async def _ask(model: ModelAdapter, system: str, text: str) -> str:
    response = await model.complete(
        ModelRequest(
            system=system,
            messages=[Message("user", text)],
            schema=PeriodDigest,
            max_tokens=PERIOD_MAX_TOKENS,
        )
    )
    return response.parsed.summary.strip()


async def summarise_day(
    model: ModelAdapter, store: Store, day: date, *, clock: Callable[[], float] = time.time
) -> PeriodRow | None:
    """Write `day`'s summary; None when nothing was asked that day."""
    start, end = midnight(day), midnight(day + timedelta(days=1))
    rows = await store.done_between(start, end)
    if not rows:
        return None
    if len(rows) <= PLAIN_DAY:
        text = plain_day(rows)
    else:
        shown = rows[-DAY_TASKS:]
        lines = [f"{day:%A %d %B %Y}: {len(rows)} requests"]
        if len(rows) > len(shown):
            lines.append(f"({len(rows) - len(shown)} earlier requests not shown)")
        lines += [f"- {task_line(row)}" for row in shown]
        text = await _ask(model, DAY_SYSTEM, "\n".join(lines))
    row = PeriodRow(f"day:{day.isoformat()}", "day", start, end, text, len(rows), clock())
    await store.save_period(row)
    return row


async def summarise_week(
    model: ModelAdapter, store: Store, monday: date, *, clock: Callable[[], float] = time.time
) -> PeriodRow | None:
    """Write the summary of the week starting `monday`, from its days' summaries; None
    when nothing was asked that week."""
    start, end = midnight(monday), midnight(monday + timedelta(days=7))
    days = {row.start: row for row in await store.periods_between("day", start, end)}
    lines: list[str] = []
    total = 0
    for n in range(7):
        day = monday + timedelta(days=n)
        if (found := days.get(midnight(day))) is not None:
            total += found.tasks
            lines.append(f"{day:%A %d %B} ({found.tasks} requests): {found.text}")
            continue
        rows = await store.done_between(midnight(day), midnight(day + timedelta(days=1)))
        if rows:  # a day older than DAYS_BACK, never summarised: its requests
            total += len(rows)
            lines.append(f"{day:%A %d %B} ({len(rows)} requests): {plain_day(rows[-8:])}")
    if not lines:
        return None
    text = (
        lines[0].split(": ", 1)[1]
        if len(lines) == 1
        else await _ask(model, WEEK_SYSTEM, f"The week of {monday:%d %B %Y}:\n" + "\n".join(lines))
    )
    row = PeriodRow(f"week:{monday.isoformat()}", "week", start, end, text, total, clock())
    await store.save_period(row)
    return row


async def catch_up(
    model: ModelAdapter, store: Store, today: date, *, clock: Callable[[], float] = time.time
) -> int:
    """Summarise the finished days (DAYS_BACK) and weeks (WEEKS_BACK) that have none
    yet, oldest first, so a week is written after its days. Returns how many were written."""
    written = 0
    first_day = today - timedelta(days=DAYS_BACK)
    have = {
        row.start
        for row in await store.periods_between("day", midnight(first_day), midnight(today))
    }
    for n in range(DAYS_BACK, 0, -1):
        day = today - timedelta(days=n)
        if midnight(day) in have:
            continue
        try:
            if await summarise_day(model, store, day, clock=clock) is not None:
                written += 1
        except Exception as error:  # memory is an aid: try that day again next time
            log.warning("could not summarise %s: %s", day, error)
    this_monday = monday_of(today)
    first_monday = this_monday - timedelta(days=7 * WEEKS_BACK)
    have = {
        row.start
        for row in await store.periods_between(
            "week", midnight(first_monday), midnight(this_monday)
        )
    }
    for n in range(WEEKS_BACK, 0, -1):
        monday = this_monday - timedelta(days=7 * n)
        if midnight(monday) in have:
            continue
        try:
            if await summarise_week(model, store, monday, clock=clock) is not None:
                written += 1
        except Exception as error:
            log.warning("could not summarise the week of %s: %s", monday, error)
    return written


def next_run(now: datetime) -> float:
    """Seconds from `now` until 00:05 tomorrow: the day just over gets its summary."""
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return max(60.0, (tomorrow - now).total_seconds())
