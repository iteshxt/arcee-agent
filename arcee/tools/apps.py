"""App tools (spec section 11): `app.list` and `app.open`.

Installed apps are read from the `.desktop` files in the XDG data directories,
which cover apt, snap and flatpak installs, and launched with `gio launch` on
the matching file. This is what `Gio.DesktopAppInfo` does, without needing the
GObject bindings in the type-checked core.
"""

import asyncio
import configparser
import os
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import Field

from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage

MAX_LISTED: Final = 25
LAUNCH_CHECK_S: Final = 1.0


@dataclass(frozen=True, slots=True)
class App:
    id: str
    name: str
    comment: str
    keywords: str
    path: Path

    def score(self, query: str) -> float:
        """How well `query` names this app: exact name or id first, then words, then fuzzy."""
        q = query.lower().strip()
        name, ident = self.name.lower(), self.id.lower().removesuffix(".desktop")
        if q in (name, ident) or ident.endswith(f".{q}") or ident.endswith(f"_{q}"):
            return 1.0
        if q in name or q in ident:
            return 0.8
        if q in self.keywords.lower() or q in self.comment.lower():
            return 0.6
        return 0.5 * SequenceMatcher(None, q, name).ratio()


def application_dirs() -> list[Path]:
    """Where desktop files live, highest priority first, as the XDG spec orders them."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(d) / "applications" for d in [data_home, *data_dirs.split(":")] if d]


def installed_apps(dirs: list[Path]) -> list[App]:
    """Visible apps, first definition of each id winning."""
    apps: dict[str, App] = {}
    for folder in dirs:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.desktop")):
            app_id = str(path.relative_to(folder)).replace("/", "-")
            if app_id in apps:
                continue
            if (app := _read(path, app_id)) is not None:
                apps[app_id] = app
    return sorted(apps.values(), key=lambda a: a.name.lower())


def _read(path: Path, app_id: str) -> App | None:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error, UnicodeDecodeError:
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    entry = parser["Desktop Entry"]
    hidden = entry.get("NoDisplay", "false") == "true" or entry.get("Hidden", "false") == "true"
    if entry.get("Type") != "Application" or hidden or not entry.get("Name"):
        return None
    return App(
        id=app_id,
        name=entry["Name"],
        comment=entry.get("Comment", ""),
        keywords=entry.get("Keywords", ""),
        path=path,
    )


def best_match(apps: list[App], query: str) -> App | None:
    scored = max(apps, key=lambda a: a.score(query), default=None)
    return scored if scored is not None and scored.score(query) >= 0.6 else None


class ListArgs(ToolArgs):
    query: str = Field(default="", description="Part of an app's name or what it does")


class AppList(Tool[ListArgs]):
    name = "app.list"
    read_only = True
    description = "Find installed apps by name or purpose; returns their names and ids."
    usage = Usage(
        when="You need to know whether an app is installed, or its exact name to open it.",
        avoid="You already know the app's name: call app.open directly.",
        example='app.list(query="music")',
    )
    args_model = ListArgs
    timeout_s = 10

    def __init__(self, *, dirs: list[Path] | None = None) -> None:
        self._dirs = dirs

    async def run(self, args: ListArgs, ctx: ToolContext) -> ToolResult:
        apps = await asyncio.to_thread(installed_apps, self._dirs or application_dirs())
        if args.query:
            apps = sorted(
                (a for a in apps if a.score(args.query) >= 0.6),
                key=lambda a: -a.score(args.query),
            )
        shown = [{"name": a.name, "id": a.id, "about": a.comment} for a in apps[:MAX_LISTED]]
        data = {"query": args.query, "count": len(apps), "apps": shown}
        return ToolResult.ok(f"{len(apps)} installed apps match {args.query!r}", data=data)


class Windows(Protocol):
    """Open windows and focusing one (the Arcee shell extension; a fake in tests)."""

    def windows(self) -> list[dict[str, Any]]: ...
    def focus_window(self, window_id: int) -> bool: ...


class OpenAppArgs(ToolArgs):
    name: str = Field(description="App name or id, e.g. Firefox or org.gnome.Calculator")
    args: list[str] = Field(default_factory=list[str], description="Files or URLs to open in it")


class AppOpen(Tool[OpenAppArgs]):
    name = "app.open"
    description = "Start an installed app by name, optionally opening files or URLs in it."
    usage = Usage(
        when="The user asked to open or start an application.",
        avoid="Opening a file in whatever app handles it: use fs.open_with_default.",
        mistakes="Guessing a command name for shell.run instead; this finds the real app.",
        example='app.open(name="Calculator")',
    )
    args_model = OpenAppArgs
    observation_kind = "screenshot_auto"
    timeout_s = 15

    def __init__(
        self,
        *,
        dirs: list[Path] | None = None,
        command: tuple[str, ...] = ("gio", "launch"),
        windows: Windows | None = None,
    ) -> None:
        self._dirs = dirs
        self._command = command
        self._windows = windows

    async def run(self, args: OpenAppArgs, ctx: ToolContext) -> ToolResult:
        apps = await asyncio.to_thread(installed_apps, self._dirs or application_dirs())
        app = best_match(apps, args.name)
        if app is None:
            return ToolResult.fail(f"no installed app matches {args.name!r}; try app.list")
        # Already open: bring its window forward. Launching again opened a second
        # Calculator and left the model reading the wrong one.
        if not args.args and await self._focus_open(app):
            return ToolResult.ok(
                f"Switched to the open {app.name} window",
                data={"opened": app.name, "id": app.id, "already_open": True},
            )
        proc = await asyncio.create_subprocess_exec(
            *self._command,
            str(app.path),
            *args.args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            # Some launchers stay attached to the app; a second is enough to see a failure.
            _, err = await asyncio.wait_for(proc.communicate(), timeout=LAUNCH_CHECK_S)
        except TimeoutError:
            err = b""
        if proc.returncode not in (0, None):
            return ToolResult.fail(
                err.decode(errors="replace").strip() or f"{app.name} did not start"
            )
        data = {"opened": app.name, "id": app.id}
        return ToolResult.ok(f"Opened {app.name}", data=data)

    async def _focus_open(self, app: App) -> bool:
        if self._windows is None:
            from arcee.platform.shell import ShellBridge

            self._windows = ShellBridge()
        windows = self._windows
        wanted = app.id.removesuffix(".desktop").lower()

        def focus() -> bool:
            try:
                for window in windows.windows():
                    if str(window.get("app_id", "")).lower() == wanted:
                        return windows.focus_window(int(window["id"]))
            except Exception:  # no extension or no session bus: just launch
                return False
            return False

        return await asyncio.to_thread(focus)
