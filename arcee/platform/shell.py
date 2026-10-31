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
            raise ShellUnavailableError(f"{method} failed: {exc.message}") from exc
        return reply.unpack()

    def _call(self, method: str, args: Any) -> Any:
        if not self.available():
            return None
        from gi.repository import Gio

        reply = self._connect().call_sync(method, args, Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)
        return reply.unpack()


class StopWatcher:
    """Calls `on_stop` (from its own thread) when the user presses the stop shortcut.

    The extension emits org.arcee.Shell.Stopped; a GLib main loop on a daemon thread
    receives it, so the asyncio loop stays free.
    """

    def __init__(self, on_stop: Callable[[], None]) -> None:
        self._on_stop = on_stop
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
            "Stopped",
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda *_: self._on_stop(),
        )
        ready.set()
        self._loop.run()
