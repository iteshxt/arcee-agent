# pyright: basic, reportMissingImports=false
"""Client for the Arcee GNOME Shell extension (me.iteshxt.Arcee.Shell on the session bus).

On Wayland only the compositor can bring a window to the front, see the whole
screen or synthesise input, so these go through the extension (decision 0009).
Calls are synchronous GIO D-Bus; callers run them off the event loop. Without the
extension, focusing reports that it is unavailable and Arcee carries on; screen
and input calls raise ShellUnavailableError.
"""

import json
import logging
import re
import threading
from collections.abc import Callable, Sequence
from typing import Any, Final

BUS_NAME: Final = "me.iteshxt.Arcee.Shell"
OBJECT_PATH: Final = "/me/iteshxt/Arcee/Shell"
log = logging.getLogger(__name__)
TIMEOUT_MS: Final = 2000
# Taking control waits longer: while an app cold-starts (Chrome) GNOME Shell can be
# busy for seconds, and a BeginControl that gave up after 2 s failed the request.
CONTROL_TIMEOUT_MS: Final = 10_000
# A screenshot of a large screen takes longer than a click.
SCREENSHOT_TIMEOUT_MS: Final = 10_000


CONTROL_REASON: Final = "Arcee is working"
# How often to look whether the user has resumed Arcee.
PAUSE_POLL_S: Final = 0.5


class ShellUnavailableError(RuntimeError):
    """The Arcee shell extension is not running, or refused the call."""


class ShellBridge:
    def __init__(self) -> None:
        self._proxy: Any = None

    def _connect(self) -> Any:
        if self._proxy is None:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio

            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
                | Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS,
                None,
                BUS_NAME,
                OBJECT_PATH,
                BUS_NAME,
                None,
            )
        return self._proxy

    def available(self) -> bool:
        try:
            return self._connect().get_name_owner() is not None
        except Exception:
            return False

    def windows(self) -> list[dict[str, Any]]:
        result = self._call("ListWindows", None)
        return json.loads(result[0]) if result else []

    def window_action(self, window_id: int, action: str) -> bool:
        """focus, minimize, maximize, restore or close; False when the window is gone."""
        from gi.repository import GLib

        return bool(self._must("WindowAction", GLib.Variant("(ts)", (window_id, action)))[0])

    def tile_window(self, window_id: int, where: str) -> bool:
        """left, right, top, bottom, the quarters or center; False when the window is gone."""
        from gi.repository import GLib

        return bool(self._must("TileWindow", GLib.Variant("(ts)", (window_id, where)))[0])

    def move_window(self, window_id: int, monitor: int, workspace: int) -> bool:
        """To monitor and/or workspace (0-based; -1 leaves it); False when it is gone."""
        from gi.repository import GLib

        variant = GLib.Variant("(tii)", (window_id, monitor, workspace))
        return bool(self._must("MoveWindow", variant)[0])

    def monitors(self) -> dict[str, Any]:
        result = self._must("Monitors", None)
        return json.loads(result[0]) if result else {}

    def notifications(self) -> list[dict[str, Any]]:
        result = self._must("Notifications", None)
        return json.loads(result[0]) if result else []

    def focus_window(self, window_id: int) -> bool:
        from gi.repository import GLib

        result = self._call("FocusWindow", GLib.Variant("(t)", (window_id,)))
        return bool(result and result[0])

    def focus_title(self, title: str) -> bool:
        if not self.available():
            return False
        from gi.repository import GLib

        result = self._call("FocusTitle", GLib.Variant("(s)", (title,)))
        return bool(result and result[0])

    def screenshot(self) -> dict[str, Any]:
        """The whole screen as a PNG: {path, width, height}, the size in screen coordinates."""
        return json.loads(self._must("Screenshot", None, SCREENSHOT_TIMEOUT_MS)[0])

    def click(self, x: float, y: float, button: int = 1, count: int = 1) -> None:
        from gi.repository import GLib

        self._must("PointerClick", GLib.Variant("(dduu)", (x, y, button, count)))

    def scroll(self, x: float, y: float, steps: int) -> None:
        from gi.repository import GLib

        self._must("Scroll", GLib.Variant("(ddi)", (x, y, steps)))

    def key_combo(self, keyvals: Sequence[int]) -> None:
        from gi.repository import GLib

        self._must("KeyCombo", GLib.Variant("(au)", (list(keyvals),)))

    def type_text(self, text: str) -> None:
        from gi.repository import GLib

        self._must("TypeText", GLib.Variant("(s)", (text,)))

    def release_all(self) -> None:
        self._must("ReleaseAll", None)

    def begin_control(self, reason: str) -> str:
        """Take (or keep) control of the screen; returns "active" or "paused"."""
        from gi.repository import GLib

        reply = self._must("BeginControl", GLib.Variant("(s)", (reason,)), CONTROL_TIMEOUT_MS)
        return str(reply[0])

    def end_control(self) -> None:
        """Give control back, if this process holds it. Quiet without the extension."""
        if self.available():
            self._must("EndControl", None)

    def control_state(self) -> str:
        """ "off", "active" or "paused"."""
        return str(self._must("ControlState", None)[0])

    def _must(self, method: str, args: Any, timeout_ms: int = TIMEOUT_MS) -> Any:
        if not self.available():
            raise ShellUnavailableError(
                "the Arcee shell extension is not running (arcee-cli shell-extension)"
            )
        from gi.repository import Gio, GLib

        try:
            reply = self._connect().call_sync(
                method, args, Gio.DBusCallFlags.NONE, timeout_ms, None
            )
        except GLib.Error as exc:
            if "UnknownMethod" in exc.message:
                # GNOME loads an extension's new code only at the next login.
                raise ShellUnavailableError(
                    "Arcee's GNOME Shell extension running now is older than Arcee: log out "
                    "and back in to load the new one"
                ) from exc
            # Keep the extension's own words, without "GDBus.Error:<error name>: ".
            message = re.sub(r"^GDBus\.Error:[\w.\-]+:\s*", "", exc.message)
            raise ShellUnavailableError(f"{method} failed: {message}") from exc
        return reply.unpack()

    def _call(self, method: str, args: Any) -> Any:
        if not self.available():
            return None
        from gi.repository import Gio

        reply = self._connect().call_sync(method, args, Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)
        return reply.unpack()


class ShellPresence:
    """Control of the screen through the extension, for actions the user can see.

    Without the extension it does nothing. A stop during a pause is left to the stop
    signal, which cancels the task.
    """

    def __init__(self, bridge: ShellBridge | None = None) -> None:
        self._bridge = bridge or ShellBridge()
        self.held = False

    async def acting(self) -> None:
        import asyncio

        if not await asyncio.to_thread(self._bridge.available):
            return
        try:
            state = await asyncio.to_thread(self._bridge.begin_control, CONTROL_REASON)
        except ShellUnavailableError as exc:
            # The glow is for what the user sees; a step that opens an app needs none.
            # Screen and input calls still refuse without control, so nothing acts unseen.
            log.warning("could not show that Arcee is working: %s", exc)
            self.held = False
            return
        while state == "paused":
            await asyncio.sleep(PAUSE_POLL_S)
            state = await asyncio.to_thread(self._bridge.control_state)
        self.held = state == "active"

    async def release(self) -> None:
        import asyncio

        if self.held:
            self.held = False
            await asyncio.to_thread(self._bridge.end_control)


class StopWatcher:
    """Calls `on_stop` (from its own thread) when the user stops Arcee.

    The extension emits me.iteshxt.Arcee.Shell.Stopped for the shortcut and the Stop pill,
    and Paused and Resumed when the user takes the mouse or keyboard and gives it
    back; a GLib main loop on a daemon thread receives them, so the asyncio loop
    stays free.
    """

    def __init__(
        self,
        on_stop: Callable[[], None],
        on_pause: Callable[[], None] = lambda: None,
        on_resume: Callable[[], None] = lambda: None,
    ) -> None:
        self._handlers = {"Stopped": on_stop, "Paused": on_pause, "Resumed": on_resume}
        self._loop: Any = None

    def start(self) -> None:
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True, name="arcee-stop").start()
        ready.wait(2)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.quit()

    def _run(self, ready: threading.Event) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib

            context = GLib.MainContext.new()
            context.push_thread_default()
            self._loop = GLib.MainLoop.new(context, False)
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:  # no session bus (a test, a bare TTY): nothing to watch
            ready.set()
            return
        bus.signal_subscribe(
            None,
            BUS_NAME,
            None,
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda _bus, _sender, _path, _iface, signal, _args: self._handlers.get(
                signal, lambda: None
            )(),
        )
        ready.set()
        self._loop.run()
