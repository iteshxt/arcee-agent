"""App tools (spec section 11): `app.list` and `app.open`.

Installed apps are read from the `.desktop` files in the XDG data directories,
which cover apt, snap and flatpak installs, and launched with `gio launch` on
the matching file. This is what `Gio.DesktopAppInfo` does, without needing the
GObject bindings in the type-checked core.
"""

import asyncio
import configparser
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import Field

from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage

MAX_LISTED: Final = 25
# Ids a browser makes for its web apps: chrome-<32 letters>-Default, msedge-<…>-Default.
_GENERATED_ID: Final = re.compile(r"(^|-)[a-p]{32}(-|$)")
LAUNCH_CHECK_S: Final = 1.0
WINDOW_WAIT_S: Final = 10.0  # app.open returns when the window shows, or after this long
WINDOW_POLL_S: Final = 0.25


@dataclass(frozen=True, slots=True)
class App:
    id: str
    name: str
    comment: str
    keywords: str
    path: Path

    def score(self, query: str) -> float:
        """How well `query` names this app: exact name or id first, then a word of its name,
        then part of its name or id, then what it does, then fuzzy.

        The name counts before the id: a Chrome web app's id is "chrome-<code>-Default"
        whatever it is called, and "Chrome" opened one (Cinemode, 2026-11-04) because
        "chrome" was in its id and the tie went to the first name alphabetically. Such
        made-up ids are not matched at all; the app is found by its own name.
        """
        q = query.lower().strip()
        name, ident = self.name.lower(), self.id.lower().removesuffix(".desktop")
        generated = bool(_GENERATED_ID.search(ident))
        if q == name:
            return 1.0
        # "google-chrome", "org.gnome.Calculator": below an exact name ("Settings" is
        # GNOME's, not nvidia-settings).
        if not generated and (q == ident or any(ident.endswith(c + q) for c in ".-_")):
            return 0.95
        if q in re.findall(r"\w+", name):
            return 0.9
        if q in name:
            return 0.8
        if q in ident and not generated:
            return 0.7
        keywords = self.keywords.lower()
        # "VS Code" is the keyword "vscode".
        if q in keywords or q.replace(" ", "") in keywords.split(";") or q in self.comment.lower():
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
        window_wait_s: float = WINDOW_WAIT_S,
    ) -> None:
        self._dirs = dirs
        self._command = command
        self._windows = windows
        self._window_wait_s = window_wait_s

    def sentence(self, args: OpenAppArgs) -> str:
        return f"Open {args.name}?"

    def preview(self, args: OpenAppArgs) -> tuple[str, ...]:
        return tuple(args.args)

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
        before = await self._window_list() if self._window_wait_s > 0 else None
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
        data: dict[str, Any] = {"opened": app.name, "id": app.id}
        # Files for an app that is already open usually land in its existing window
        # (a new tab), so there is no new window to wait for.
        if before is None or (args.args and any(_is(app, w) for w in before)):
            return ToolResult.ok(f"Opened {app.name}", data=data)
        window = await self._new_window(app, {w.get("id") for w in before})
        if window is None:
            data["window_shown"] = False
            return ToolResult.ok(
                f"Started {app.name}; no window yet after {self._window_wait_s:g} s "
                "(it may still be loading; control.wait_for can wait longer)",
                data=data,
            )
        data |= {"window_shown": True, "window": window.get("id"), "title": window.get("title")}
        return ToolResult.ok(
            f"Opened {app.name}: “{window.get('title', '')}” is showing", data=data
        )

    def _bridge(self) -> Windows:
        if self._windows is None:
            from arcee.platform.shell import ShellBridge

            self._windows = ShellBridge()
        return self._windows

    async def _window_list(self) -> list[dict[str, Any]] | None:
        """The open windows, or None when they cannot be seen (no extension, no session)."""
        windows = self._bridge()

        def read() -> list[dict[str, Any]] | None:
            try:
                return windows.windows()
            except Exception:
                return None

        return await asyncio.to_thread(read)

    async def _new_window(self, app: App, known: set[Any]) -> dict[str, Any] | None:
        """Wait for a window that was not open before the launch: the app's own first,
        any new one if it is the only thing that appeared (Flatpak and Chrome apps
        report ids that differ from their .desktop file)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._window_wait_s
        while True:
            now = await self._window_list() or []
            fresh = [w for w in now if w.get("id") not in known]
            if fresh:
                return next((w for w in fresh if _is(app, w)), fresh[0])
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(WINDOW_POLL_S)

    async def _focus_open(self, app: App) -> bool:
        windows = self._bridge()

        def focus() -> bool:
            try:
                for window in windows.windows():
                    if _is(app, window):
                        return windows.focus_window(int(window["id"]))
            except Exception:  # no extension or no session bus: just launch
                return False
            return False

        return await asyncio.to_thread(focus)


def _is(app: App, window: dict[str, Any]) -> bool:
    """Whether `window` belongs to `app` (GNOME's app id is the .desktop file's name)."""
    return str(window.get("app_id", "")).lower() == app.id.removesuffix(".desktop").lower()
