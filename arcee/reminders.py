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
from datetime import datetime
from typing import Any, Final, Protocol

from arcee.context.store import ReminderRow

log = logging.getLogger(__name__)

POLL_S: Final = 30.0
SNOOZE_S: Final = 600.0  # the notification's "Snooze 10 min"
LATE_S: Final = 120.0  # fired this much after it was due: say so
NOTIFICATIONS: Final = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications")


class Store(Protocol):
    async def pending_reminders(self) -> list[ReminderRow]: ...
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
    title = "Timer done" if plain_timer else reminder.text
    if late > LATE_S:
        body = f"It was due at {due:%H:%M} (Arcee was not running then)"
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
        self._shown[int(reply.body[0])] = reminder.id
        log.info("reminder %s shown: %s", reminder.id, title)

    def _on_message(self, message: Any) -> None:
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
        await self._store.set_reminder(reminder_id, status="pending", due=self._clock() + SNOOZE_S)
