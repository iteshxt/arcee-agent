"""Is every part of Arcee in place? One check per part, each with the fix.

The Diagnostics page shows these; they replace running shell-check, browser-check
and the like by hand. Checks only look: none starts or changes anything (asking
arceed for its status may start it, which is what its D-Bus activation is for).
"""

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcee.browser.client import BrowserClient, BrowserError
from arcee.browser.host import HOST_NAME
from arcee.browser.setup import EXTENSION_DIR, chrome_hosts_dir
from arcee.platform.shell import ShellBridge
from arcee.voice import KEY_ENV as SPEECH_KEY_ENV


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    # A failed advisory check is not a fault (Chrome closed is normal).
    advisory: bool = False
    # What the app's Fix button runs: "shell_extension", "daemon", "browser_bridge" or
    # "keys"; empty when only the user can fix it.
    fix_action: str = ""


EXTENSION_UUID = "arcee@iteshxt.me"
# GNOME Shell's ExtensionState values (org.gnome.Shell.Extensions).
_ACTIVE, _INACTIVE, _ERROR, _OUT_OF_DATE, _INITIALIZED = 1, 2, 3, 4, 6


def extension_dirs() -> list[Path]:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return [
        Path(data) / "gnome-shell" / "extensions" / EXTENSION_UUID,
        Path("/usr/share/gnome-shell/extensions") / EXTENSION_UUID,
    ]


def extension_check(info: dict[str, Any] | None, installed: bool) -> Check:
    """What is in the way of the extension, from GNOME's own view of it.

    "Not running" alone sent people who had just installed the .deb looking for a
    missing file: GNOME loads new extensions only at login, and then they are off.
    """
    name = "GNOME Shell extension"
    state = int((info or {}).get("state", 0))
    if not installed:
        return Check(name, False, "not installed", "install it", fix_action="shell_extension")
    if not info:
        return Check(
            name,
            False,
            "installed, but GNOME loads new extensions at login",
            "log out and back in, then press Fix to turn it on",
            fix_action="shell_extension",
        )
    if state in (_INACTIVE, _INITIALIZED):
        return Check(
            name, False, "installed but turned off", "turn it on", fix_action="shell_extension"
        )
    if state == _ERROR:
        return Check(
            name,
            False,
            f"failed to start: {(info or {}).get('error', 'unknown error')}",
            "log out and back in; if it stays, report it",
        )
    if state == _OUT_OF_DATE:
        return Check(name, False, "not made for this GNOME version")
    return Check(name, False, "starting", "wait a moment, then check again")


async def extension_info() -> dict[str, Any] | None:
    """GNOME Shell's record of the extension; None (or {}) when it has not loaded it."""
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except OSError:
        return None
    try:
        reply = await asyncio.wait_for(
            bus.call(
                Message(
                    destination="org.gnome.Shell.Extensions",
                    path="/org/gnome/Shell/Extensions",
                    interface="org.gnome.Shell.Extensions",
                    member="GetExtensionInfo",
                    signature="s",
                    body=[EXTENSION_UUID],
                )
            ),
            timeout=5,
        )
    except TimeoutError:
        return None
    finally:
        bus.disconnect()
    if reply.message_type is not MessageType.METHOD_RETURN:
        return None
    return {key: variant.value for key, variant in reply.body[0].items()}


async def shell_extension() -> Check:
    if await asyncio.to_thread(ShellBridge().available):
        return Check("GNOME Shell extension", True, "running")
    installed = any(path.is_dir() for path in extension_dirs())
    return extension_check(await extension_info(), installed)


async def daemon() -> Check:
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except OSError as exc:
        return Check("Arcee service (arceed)", False, f"no session bus: {exc}")
    try:
        reply = await asyncio.wait_for(
            bus.call(
                Message(
                    destination="me.iteshxt.Arcee.Agent",
                    path="/me/iteshxt/Arcee/Agent",
                    interface="me.iteshxt.Arcee.Agent",
                    member="Status",
                )
            ),
            timeout=10,
        )
    except TimeoutError:
        reply = None
    finally:
        bus.disconnect()
    if reply is None or reply.message_type is not MessageType.METHOD_RETURN:
        return Check(
            "Arcee service (arceed)",
            False,
            "not reachable: the panel cannot run requests",
            "install the service",
            fix_action="daemon",
        )
    state = "working" if '"running"' in str(reply.body[0]) else "ready"
    return Check("Arcee service (arceed)", True, state)


async def chrome() -> Check:
    client = BrowserClient()
    try:
        await asyncio.wait_for(client.request("ping"), timeout=5)
    except BrowserError, TimeoutError:
        if not any(path.is_file() for path in host_manifests()):
            return Check(
                "Chrome bridge",
                False,
                "the Chrome helper is not registered",
                "register it, then load the Arcee extension in Chrome",
                advisory=True,
                fix_action="browser_bridge",
            )
        return Check(
            "Chrome bridge",
            False,
            "Chrome is closed, or the Arcee extension is not loaded in it",
            f"load it once: chrome://extensions, Developer mode, Load unpacked, {EXTENSION_DIR}",
            advisory=True,
            fix_action="browser_bridge",
        )
    finally:
        await client.aclose()
    return Check("Chrome bridge", True, "connected")


def host_manifests() -> list[Path]:
    """Where Chrome looks for Arcee's native host: the user's own, then every user's."""
    name = f"{HOST_NAME}.json"
    return [
        chrome_hosts_dir() / name,
        Path("/etc/opt/chrome/native-messaging-hosts") / name,
        Path("/etc/chromium/native-messaging-hosts") / name,
    ]


async def keys() -> list[Check]:
    chat = bool(os.environ.get("GROQ_API_KEY"))
    speech = bool(os.environ.get(SPEECH_KEY_ENV))
    return [
        Check(
            "Groq key (thinking)",
            chat,
            "set" if chat else "missing: the Groq models cannot run",
            "" if chat else "add it on the Models page",
            fix_action="" if chat else "keys",
        ),
        Check(
            "Groq key (speech)",
            speech,
            "set" if speech else "missing: the mic cannot transcribe",
            "" if speech else "add it on the Models page",
            fix_action="" if speech else "keys",
        ),
    ]


# Programs Arcee runs, what each is for, and how to install it.
PROGRAMS: tuple[tuple[str, str, str], ...] = (
    ("tesseract", "reads the screen", "sudo apt install tesseract-ocr"),
    ("pw-record", "records the mic", "sudo apt install pipewire-bin"),
    ("gio", "opens apps and files", "sudo apt install libglib2.0-bin"),
)


async def programs() -> list[Check]:
    out: list[Check] = []
    for name, purpose, install in PROGRAMS:
        found = shutil.which(name) is not None
        detail = purpose if found else f"missing (it {purpose})"
        out.append(Check(name, found, detail, "" if found else install))
    return out


CHECKS: tuple[Callable[[], Awaitable[Check | list[Check]]], ...] = (
    shell_extension,
    daemon,
    chrome,
    keys,
    programs,
)


async def run_all() -> list[Check]:
    """Every check, side by side; one that crashes reports itself as failed."""
    results = await asyncio.gather(*(check() for check in CHECKS), return_exceptions=True)
    out: list[Check] = []
    for check, result in zip(CHECKS, results, strict=True):
        if isinstance(result, BaseException):
            out.append(Check(check.__name__.replace("_", " "), False, f"check failed: {result}"))
        elif isinstance(result, list):
            out.extend(result)
        else:
            out.append(result)
    return out
