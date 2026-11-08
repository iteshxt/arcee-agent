"""Alarms that ring, reminders and routines that come again, time elsewhere, a stopwatch."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from arcee.context.store import Store
from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import REMINDERS, filters_for
from arcee.models.base import Message
from arcee.reminders import DesktopNotifier, Scheduler, message, next_due
from arcee.tools.base import ToolContext
from arcee.tools.clock import Stopwatch, StopwatchArgs, TimeIn, TimeInArgs, local_zone
from arcee.tools.reminders import AddArgs, ReminderAdd, RoutineAdd, RoutineArgs

# A Friday afternoon, here (this computer's zone, whatever it is).
NOW = datetime(2026, 11, 6, 14, 0).astimezone()
CTX = ToolContext("t_r", Path("/home/user"))


@pytest.fixture
def store(tmp_path: Path) -> Any:
    opened = Store(tmp_path)
    yield opened
    opened.close()


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 11, day, hour, minute).timestamp()


@pytest.mark.parametrize(
    ("repeat", "expected"),
    [
        ("daily", at(7, 9)),
        ("weekdays", at(9, 9)),  # Friday: next is Monday
        ("weekends", at(7, 9)),
        ("weekly", at(13, 9)),
    ],
)
def test_the_next_time_keeps_the_clock_time(repeat: str, expected: float) -> None:
    assert next_due(at(6, 9), repeat, at(6, 9, 1)) == expected


def test_a_long_gap_skips_to_the_next_one_to_come() -> None:
    assert next_due(at(1, 9), "daily", at(6, 10)) == at(7, 9)


async def test_a_daily_reminder_fires_and_stays_pending_for_tomorrow(store: Any) -> None:
    fired: list[str] = []

    async def fire(reminder: Any, late: float) -> None:
        fired.append(reminder.text)

    reminder_id = await store.add_reminder("Take medicine", at(6, 9), repeat="daily")
    await Scheduler(store, fire, clock=lambda: at(6, 9, 0)).fire_due()

    again = await store.reminder(reminder_id)
    assert fired == ["Take medicine"]
    assert again is not None
    assert (again.status, again.due, again.repeat) == ("pending", at(7, 9), "daily")


async def test_an_alarm_and_a_repeating_reminder_are_set(store: Any) -> None:
    tool = ReminderAdd(store, clock=lambda: NOW)
    alarm = await tool.run(AddArgs(text="Alarm", at="07:00", alarm=True), CTX)
    daily = await tool.run(AddArgs(text="Medicine", at="9 pm", repeat="daily"), CTX)
    assert alarm.summary.startswith("Alarm set for 07:00 tomorrow")
    assert daily.summary.startswith("Reminder set for 21:00 today")
    assert ", then daily: Medicine" in daily.summary
    kinds = {(r.kind, r.repeat) for r in await store.pending_reminders()}
    assert kinds == {("alarm", ""), ("reminder", "daily")}
    with pytest.raises(ValueError, match="needs a clock time"):
        AddArgs(text="x", in_minutes=5, repeat="daily")


def test_an_alarm_says_so() -> None:
    from arcee.context.store import ReminderRow

    row = ReminderRow("r_1", "Alarm", NOW.timestamp(), NOW.timestamp(), "alarm", "fired", None)
    assert message(row, 0) == ("Alarm", f"Alarm for {NOW:%H:%M}")


class Bus:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def add_message_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def call(self, message: Any) -> Any:
        from dbus_fast import Message, MessageType

        return Message(
            message_type=MessageType.METHOD_RETURN, reply_serial=1, signature="u", body=[9]
        )


async def test_an_alarm_rings_until_dismissed(store: Any, tmp_path: Path) -> None:
    from dbus_fast import Message, MessageType

    bus = Bus()
    notifier = DesktopNotifier(bus, store)
    notifier.ring_sound = str(tmp_path / "none.oga")  # pw-play fails quietly: no sound
    await notifier.watch()
    alarm_id = await store.add_reminder("Alarm", NOW.timestamp(), kind="alarm")
    alarm = await store.reminder(alarm_id)
    assert alarm is not None

    await notifier.show(alarm, 0)
    ringing = notifier._ringing[9]  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)
    assert not ringing.done()
    closed = Message(
        message_type=MessageType.SIGNAL,
        path="/org/freedesktop/Notifications",
        interface="org.freedesktop.Notifications",
        member="NotificationClosed",
        signature="uu",
        body=[9, 2],
    )
    for handler in bus.handlers:
        handler(closed)
    await asyncio.sleep(0)
    assert ringing.cancelled()


async def test_snoozing_a_daily_reminder_keeps_its_own_time(store: Any) -> None:
    notifier = DesktopNotifier(Bus(), store, clock=lambda: at(6, 9))
    daily = await store.add_reminder("Medicine", at(7, 9), repeat="daily")
    await notifier.snooze(daily)
    pending = sorted((r.due, r.repeat) for r in await store.pending_reminders())
    assert pending == [(at(6, 9, 10), ""), (at(7, 9), "daily")]


async def test_a_routine_asks_first_and_starts_on_a_weekday(store: Any) -> None:
    tool = RoutineAdd(store, clock=lambda: NOW)
    args = RoutineArgs(request="Weather in Delhi and my meetings", at="09:00", repeat="weekdays")
    assert tool.tier_for(args, Policy()) is Tier.CONFIRM
    assert tool.sentence(args) == (
        "Every weekday at 09:00, run: “Weather in Delhi and my meetings”?"
    )
    result = await tool.run(args, CTX)
    (routine,) = await store.pending_reminders()
    assert (routine.kind, routine.repeat) == ("routine", "weekdays")
    assert routine.due == at(9, 9)  # Saturday and Sunday skipped
    assert result.summary == "Routine set: every weekday at 09:00, first Mon 9 Nov 09:00"


async def test_arceed_runs_a_routine_when_free_and_skips_one_long_missed() -> None:
    from arcee.daemon import ROUTINE_LATE_S, AgentService

    from .test_daemon import FakeRunner

    service = AgentService(FakeRunner())  # pyright: ignore[reportArgumentType]
    started: list[str] = []
    service.start_request = started.append  # type: ignore[method-assign]
    assert not await service.run_routine("weather", ROUTINE_LATE_S + 60)
    assert await service.run_routine("weather", 30)
    assert started == ["weather"]


# --- time.in and time.stopwatch ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("place", "zone"),
    [
        ("New York", "America/New_York"),
        ("delhi", "Asia/Kolkata"),
        ("IST", "Asia/Kolkata"),
        ("Tokyo", "Asia/Tokyo"),
        ("San Francisco", "America/Los_Angeles"),
        ("Europe/Berlin", "Europe/Berlin"),
        ("Atlantis", None),
    ],
)
def test_places_people_say_have_a_zone(place: str, zone: str | None) -> None:
    assert local_zone(place) == zone


UTC_NOON = datetime.fromisoformat("2026-11-08T12:00:00+00:00")


async def test_the_time_in_a_city() -> None:
    tool = TimeIn(now=lambda: UTC_NOON)
    result = await tool.run(TimeInArgs(place="Tokyo"), CTX)
    assert result.data is not None
    assert (result.data["time"], result.data["offset"]) == ("21:00", "UTC+9")


async def test_a_time_there_converted() -> None:
    tool = TimeIn(now=lambda: UTC_NOON)
    result = await tool.run(TimeInArgs(place="Delhi", at="11 pm", from_place="London"), CTX)
    assert result.data is not None
    assert result.data["time"] == "04:30"
    assert result.data["date"] == "2026-11-09"
    assert result.data["from"] == "23:00 London"


async def test_any_other_place_is_geocoded() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == "Ludhiana"
        return httpx.Response(
            200,
            json={
                "results": [{"name": "Ludhiana", "country": "India", "timezone": "Asia/Kolkata"}]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    tool = TimeIn(now=lambda: UTC_NOON, client=client)
    result = await tool.run(TimeInArgs(place="Ludhiana"), CTX)
    await client.aclose()
    assert result.summary.startswith("17:30 Sun 08 Nov in Ludhiana, India")


async def test_the_stopwatch_runs_pauses_and_laps() -> None:
    now = [100.0]
    watch = Stopwatch(clock=lambda: now[0])
    await watch.run(StopwatchArgs(action="start"), CTX)
    now[0] = 130.0
    lap = await watch.run(StopwatchArgs(action="lap"), CTX)
    now[0] = 200.0
    stopped = await watch.run(StopwatchArgs(action="stop"), CTX)
    now[0] = 500.0
    status = await watch.run(StopwatchArgs(action="status"), CTX)
    assert lap.data is not None
    assert lap.data["laps"] == ["30 s"]
    assert stopped.summary == "Stopwatch stopped: 1 min 40 s"
    assert status.summary == "Stopwatch stopped: 1 min 40 s"


@pytest.mark.parametrize(
    "said",
    [
        "What time is it in New York?",
        "Start a stopwatch",
        "Every morning at 9, tell me the weather",
        "Wake me up at 7",
        "Start a 25 minute focus session",
    ],
)
def test_time_requests_bring_the_time_tools(said: str) -> None:
    assert set(REMINDERS) <= set(filters_for([Message("user", said)]))
