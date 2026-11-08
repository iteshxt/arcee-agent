"""The clipboard, screenshots kept for the user, and recordings of the screen or the mic.

Arcee's own screenshots (screen.look, screen.read) are private: a file in the runtime
directory, replaced by the next one. `screen.save` keeps one where the user's own
screenshots go. Recording the screen goes through GNOME Shell's own recorder (its red
dot shows in the top bar while it runs); it records for as long as the D-Bus connection
that started it stays open, so arceed holds it. Audio is recorded by pw-record, as the
mic button does. Both ask first, and stop by themselves after `max_minutes`.
"""

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, Protocol

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from pydantic import Field

from arcee.core.policy import Policy, Tier
from arcee.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.commands import Commands, SystemCommands

CLIPBOARD_LIMIT: Final = 20_000
TEXT_TYPES: Final = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "TEXT", "STRING")


def stamp(now: datetime | None = None) -> str:
    """As GNOME names its own screenshots: "2026-11-08 19-18-02"."""
    return (now or datetime.now()).strftime("%Y-%m-%d %H-%M-%S")


def _folder(path: str, home: Path, default: str) -> Path:
    """`path` (else `default`), with "~" as the task's home."""
    text = path or default
    if text == "~" or text.startswith("~/"):
        return home / text[2:]
    folder = Path(text)
    return folder if folder.is_absolute() else home / folder


# --- clipboard ----------------------------------------------------------------------------


class ClipboardGet(Tool[NoArgs]):
    name = "clipboard.get"
    description = "What is on the clipboard now: its text, or what kind of thing it is."
    usage = Usage(
        when='"What did I just copy?", "summarise what I copied", "paste it into the email".',
        example="clipboard.get()",
    )
    args_model = NoArgs
    read_only: ClassVar[bool] = True
    untrusted: ClassVar[bool] = True  # copied from anywhere: a page, a message
    personal: ClassVar[str] = "your clipboard"
    timeout_s = 10

    def __init__(self, commands: Commands | None = None) -> None:
        self._commands = commands or SystemCommands()

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        types = await self._commands.run(["wl-paste", "--list-types"], limit_s=5)
        if not types.ok:
            if "nothing is copied" in (types.err + types.out).lower():
                return ToolResult.ok("The clipboard is empty", data={"empty": True})
            return ToolResult.fail(f"could not read the clipboard: {types.why()}")
        offered = [t.strip() for t in types.out.splitlines() if t.strip()]
        text_type = next((t for t in TEXT_TYPES if t in offered), None)
        if text_type is None:
            kind = next((t for t in offered if "/" in t), offered[0] if offered else "unknown")
            return ToolResult.ok(f"The clipboard holds {kind}, not text", data={"type": kind})
        got = await self._commands.run(["wl-paste", "--no-newline", "--type", text_type], limit_s=5)
        if not got.ok:
            return ToolResult.fail(f"could not read the clipboard: {got.why()}")
        text = got.out
        data: dict[str, Any] = {"text": text[:CLIPBOARD_LIMIT], "characters": len(text)}
        if len(text) > CLIPBOARD_LIMIT:
            data["cut_at"] = CLIPBOARD_LIMIT
        return ToolResult.ok(f"Clipboard: {len(text)} characters of text", data=data)


class ClipboardSetArgs(ToolArgs):
    text: str = Field(min_length=1, max_length=100_000, description="Exactly what to copy")


class ClipboardSet(Tool[ClipboardSetArgs]):
    name = "clipboard.set"
    description = "Put text on the clipboard, ready for the user to paste."
    usage = Usage(
        when='"Copy my email address", "copy the code on screen" (screen.read it first), '
        '"put this on the clipboard".',
        example='clipboard.set(text="petri@example.com")',
    )
    args_model = ClipboardSetArgs
    timeout_s = 10

    def __init__(self, commands: Commands | None = None) -> None:
        self._commands = commands or SystemCommands()

    async def run(self, args: ClipboardSetArgs, ctx: ToolContext) -> ToolResult:
        # wl-copy stays behind (it serves the paste) and returns at once.
        done = await self._commands.run(
            ["wl-copy", "--type", "text/plain;charset=utf-8"],
            stdin=args.text.encode(),
            limit_s=5,
            quiet=True,
        )
        if not done.ok:
            return ToolResult.fail(f"could not copy: {done.why()}")
        preview = args.text if len(args.text) <= 60 else args.text[:57] + "…"
        return ToolResult.ok(f"Copied: {preview}", data={"copied_characters": len(args.text)})


# --- screen.save --------------------------------------------------------------------------


class Capture(Protocol):
    async def capture(self) -> Any: ...  # a Shot: .path to a PNG
    async def focus(self, title: str) -> bool: ...


class SaveArgs(ToolArgs):
    window: str = Field(
        default="", description="A window to bring forward first (its app or title)"
    )
    folder: str = Field(default="", description="Where to save it; default ~/Pictures/Screenshots")


class ScreenSave(Tool[SaveArgs]):
    name = "screen.save"
    description = "Take a screenshot and save it as a PNG file for the user."
    usage = Usage(
        when='"Take a screenshot", "screenshot this and save it to Pictures".',
        avoid="Looking at the screen yourself: screen.look and screen.read.",
        example="screen.save()",
    )
    args_model = SaveArgs
    timeout_s = 30

    def __init__(self, screen: Callable[[], Capture] | None = None) -> None:
        self._screen = screen

    def _get(self) -> Capture:
        if self._screen is not None:
            return self._screen()
        from arcee.platform.screen import GnomeScreen

        return GnomeScreen()

    async def run(self, args: SaveArgs, ctx: ToolContext) -> ToolResult:
        screen = self._get()
        if args.window and not await screen.focus(args.window):
            return ToolResult.fail(f"no window matches “{args.window}”")
        try:
            shot = await screen.capture()
        except Exception as exc:
            return ToolResult.fail(f"could not take the screenshot: {exc}")
        folder = _folder(args.folder, ctx.home, "~/Pictures/Screenshots")
        target = folder / f"Screenshot From {stamp()}.png"

        def keep() -> None:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(shot.path, target)

        await asyncio.to_thread(keep)
        return ToolResult.ok(f"Saved {target}", data={"saved": str(target)})


# --- record.screen and record.audio ---------------------------------------------------------


class RecordArgs(ToolArgs):
    action: Literal["start", "stop", "status"] = Field(
        description="start a recording, stop it (it is saved), or status"
    )
    max_minutes: int = Field(default=30, ge=1, le=240, description="Stops by itself after this")
    name: str = Field(default="", description="A name for the file; default the date and time")


class _Recording:
    def __init__(self, path: Path, started: float, stop: Callable[[], Awaitable[None]]) -> None:
        self.path = path
        self.started = started
        self.stop = stop
        self.limit: asyncio.TimerHandle | None = None


class _Recorder(Tool[RecordArgs]):
    """start, stop and status, with one recording at a time per kind."""

    args_model = RecordArgs
    timeout_s = 30
    what: ClassVar[str]
    folder: ClassVar[str]
    suffix: ClassVar[str]

    def __init__(self) -> None:
        self._now: _Recording | None = None

    def tier_for(self, args: RecordArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM if args.action == "start" else Tier.AUTO

    def sentence(self, args: RecordArgs) -> str:
        return f"Start recording {self.what} (for up to {args.max_minutes} minutes)?"

    async def run(self, args: RecordArgs, ctx: ToolContext) -> ToolResult:
        if args.action == "status":
            if self._now is None:
                return ToolResult.ok("Not recording", data={"recording": False})
            seconds = round(time.time() - self._now.started)
            return ToolResult.ok(
                f"Recording {self.what} for {seconds} s",
                data={"recording": True, "seconds": seconds, "file": str(self._now.path)},
            )
        if args.action == "stop":
            return await self._stop()
        if self._now is not None:
            return ToolResult.fail(f"already recording {self.what} to {self._now.path}")
        folder = _folder("", ctx.home, self.folder)
        base = args.name.strip().replace("/", "-") or f"{self.label} {stamp()}"
        path = folder / f"{base}{self.suffix}"
        try:
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
            self._now = await self._start(path)
        except Exception as exc:
            return ToolResult.fail(f"could not start recording: {exc}")
        loop = asyncio.get_running_loop()
        self._now.limit = loop.call_later(
            args.max_minutes * 60, lambda: asyncio.ensure_future(self._stop())
        )
        return ToolResult.ok(
            f"Recording {self.what} to {self._now.path}",
            data={"recording": True, "file": str(self._now.path), "max_minutes": args.max_minutes},
        )

    async def _stop(self) -> ToolResult:
        now, self._now = self._now, None
        if now is None:
            return ToolResult.ok("Nothing was being recorded", data={"recording": False})
        if now.limit is not None:
            now.limit.cancel()
        await now.stop()
        seconds = round(time.time() - now.started)
        return ToolResult.ok(
            f"Saved {seconds} s of {self.what} to {now.path}",
            data={"saved": str(now.path), "seconds": seconds},
        )

    label: ClassVar[str]

    async def _start(self, path: Path) -> _Recording:
        raise NotImplementedError


SCREENCAST: Final = ("org.gnome.Shell.Screencast", "/org/gnome/Shell/Screencast")


class RecordScreen(_Recorder):
    name = "record.screen"
    description = "Record the whole screen to a video in ~/Videos/Screencasts; stop when asked."
    usage = Usage(
        when='"Record my screen", "stop recording".',
        mistakes="Leaving it running: say how to stop it (ask Arcee, or the red dot in the "
        "top bar).",
        example='record.screen(action="start")',
    )
    what = "the screen"
    label = "Screencast From"
    folder = "~/Videos/Screencasts"
    suffix = ".webm"  # the Shell picks the container and says which (mp4 on GNOME 50)

    async def _start(self, path: Path) -> _Recording:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        template = str(path.with_suffix(""))
        reply = await bus.call(
            Message(
                destination=SCREENCAST[0],
                path=SCREENCAST[1],
                interface=SCREENCAST[0],
                member="Screencast",
                signature="sa{sv}",
                body=[template, {"draw-cursor": Variant("b", True)}],
            )
        )
        if reply.message_type is MessageType.ERROR or not reply.body[0]:
            bus.disconnect()
            raise RuntimeError(str(reply.body[0] if reply.body else "GNOME Shell said no"))

        async def stop() -> None:
            with contextlib.suppress(Exception):
                await bus.call(
                    Message(
                        destination=SCREENCAST[0],
                        path=SCREENCAST[1],
                        interface=SCREENCAST[0],
                        member="StopScreencast",
                    )
                )
            bus.disconnect()

        return _Recording(Path(str(reply.body[1])), time.time(), stop)


class RecordAudio(_Recorder):
    name = "record.audio"
    description = "Record from the microphone to a voice memo in ~/Music/Recordings."
    usage = Usage(
        when='"Record a voice memo", "record this meeting\'s audio", "stop recording".',
        example='record.audio(action="start", name="Lecture notes")',
    )
    what = "audio"
    label = "Recording"
    folder = "~/Music/Recordings"
    suffix = ".ogg"

    async def _start(self, path: Path) -> _Recording:
        process = await asyncio.create_subprocess_exec(
            "pw-record",
            "--media-role",
            "Production",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(0.3)
        if process.returncode is not None:
            err = (await process.stderr.read()).decode() if process.stderr else ""
            raise RuntimeError(err.strip() or "pw-record stopped at once")

        async def stop() -> None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()  # pw-record closes the file properly on SIGTERM
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()

        return _Recording(path, time.time(), stop)


def capture_tools(commands: Commands | None = None) -> list[Tool[Any]]:
    return [
        ClipboardGet(commands),
        ClipboardSet(commands),
        ScreenSave(),
        RecordScreen(),
        RecordAudio(),
    ]
