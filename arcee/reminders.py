"""arceed's scheduler (decision 0018): fires timers and reminders when they are due.

It sleeps until the soonest pending reminder in History, or POLL_S at most (a reminder
set by another process, `arcee-cli`, is seen within that), and wakes at once when one
is set or cancelled here (`poke`). One that fell due while arceed was not running
(the computer off, Arcee quit) fires when it next starts, said to be late.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from arcee.context.store import ReminderRow

log = logging.getLogger(__name__)

POLL_S: Final = 30.0
SNOOZE_S: Final = 600.0  # the notification's "Snooze 10 min"
LATE_S: Final = 120.0  # fired this much after it was due: say so
NOTIFICATIONS: Final = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications")


REPEATS: Final = ("daily", "weekdays", "weekends", "weekly")
ALARM_RING_S: Final = 300.0  # an alarm rings this long at most, unless dismissed
ALARM_EVERY_S: Final = 2.5
ALARM_SOUND: Final = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"


def next_due(due: float, repeat: str, now: float) -> float:
    """The next time a `repeat` reminder comes, after `now`, at the same clock time."""
    when = datetime.fromtimestamp(due)
    step = timedelta(days=7 if repeat == "weekly" else 1)
    while True:
        when += step
        if when.timestamp() <= now:
            continue
        weekend = when.weekday() >= 5
        if repeat == "weekdays" and weekend:
            continue
        if repeat == "weekends" and not weekend:
            continue
        return when.timestamp()


class Store(Protocol):
    async def pending_reminders(self) -> list[ReminderRow]: ...
    async def reminder(self, reminder_id: str) -> ReminderRow | None: ...
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
    async def set_reminder(
        self, reminder_id: str, *, status: str, due: float | None = None
    ) -> bool: ...


Fire = Callable[[ReminderRow, float], Awaitable[None]]


class Scheduler:
    """Fires each pending reminder at its time with `fire(reminder, seconds_late)`."""

    def __init__(self, store: Store, fire: Fire, *, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._fire = fire
        self._clock = clock
        self._wake = asyncio.Event()

    def poke(self) -> None:
        """A reminder was set, cancelled or snoozed: look again now."""
        self._wake.set()

    async def run(self) -> None:
        while True:
            wait = await self.fire_due()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), wait)

    async def fire_due(self) -> float:
        """Fire what is due; the seconds until the next one (POLL_S at most)."""
        pending = await self._store.pending_reminders()
        now = self._clock()
        upcoming: list[float] = []
        for reminder in pending:
            if reminder.due > now + 0.25:
                upcoming.append(reminder.due)
                continue
            if reminder.repeat in REPEATS:  # it comes again: pending at its next time
                again = next_due(reminder.due, reminder.repeat, now)
                await self._store.set_reminder(reminder.id, status="pending", due=again)
                upcoming.append(again)
            else:
                await self._store.set_reminder(reminder.id, status="fired")
            try:
                await self._fire(reminder, max(0.0, now - reminder.due))
            except Exception:  # one notification failing must not stop the others
                log.exception("could not show reminder %s", reminder.id)
        if not upcoming:
            return POLL_S
        return max(0.05, min(POLL_S, min(upcoming) - self._clock()))


def message(reminder: ReminderRow, late: float) -> tuple[str, str]:
    """The notification's title and body."""
    due = datetime.fromtimestamp(reminder.due)
    plain_timer = reminder.kind == "timer" and reminder.text.strip().lower() == "timer"
    plain_alarm = reminder.kind == "alarm" and reminder.text.strip().lower() in ("alarm", "")
    title = "Timer done" if plain_timer else "Alarm" if plain_alarm else reminder.text
    if late > LATE_S:
        body = f"It was due at {due:%H:%M} (Arcee was not running then)"
    elif reminder.kind == "alarm":
        body = f"Alarm for {due:%H:%M}"
    elif reminder.kind == "timer":
        set_for = round((reminder.due - reminder.created) / 60)
        body = f"Your {set_for}-minute timer is up" if set_for >= 1 else "Your timer is up"
    else:
        body = f"Reminder for {due:%H:%M}"
    return title, body


class DesktopNotifier:
    """GNOME's notifications, over arceed's own session bus: a banner with a sound that
    stays until dismissed, with Snooze 10 min and Done."""

    def __init__(self, bus: Any, store: Store, *, clock: Callable[[], float] = time.time) -> None:
        self._bus = bus
        self._store = store
        self._clock = clock
        self._shown: dict[int, str] = {}  # notification id -> reminder id
        self._snoozing: set[asyncio.Task[None]] = set()
        self._ringing: dict[int, asyncio.Task[None]] = {}  # an alarm's notification id
        self.ring_sound = ALARM_SOUND

    async def watch(self) -> None:
        """Hear which button was pressed on Arcee's notifications."""
        from dbus_fast import Message

        self._bus.add_message_handler(self._on_message)
        await self._bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=["type='signal',interface='org.freedesktop.Notifications'"],
            )
        )

    async def show(self, reminder: ReminderRow, late: float) -> None:
        from dbus_fast import Message, MessageType, Variant

        title, body = message(reminder, late)
        reply = await self._bus.call(
            Message(
                destination=NOTIFICATIONS[0],
                path=NOTIFICATIONS[1],
                interface=NOTIFICATIONS[0],
                member="Notify",
                signature="susssasa{sv}i",
                body=[
                    "Arcee",
                    0,
                    "alarm-symbolic",
                    title,
                    body,
                    ["snooze", "Snooze 10 min", "done", "Done"],
                    {
                        "urgency": Variant("y", 2),  # stays on screen until dismissed
                        "sound-name": Variant("s", "alarm-clock-elapsed"),
                        "desktop-entry": Variant("s", "me.iteshxt.Arcee"),
                        "category": Variant("s", "x-gnome.reminder"),
                    },
                    0,
                ],
            )
        )
        if reply is None or reply.message_type is MessageType.ERROR:
            raise RuntimeError(f"no notification: {reply.body if reply else 'no reply'}")
        notification = int(reply.body[0])
        self._shown[notification] = reminder.id
        if reminder.kind == "alarm":
            self._ringing[notification] = asyncio.ensure_future(self._ring())
        log.info("reminder %s shown: %s", reminder.id, title)

    async def _ring(self) -> None:
        """An alarm rings until it is dismissed or snoozed, ALARM_RING_S at most."""
        started = self._clock()
        while self._clock() - started < ALARM_RING_S:
            with contextlib.suppress(OSError):
                player = await asyncio.create_subprocess_exec(
                    "pw-play", "--media-role", "Notification", self.ring_sound
                )
                await player.wait()
            await asyncio.sleep(ALARM_EVERY_S)

    def _quiet(self, notification: int) -> None:
        if (ringing := self._ringing.pop(notification, None)) is not None:
            ringing.cancel()

    def _on_message(self, message: Any) -> None:
        if message.member in ("ActionInvoked", "NotificationClosed") and message.body:
            self._quiet(int(message.body[0]))
        if message.member == "ActionInvoked" and len(message.body) == 2:
            notification, action = int(message.body[0]), str(message.body[1])
            reminder_id = self._shown.pop(notification, None)
            if reminder_id is not None and action == "snooze":
                task = asyncio.ensure_future(self.snooze(reminder_id))
                self._snoozing.add(task)
                task.add_done_callback(self._snoozing.discard)
        elif message.member == "NotificationClosed" and message.body:
            self._shown.pop(int(message.body[0]), None)

    async def snooze(self, reminder_id: str) -> None:
        found = await self._store.reminder(reminder_id)
        due = self._clock() + SNOOZE_S
        if found is not None and found.repeat:
            # One that comes again keeps its own time: the snooze is a one-off copy.
            await self._store.add_reminder(found.text, due, kind=found.kind)
            return
        await self._store.set_reminder(reminder_id, status="pending", due=due)
