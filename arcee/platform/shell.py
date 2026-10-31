# pyright: basic, reportMissingImports=false
"""Client for the Arcee GNOME Shell extension (org.arcee.Shell on the session bus).

On Wayland only the compositor can bring a window to the front, see the whole
screen or synthesise input, so these go through the extension (decision 0009).
Calls are synchronous GIO D-Bus; callers run them off the event loop. Without the
extension, focusing reports that it is unavailable and Arcee carries on; screen
and input calls raise ShellUnavailableError.
"""

import json
import threading
from collections.abc import Callable, Sequence
from typing import Any, Final

BUS_NAME: Final = "org.arcee.Shell"
OBJECT_PATH: Final = "/org/arcee/Shell"
TIMEOUT_MS: Final = 2000
# A screenshot of a large screen takes longer than a click.
SCREENSHOT_TIMEOUT_MS: Final = 10_000


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

        return str(self._must("BeginControl", GLib.Variant("(s)", (reason,)))[0])

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
            Gio.DBusError.strip_remote_error(exc)  # keep the extension's own words
            raise ShellUnavailableError(f"{method} failed: {exc.message}") from exc
        return reply.unpack()

    def _call(self, method: str, args: Any) -> Any:
        if not self.available():
            return None
        from gi.repository import Gio

        reply = self._connect().call_sync(method, args, Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)
        return reply.unpack()


class StopWatcher:
    """Calls `on_stop` (from its own thread) when the user stops Arcee.

    The extension emits org.arcee.Shell.Stopped for the shortcut and the Stop pill,
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
