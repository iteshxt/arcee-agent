"""Window and app tools (spec section 11, `window.*` and `app.close`), through the shell.

On Wayland only the compositor can list, raise, minimize or close other apps'
windows, so these go through the Arcee GNOME Shell extension, inside a control
session like any visible action. Closing a window or an app asks first: unsaved
work may be lost (an app may still ask to save, but Arcee does not count on it).
"""

import asyncio
from typing import Any, Final, Literal, Protocol

from pydantic import Field

from arcee.core.policy import Policy, Tier
from arcee.platform.atspi import pick_frame
from arcee.platform.shell import ShellBridge, ShellUnavailableError
from arcee.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolResult, Usage

NO_EXTENSION: Final = "the Arcee shell extension is not running (arcee-cli shell-extension)"


class Windows(Protocol):
    """The shell's windows (the extension; a fake in tests). Calls block."""

    def available(self) -> bool: ...
    def windows(self) -> list[dict[str, Any]]: ...
    def window_action(self, window_id: int, action: str) -> bool: ...
    def tile_window(self, window_id: int, where: str) -> bool: ...
    def move_window(self, window_id: int, monitor: int, workspace: int) -> bool: ...
    def monitors(self) -> dict[str, Any]: ...
    def notifications(self) -> list[dict[str, Any]]: ...


def find_window(wanted: str, windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The window `wanted` names: its id, or its app or title (see pick_frame)."""
    if wanted.isdigit():
        return next((w for w in windows if str(w.get("id")) == wanted), None)
    index = pick_frame(
        wanted, [(str(w.get("app_id", "")), str(w.get("title", ""))) for w in windows]
    )
    return None if index is None else windows[index]


def describe(window: dict[str, Any]) -> str:
    states = [s for s in ("focused", "minimized", "maximized") if window.get(s)]
    flags = f" ({', '.join(states)})" if states else ""
    return f"{window.get('id')} {window.get('app_id') or '?'}: {window.get('title', '')}{flags}"


class _WindowTool[A: ToolArgs](Tool[A]):
    timeout_s = 15

    def __init__(self, shell: Windows | None = None) -> None:
        self._shell: Windows = shell or ShellBridge()

    async def _windows(self) -> list[dict[str, Any]]:
        if not await asyncio.to_thread(self._shell.available):
            raise ShellUnavailableError(NO_EXTENSION)
        return await asyncio.to_thread(self._shell.windows)


class WindowList(_WindowTool[NoArgs]):
    name = "window.list"
    read_only = True
    description = "List the open windows: id, app, title, and which is focused or minimized."
    usage = Usage(
        when="You need to know which windows are open before focusing, arranging or closing one.",
        example="window.list()",
    )
    args_model = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        try:
            windows = await self._windows()
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        lines = [describe(w) for w in windows]
        return ToolResult.ok(f"{len(windows)} windows open", data={"windows": lines})


class WindowArgs(ToolArgs):
    window: str = Field(description="Window id from window.list, or its app or title")
    action: Literal["focus", "minimize", "maximize", "fullscreen", "restore", "close"]


class WindowAct(_WindowTool[WindowArgs]):
    name = "window.act"
    description = (
        "Bring a window to the front, minimize, maximize, make it full screen, restore or close it."
    )
    usage = Usage(
        when="The user asks to show, hide, enlarge or close a window.",
        avoid="Quitting a whole app with all its windows: app.close.",
        mistakes="Asking the user which window or for its id: the app's name is enough "
        '(window="Chrome"), or window.list shows them. Closing a window that may hold unsaved '
        "work without being asked to.",
        example='window.act(window="Calculator", action="minimize")',
    )
    args_model = WindowArgs

    def tier_for(self, args: WindowArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM if args.action == "close" else Tier.AUTO

    def sentence(self, args: WindowArgs) -> str:
        return f"Close the window “{args.window}”?"

    async def run(self, args: WindowArgs, ctx: ToolContext) -> ToolResult:
        try:
            window = find_window(args.window, await self._windows())
            if window is None:
                return ToolResult.fail(f"no open window matches {args.window!r}; see window.list")
            done = await asyncio.to_thread(
                self._shell.window_action, int(window["id"]), args.action
            )
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        if not done:
            return ToolResult.fail(f"the window {window.get('title')!r} is gone")
        past = {
            "focus": "Focused",
            "close": "Closed",
            "restore": "Restored",
            "fullscreen": "Made full screen:",
        }.get(args.action, f"{args.action.capitalize()}d")
        return ToolResult.ok(f"{past} {window.get('title')}", data={"window": describe(window)})


class AppCloseArgs(ToolArgs):
    name: str = Field(description="App name or id, e.g. Calculator or org.gnome.Calculator")


class AppClose(_WindowTool[AppCloseArgs]):
    name = "app.close"
    tier = Tier.CONFIRM
    description = "Close every window of an app, as its close buttons would."
    usage = Usage(
        when="The user asks to close or quit an app.",
        avoid="One window of several: window.act with close.",
        example='app.close(name="Calculator")',
    )
    args_model = AppCloseArgs

    def sentence(self, args: AppCloseArgs) -> str:
        return f"Close every {args.name} window?"

    async def run(self, args: AppCloseArgs, ctx: ToolContext) -> ToolResult:
        wanted = args.name.lower()
        try:
            windows = await self._windows()
            mine = [w for w in windows if wanted in str(w.get("app_id", "")).lower()]
            if not mine and (one := find_window(args.name, windows)) is not None:
                # Named by a window title: the app that window belongs to.
                mine = [w for w in windows if w.get("app_id") == one.get("app_id")]
            if not mine:
                return ToolResult.fail(f"no open window belongs to {args.name!r}; see window.list")
            for window in mine:
                await asyncio.to_thread(self._shell.window_action, int(window["id"]), "close")
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(
            f"Closed {len(mine)} {args.name} window{'s' if len(mine) > 1 else ''}",
            data={"closed": [describe(w) for w in mine]},
        )


# --- window.tile, window.move ---------------------------------------------------------------

Place = Literal[
    "left",
    "right",
    "top",
    "bottom",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "center",
]


class TileArgs(ToolArgs):
    window: str = Field(description="Window id from window.list, or its app or title")
    where: Place = Field(description="left or right half, a quarter, top/bottom, or center")


class WindowTile(_WindowTool[TileArgs]):
    name = "window.tile"
    description = "Put a window in the left or right half of its screen, a quarter, or the middle."
    usage = Usage(
        when='"Put Chrome on the left and the terminal on the right": one call each.',
        example='window.tile(window="Chrome", where="left")',
    )
    args_model = TileArgs

    async def run(self, args: TileArgs, ctx: ToolContext) -> ToolResult:
        try:
            window = find_window(args.window, await self._windows())
            if window is None:
                return ToolResult.fail(f"no open window matches {args.window!r}; see window.list")
            done = await asyncio.to_thread(self._shell.tile_window, int(window["id"]), args.where)
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        if not done:
            return ToolResult.fail(f"the window {window.get('title')!r} is gone")
        return ToolResult.ok(
            f"Put {window.get('title')} on the {args.where}", data={"window": describe(window)}
        )


class MoveArgs(ToolArgs):
    window: str = Field(description="Window id from window.list, or its app or title")
    monitor: int | None = Field(
        default=None, ge=1, le=8, description="Screen number, 1 the first (window.screens)"
    )
    workspace: int | None = Field(default=None, ge=1, le=32, description="Workspace, 1 the first")


class WindowMove(_WindowTool[MoveArgs]):
    name = "window.move"
    description = "Move a window to another screen (monitor) or workspace."
    usage = Usage(
        when='"Open Files on my second screen" (app.open, then this), "move Spotify to '
        'workspace 2".',
        example='window.move(window="Files", monitor=2)',
    )
    args_model = MoveArgs

    async def run(self, args: MoveArgs, ctx: ToolContext) -> ToolResult:
        if args.monitor is None and args.workspace is None:
            return ToolResult.fail("say a monitor or a workspace to move it to")
        monitor = args.monitor - 1 if args.monitor else -1
        workspace = args.workspace - 1 if args.workspace else -1
        try:
            window = find_window(args.window, await self._windows())
            if window is None:
                return ToolResult.fail(f"no open window matches {args.window!r}; see window.list")
            done = await asyncio.to_thread(
                self._shell.move_window, int(window["id"]), monitor, workspace
            )
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        if not done:
            return ToolResult.fail(f"the window {window.get('title')!r} is gone")
        where = " and ".join(
            f"{name} {n}"
            for name, n in (("screen", args.monitor), ("workspace", args.workspace))
            if n
        )
        return ToolResult.ok(
            f"Moved {window.get('title')} to {where}", data={"window": describe(window)}
        )


class WindowScreens(_WindowTool[NoArgs]):
    name = "window.screens"
    read_only = True
    description = "The screens (monitors) connected, which is the main one, and the workspaces."
    usage = Usage(
        when='"Which is my second screen?", before window.move.', example="window.screens()"
    )
    args_model = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        try:
            if not await asyncio.to_thread(self._shell.available):
                raise ShellUnavailableError(NO_EXTENSION)
            found = await asyncio.to_thread(self._shell.monitors)
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        screens = [
            f"screen {m['index'] + 1}: {m['width']}x{m['height']}"
            + (f" {m['connector']}" if m.get("connector") else "")
            + (" (main)" if m.get("primary") else "")
            for m in found.get("monitors", [])
        ]
        data = {
            "screens": screens,
            "workspaces": found.get("workspaces"),
            "current_workspace": int(found.get("active_workspace", 0)) + 1,
        }
        return ToolResult.ok(f"{len(screens)} screens", data=data)


# --- notifications.list ---------------------------------------------------------------------


class NotificationsList(_WindowTool[NoArgs]):
    name = "notifications.list"
    read_only = True
    untrusted = True  # other apps' words
    personal = "your notifications"
    description = "The notifications waiting in GNOME's message tray: what the user missed."
    usage = Usage(
        when='"What notifications did I miss?", "did anyone message me?".',
        example="notifications.list()",
    )
    args_model = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        from datetime import datetime

        try:
            if not await asyncio.to_thread(self._shell.available):
                raise ShellUnavailableError(NO_EXTENSION)
            found = await asyncio.to_thread(self._shell.notifications)
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        found.sort(key=lambda n: n.get("time", 0), reverse=True)
        lines: list[str] = []
        for n in found[:40]:
            when = datetime.fromtimestamp(n["time"]).strftime("%a %H:%M") if n.get("time") else ""
            body = str(n.get("body", "")).replace("\n", " ")[:200]
            lines.append(
                f"{when} {n.get('app', '')}: {n.get('title', '')}" + (f" — {body}" if body else "")
            )
        if not lines:
            return ToolResult.ok("No notifications waiting", data={"notifications": []})
        return ToolResult.ok(f"{len(found)} notifications waiting", data={"notifications": lines})


def window_tools(shell: Windows | None = None) -> list[Tool[Any]]:
    """The window tools, sharing one shell connection."""
    bridge: Windows = shell or ShellBridge()
    return [
        WindowList(bridge),
        WindowAct(bridge),
        WindowTile(bridge),
        WindowMove(bridge),
        WindowScreens(bridge),
        AppClose(bridge),
        NotificationsList(bridge),
    ]
