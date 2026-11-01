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

from arcee.browser.client import BrowserClient, BrowserError
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


async def shell_extension() -> Check:
    bridge = ShellBridge()
    if not await asyncio.to_thread(bridge.available):
        return Check(
            "GNOME Shell extension",
            False,
            "not running: no glow, screen reading or window focus",
            "install it, then log out and back in",
            fix_action="shell_extension",
        )
    return Check("GNOME Shell extension", True, "running")


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
        return Check(
            "Chrome bridge",
            False,
            "not connected: Arcee starts Chrome when it needs it",
            "if it never connects: register it with Chrome and load the extension",
            advisory=True,
            fix_action="browser_bridge",
        )
    finally:
        await client.aclose()
    return Check("Chrome bridge", True, "connected")


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
