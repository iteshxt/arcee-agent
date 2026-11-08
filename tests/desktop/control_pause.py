"""What pauses Arcee's control of the screen, in a headless GNOME Shell (control_pause.sh).

The user's input comes from a virtual device of its own made in the Shell (Eval), which
the extension's event filter ignores like Arcee's own: only the watch on the pointer and
GNOME's idle time can see it, as it alone sees input meant for app windows.
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

ARCEE = ("me.iteshxt.Arcee.Shell", "/me/iteshxt/Arcee/Shell", "me.iteshxt.Arcee.Shell")
GNOME = ("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell")
BUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
# The user's own keyboard and mouse: devices of their own, not the bridge's.
USER = """
const C = imports.gi.Clutter, G = imports.gi.GLib;
const seat = C.get_default_backend().get_default_seat();
globalThis.userKeys ??= seat.create_virtual_device(C.InputDeviceType.KEYBOARD_DEVICE);
globalThis.userMouse ??= seat.create_virtual_device(C.InputDeviceType.POINTER_DEVICE);
const t = G.get_monotonic_time();
"""
KEY = "userKeys.notify_keyval(t, 0x35, C.KeyState.PRESSED);" + (
    "userKeys.notify_keyval(t + 20000, 0x35, C.KeyState.RELEASED);"
)
MOVE = "for (let i = 0; i < 6; i++) userMouse.notify_relative_motion(t + i * 10000, 20, 0);"
NUDGE = "userMouse.notify_relative_motion(t, 6, 4);"

Act = Callable[[], Awaitable[None]]


class Test:
    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self.paused = False
        self.results: list[bool] = []
        bus.add_message_handler(self._signal)

    def _signal(self, message: Message) -> None:
        if message.message_type is MessageType.SIGNAL and message.member == "Paused":
            self.paused = True

    async def call(
        self, member: str, sig: str = "", body: tuple[Any, ...] = (), to: tuple[str, ...] = ARCEE
    ) -> Any:
        reply = await self.bus.call(
            Message(
                destination=to[0],
                path=to[1],
                interface=to[2],
                member=member,
                signature=sig,
                body=list(body),
            )
        )
        assert reply is not None
        assert reply.message_type is not MessageType.ERROR, reply.body
        return reply.body

    async def user(self, js: str) -> None:
        ok, _ = await self.call("Eval", "s", (USER + js + "'done'",), GNOME)
        assert ok, "Eval refused: is the Shell in --unsafe-mode?"

    async def arcee(self) -> None:
        await self.call("PointerClick", "dduu", (640.0, 400.0, 1, 1))
        await self.call("TypeText", "s", ("12",))

    async def case(self, name: str, act: Act, pauses: bool) -> None:
        await self.call("BeginControl", "s", ("test",))
        await asyncio.sleep(0.6)
        self.paused = False
        await act()
        await asyncio.sleep(1.2)
        self.results.append(self.paused == pauses)
        verdict = "OK" if self.paused == pauses else "WRONG"
        print(f"{name}: paused={self.paused} expected={pauses} -> {verdict}", flush=True)
        await self.call("EndControl")
        await asyncio.sleep(0.3)


async def main() -> bool:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    test = Test(bus)
    await test.call("AddMatch", "s", ("type='signal',interface='me.iteshxt.Arcee.Shell'",), BUS)

    async def key() -> None:
        await test.user(KEY)

    async def move() -> None:
        await test.user(MOVE)

    async def nudge() -> None:
        await test.user(NUDGE)

    async def nudge_then_key() -> None:
        await nudge()
        await asyncio.sleep(0.5)
        await key()

    async def arcee_then_key() -> None:
        await test.arcee()
        await asyncio.sleep(0.8)
        await key()

    await test.case("A Arcee's own click and typing", test.arcee, pauses=False)
    await test.case("B the user's key over an app", key, pauses=True)
    await test.case("C the user's mouse, 120 px", move, pauses=True)
    await test.case("D a 7 px nudge", nudge, pauses=False)
    await test.case("E a nudge, then a key", nudge_then_key, pauses=True)
    await test.case("F Arcee acts, then the user's key", arcee_then_key, pauses=True)
    bus.disconnect()
    return all(test.results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
