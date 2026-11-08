"""Timers and reminders (decision 0018): History, the tools, arceed's scheduler and the
notification it shows."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from arcee.context.store import ReminderRow, Store
from arcee.core.toolsets import REMINDERS, filters_for
from arcee.models.base import Message
from arcee.reminders import POLL_S, SNOOZE_S, DesktopNotifier, Scheduler, message
from arcee.tools.base import ToolContext
from arcee.tools.reminders import (
    AddArgs,
    CancelArgs,
    ListArgs,
    ReminderAdd,
    ReminderCancel,
    ReminderList,
    when_at,
)

NOW = datetime(2026, 11, 8, 14, 0).astimezone()
CTX = ToolContext("t_r", Path("/home/user"))


@pytest.fixture
def store(tmp_path: Path) -> Any:
    opened = Store(tmp_path)
    yield opened
    opened.close()


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("17:00", NOW.replace(hour=17, minute=0)),
        ("5 pm", NOW.replace(hour=17, minute=0)),
        ("5:30pm", NOW.replace(hour=17, minute=30)),
        ("9:00", NOW.replace(hour=9, minute=0) + timedelta(days=1)),  # past today: tomorrow
        ("12 am", NOW.replace(hour=0, minute=0) + timedelta(days=1)),
        ("2026-11-09T07:30", datetime(2026, 11, 9, 7, 30).astimezone()),
        ("2026-11-09 07:30", datetime(2026, 11, 9, 7, 30).astimezone()),
    ],
)
def test_times_as_people_say_them(said: str, expected: datetime) -> None:
    assert when_at(said, NOW) == expected


@pytest.mark.parametrize("said", ["25:00", "13 pm", "teatime", "tomorrow"])
def test_what_is_not_a_time_is_refused(said: str) -> None:
    with pytest.raises(ValueError, match="not a"):
        when_at(said, NOW)


def test_a_reminder_needs_one_time() -> None:
    with pytest.raises(ValueError, match="either in_minutes or at"):
        AddArgs(text="Tea")
    with pytest.raises(ValueError, match="either in_minutes or at"):
        AddArgs(text="Tea", in_minutes=5, at="17:00")


async def test_a_timer_and_a_reminder_are_set_listed_and_cancelled(store: Any) -> None:
    clock = lambda: NOW  # noqa: E731
    add = ReminderAdd(store, clock)

    timer = await add.run(AddArgs(text="Timer", in_minutes=10), CTX)
    assert timer.summary == "Timer set for 10 min (until 14:10)"
    call = await add.run(AddArgs(text="Call mom", at="17:00"), CTX)
    assert call.summary == "Reminder set for 17:00 today (3 h from now): Call mom"
    assert (await add.run(AddArgs(text="Rent", at="2026-11-01T09:00"), CTX)).status != "ok"

    listed = await ReminderList(store, clock).run(ListArgs(), CTX)
    assert listed.data is not None
    assert [(r["text"], r["due"], r["kind"]) for r in listed.data["reminders"]] == [
        ("Timer", "14:10 today", "timer"),
        ("Call mom", "17:00 today", "reminder"),
    ]

    cancel = ReminderCancel(store)
    first = listed.data["reminders"][0]["reminder_id"]
    assert (await cancel.run(CancelArgs(reminder_id=first), CTX)).summary == "Cancelled: Timer"
    assert (await cancel.run(CancelArgs(reminder_id=first), CTX)).status != "ok"  # once
    assert [r.text for r in await store.pending_reminders()] == ["Call mom"]


async def test_the_scheduler_is_told_at_once(store: Any) -> None:
    told: list[str] = []
    store.on_reminders(lambda: told.append("changed"))
    reminder = await store.add_reminder("Tea", NOW.timestamp() + 60)
    await store.set_reminder(reminder, status="fired")  # firing is its own doing
    await store.set_reminder(reminder, status="pending", due=NOW.timestamp() + 600)  # snooze
    assert told == ["changed", "changed"]


async def test_due_reminders_fire_once_and_the_next_is_waited_for(store: Any) -> None:
    now = NOW.timestamp()
    clock = [now]
    fired: list[tuple[str, float]] = []

    async def fire(reminder: ReminderRow, late: float) -> None:
        fired.append((reminder.text, late))

    await store.add_reminder("Missed while off", now - 3600)
    await store.add_reminder("Now", now)
    await store.add_reminder("Soon", now + 90)
    scheduler = Scheduler(store, fire, clock=lambda: clock[0])

    wait = await scheduler.fire_due()
    assert fired == [("Missed while off", 3600.0), ("Now", 0.0)]
    assert wait == POLL_S  # the next is 90 s away: look again in 30 s at most

    clock[0] = now + 80
    assert await scheduler.fire_due() == pytest.approx(10)  # then exactly when it is due
    clock[0] = now + 90
    await scheduler.fire_due()
    assert [text for text, _ in fired] == ["Missed while off", "Now", "Soon"]
    assert await store.pending_reminders() == []


def test_what_the_notification_says() -> None:
    def row(text: str, kind: str, minutes: float) -> ReminderRow:
        due = NOW.timestamp() + minutes * 60
        return ReminderRow("r_1", text, due, NOW.timestamp(), kind, "fired", None)

    assert message(row("Timer", "timer", 10), 0) == ("Timer done", "Your 10-minute timer is up")
    assert message(row("Check the oven", "timer", 20), 0) == (
        "Check the oven",
        "Your 20-minute timer is up",
    )
    assert message(row("Call mom", "reminder", 180), 0) == ("Call mom", "Reminder for 17:00")
    late = message(row("Call mom", "reminder", 180), 3 * 3600)
    assert late[1] == "It was due at 17:00 (Arcee was not running then)"


class Bus:
    """arceed's session bus, as the notifier uses it."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.handlers: list[Any] = []

    def add_message_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def call(self, message: Any) -> Any:
        from dbus_fast import Message, MessageType

        self.sent.append(message)
        return Message(
            message_type=MessageType.METHOD_RETURN, reply_serial=1, signature="u", body=[7]
        )


async def test_the_notification_stays_rings_and_snoozes(store: Any) -> None:
    from dbus_fast import Message, MessageType

    bus = Bus()
    clock = NOW.timestamp()
    notifier = DesktopNotifier(bus, store, clock=lambda: clock)
    await notifier.watch()
    reminder_id = await store.add_reminder("Call mom", clock)
    reminder = await store.reminder(reminder_id)
    assert reminder is not None
    await store.set_reminder(reminder_id, status="fired")

    await notifier.show(reminder, 0)

    notify = bus.sent[-1]
    assert notify.member == "Notify"
    title, actions, hints = notify.body[3], notify.body[5], notify.body[6]
    assert title == "Call mom"
    assert actions == ["snooze", "Snooze 10 min", "done", "Done"]
    assert hints["urgency"].value == 2  # stays until dismissed
    assert hints["sound-name"].value == "alarm-clock-elapsed"

    pressed = Message(
        message_type=MessageType.SIGNAL,
        path="/org/freedesktop/Notifications",
        interface="org.freedesktop.Notifications",
        member="ActionInvoked",
        signature="us",
        body=[7, "snooze"],
    )
    for handler in bus.handlers:
        handler(pressed)
    await notifier.snooze(reminder_id)  # (what the handler scheduled, awaited here)
    again = await store.reminder(reminder_id)
    assert again is not None
    assert again.status == "pending"
    assert again.due == pytest.approx(clock + SNOOZE_S)


@pytest.mark.parametrize(
    "said",
    [
        "Set a timer for 10 minutes",
        "Remind me in 20 minutes to check the oven",
        "Remind me at 5 pm to call mom",
        "Wake me up at 7 tomorrow",
        "What reminders do I have?",
        "Cancel my timer",
    ],
)
def test_timer_and_reminder_words_bring_the_tools(said: str) -> None:
    assert set(REMINDERS) <= set(filters_for([Message("user", said)])), said


def test_other_requests_do_not(store: Any) -> None:
    for request in ("Open Chrome", "What's the weather like?", "Rename the photo"):
        assert not set(REMINDERS) & set(filters_for([Message("user", request)])), request


async def test_arceed_starts_at_login_for_a_reminder(store: Any, tmp_path: Path) -> None:
    from arcee.settings import reminders_pending

    history = tmp_path / "history.db"
    assert not reminders_pending(history)
    await store.add_reminder("Tea", NOW.timestamp() + 60)
    assert reminders_pending(history)
    assert not reminders_pending(tmp_path / "missing.db")
