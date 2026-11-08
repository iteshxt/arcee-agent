"""Timers and reminders (decision 0018): `reminder.add`, `reminder.list`, `reminder.cancel`.

Kept in History and fired by arceed's scheduler (arcee/reminders.py) as a GNOME
notification with a sound, which stays until dismissed and offers a snooze. A timer is a
reminder set with a duration ("in 10 minutes"); one at a clock time is set with `at`.
"""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, ClassVar, Final, Literal, Protocol

from pydantic import Field, model_validator

from arcee.context.store import ReminderRow
from arcee.core.policy import Tier
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage

LONGEST: Final = timedelta(days=366)
_CLOCK: Final = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)


class Reminders(Protocol):
    """The store, as these tools see it."""

    async def add_reminder(
        self,
        text: str,
        due: float,
        *,
        kind: str = "reminder",
        task_id: str | None = None,
        repeat: str = "",
        detail: str = "",
    ) -> str: ...
    async def pending_reminders(self) -> list[ReminderRow]: ...
    async def reminder(self, reminder_id: str) -> ReminderRow | None: ...
    async def set_reminder(
        self, reminder_id: str, *, status: str, due: float | None = None
    ) -> bool: ...


def when_at(at: str, now: datetime) -> datetime:
    """`at` as a local time: "2026-11-08T17:00", "2026-11-08 17:00", or a clock time
    ("17:00", "5 pm", "5:30pm"), today or, once past, tomorrow."""
    text = at.strip()
    if match := _CLOCK.match(text):
        hour, minute = int(match[1]), int(match[2] or 0)
        if suffix := (match[3] or "").lower():
            if not 1 <= hour <= 12:
                raise ValueError(f"{at!r} is not a time")
            hour = hour % 12 + (12 if suffix == "pm" else 0)
        if hour > 23 or minute > 59:
            raise ValueError(f"{at!r} is not a time")
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return due if due > now else due + timedelta(days=1)
    try:
        due = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError as exc:
        raise ValueError(f"{at!r} is not a date and time (e.g. 2026-11-08T17:00)") from exc
    return due.astimezone() if due.tzinfo is None else due


def describe(due: datetime, now: datetime) -> str:
    """'17:00 today', '07:00 tomorrow', 'Mon 10 Nov 09:00'."""
    clock = due.strftime("%H:%M")
    days = (due.date() - now.date()).days
    if days == 0:
        return f"{clock} today"
    if days == 1:
        return f"{clock} tomorrow"
    return f"{due:%a} {due.day} {due:%b} {clock}"


def _left(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return f"{max(1, round(seconds))} s"
    if minutes < 90:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


class AddArgs(ToolArgs):
    text: str = Field(
        min_length=1,
        max_length=200,
        description='What to remind the user of, in their words ("call mom"); "Timer" '
        "for a plain timer",
    )
    in_minutes: float | None = Field(
        default=None, gt=0, description="For a timer or 'in 20 minutes': minutes from now"
    )
    at: str | None = Field(
        default=None,
        description="For a clock time: local date and time, 2026-11-08T17:00 (worked out "
        "from Now), or a time today such as 17:00",
    )
    alarm: bool = Field(
        default=False, description='An alarm: it rings until dismissed ("wake me at 7")'
    )
    repeat: Literal["", "daily", "weekdays", "weekends", "weekly"] = Field(
        default="", description="Comes again: daily, weekdays, weekends or weekly (with at)"
    )

    @model_validator(mode="after")
    def _one_time(self) -> AddArgs:
        if (self.in_minutes is None) == (self.at is None):
            raise ValueError("give either in_minutes or at")
        if self.repeat and self.at is None:
            raise ValueError("a repeating reminder needs a clock time: at")
        return self


class ReminderAdd(Tool[AddArgs]):
    name = "reminder.add"
    description = (
        "Set a timer, a reminder or an alarm (rings until dismissed), once or every day, "
        "weekday or week: Arcee notifies the user with a sound when due."
    )
    usage = Usage(
        when='"Set a timer for 10 minutes", "remind me in 20 minutes to check the oven", '
        '"remind me at 5 pm to call mom", "wake me up at 7" (alarm), "remind me every day '
        'at 9 to take my medicine" (repeat).',
        avoid="Meetings and events others should see: calendar.add.",
        mistakes="Giving both in_minutes and at. Leaving the date out of `at` for another day.",
        example='reminder.add(text="Check the oven", in_minutes=20)',
    )
    args_model = AddArgs

    def __init__(self, reminders: Reminders, clock: Callable[[], datetime] | None = None) -> None:
        self._reminders = reminders
        self._clock = clock or (lambda: datetime.now().astimezone())

    async def run(self, args: AddArgs, ctx: ToolContext) -> ToolResult:
        now = self._clock()
        try:
            if args.in_minutes is not None:
                due, kind = now + timedelta(minutes=args.in_minutes), "timer"
            else:
                due, kind = when_at(args.at or "", now), "reminder"
            if args.alarm:
                kind = "alarm"
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        if due <= now:
            return ToolResult.fail(f"{describe(due, now)} has passed; give a time to come")
        if due - now > LONGEST:
            return ToolResult.fail("that is more than a year away; use calendar.add")
        reminder_id = await self._reminders.add_reminder(
            args.text, due.timestamp(), kind=kind, task_id=ctx.task_id, repeat=args.repeat
        )
        when = describe(due, now)
        left = _left((due - now).total_seconds())
        again = f", then {args.repeat}" if args.repeat else ""
        if kind == "timer" and args.text.strip().lower() == "timer":
            summary = f"Timer set for {left} (until {due:%H:%M})"
        elif kind == "alarm":
            summary = f"Alarm set for {when} ({left} from now){again}"
        else:
            summary = f"Reminder set for {when} ({left} from now){again}: {args.text}"
        data: dict[str, Any] = {"reminder_id": reminder_id, "text": args.text, "due": when}
        data["in"] = left
        if args.repeat:
            data["repeat"] = args.repeat
        return ToolResult.ok(summary, data=data)


class ListArgs(ToolArgs):
    pass


class ReminderList(Tool[ListArgs]):
    name = "reminder.list"
    description = "The timers and reminders still to come, soonest first, with their ids."
    usage = Usage(
        when='"What reminders do I have?", "how long is left on my timer?", or to find the '
        "id of one to cancel.",
        example="reminder.list()",
    )
    args_model = ListArgs
    read_only: ClassVar[bool] = True

    def __init__(self, reminders: Reminders, clock: Callable[[], datetime] | None = None) -> None:
        self._reminders = reminders
        self._clock = clock or (lambda: datetime.now().astimezone())

    async def run(self, args: ListArgs, ctx: ToolContext) -> ToolResult:
        now = self._clock()
        pending = await self._reminders.pending_reminders()
        items = [
            {
                "reminder_id": r.id,
                "text": r.text,
                "kind": r.kind,
                **({"repeat": r.repeat} if r.repeat else {}),
                "due": describe(datetime.fromtimestamp(r.due).astimezone(), now),
                "in": _left(max(0.0, r.due - now.timestamp())),
            }
            for r in pending
        ]
        summary = f"{len(items)} timers and reminders to come" if items else "None set"
        return ToolResult.ok(summary, data={"reminders": items})


class CancelArgs(ToolArgs):
    reminder_id: str = Field(pattern=r"^r_[0-9a-f]+$", description="Its r_... id (reminder.list)")


class ReminderCancel(Tool[CancelArgs]):
    name = "reminder.cancel"
    description = "Cancel a timer or reminder by its r_... id."
    usage = Usage(
        when='"Cancel my timer", "delete the reminder about rent": find the id with '
        "reminder.list first.",
        example='reminder.cancel(reminder_id="r_3fa1c2d0")',
    )
    args_model = CancelArgs

    def __init__(self, reminders: Reminders) -> None:
        self._reminders = reminders

    async def run(self, args: CancelArgs, ctx: ToolContext) -> ToolResult:
        found = await self._reminders.reminder(args.reminder_id)
        if found is None or found.status != "pending":
            return ToolResult.fail(
                f"no timer or reminder {args.reminder_id} to come; see reminder.list"
            )
        await self._reminders.set_reminder(found.id, status="cancelled")
        return ToolResult.ok(f"Cancelled: {found.text}", data={"cancelled": found.text})


class RoutineArgs(ToolArgs):
    request: str = Field(
        min_length=3,
        max_length=500,
        description='What Arcee should do then, as the user would ask it: "Tell me the '
        'weather in Delhi and my meetings today"',
    )
    at: str = Field(description="The clock time, such as 09:00 (the first one to come)")
    repeat: Literal["daily", "weekdays", "weekends", "weekly"] = "daily"


class RoutineAdd(Tool[RoutineArgs]):
    name = "routine.add"
    description = (
        "Run a request by itself at a time, every day, weekday, weekend or week: arceed "
        "does it then and shows the answer."
    )
    usage = Usage(
        when='"Every morning at 9, tell me the weather and my meetings", "each Friday at 6 '
        'pm, summarise my week". List and cancel them with reminder.list and reminder.cancel.',
        avoid="Something to remind the user of (reminder.add): a routine does the asking.",
        example='routine.add(request="Weather in Delhi and my meetings today", at="09:00")',
    )
    args_model = RoutineArgs
    tier = Tier.CONFIRM  # it will act later, unattended

    def __init__(self, reminders: Reminders, clock: Callable[[], datetime] | None = None) -> None:
        self._reminders = reminders
        self._clock = clock or (lambda: datetime.now().astimezone())

    def sentence(self, args: RoutineArgs) -> str:
        return f"Every {_EVERY[args.repeat]} at {args.at}, run: “{args.request}”?"

    async def run(self, args: RoutineArgs, ctx: ToolContext) -> ToolResult:
        now = self._clock()
        try:
            due = when_at(args.at, now)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        while args.repeat == "weekdays" and due.weekday() >= 5:
            due += timedelta(days=1)
        while args.repeat == "weekends" and due.weekday() < 5:
            due += timedelta(days=1)
        routine_id = await self._reminders.add_reminder(
            args.request, due.timestamp(), kind="routine", task_id=ctx.task_id, repeat=args.repeat
        )
        when = describe(due, now)
        return ToolResult.ok(
            f"Routine set: every {_EVERY[args.repeat]} at {due:%H:%M}, first {when}",
            data={"reminder_id": routine_id, "request": args.request, "first": when},
        )


_EVERY: Final = {"daily": "day", "weekdays": "weekday", "weekends": "weekend day", "weekly": "week"}


def reminder_tools(reminders: Reminders) -> list[Tool[Any]]:
    return [
        ReminderAdd(reminders),
        ReminderList(reminders),
        ReminderCancel(reminders),
        RoutineAdd(reminders),
    ]
