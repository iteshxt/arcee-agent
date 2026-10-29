# pyright: basic, reportMissingImports=false
"""Client for the Arcee GNOME Shell extension (org.arcee.Shell on the session bus).

On Wayland only the compositor can bring a window to the front, so focusing goes
through the extension. Calls are synchronous GIO D-Bus; callers run them off the
event loop (the AT-SPI thread does). Without the extension every call reports
that it is unavailable and Arcee carries on without focusing.
"""

import json
from typing import Any, Final

BUS_NAME: Final = "org.arcee.Shell"
OBJECT_PATH: Final = "/org/arcee/Shell"
TIMEOUT_MS: Final = 2000


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

    def _call(self, method: str, args: Any) -> Any:
        if not self.available():
            return None
        from gi.repository import Gio

        reply = self._connect().call_sync(method, args, Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)
        return reply.unpack()
