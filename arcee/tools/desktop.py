"""Desktop app tools (spec section 11, `ui.*`): read and act in app windows by their tree.

`ui.tree` numbers a window's buttons, menu items, fields and list entries
(`el_N`) and lists the window's own actions (GTK apps expose many: Files has
`view.rename`); `ui.act` acts on them. Clicking anything labelled delete, send,
discard and the like, or triggering such a window action, needs confirmation.
Window contents are untrusted: apps show web pages, mail and documents.
"""

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Protocol

from pydantic import Field

from arcee.core.policy import Policy, Tier
from arcee.platform.atspi import Snapshot
from arcee.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolResult, Usage

RISKY: Final = re.compile(
    r"\b(delete|trash|remove|erase|discard|send|submit|pay|buy|purchase|format|uninstall|"
    r"empty|reset|sign out|log ?out|shut ?down|restart|overwrite|replace)\b",
    re.IGNORECASE,
)


class Desktop(Protocol):
    """The accessibility backend (AT-SPI on GNOME; a fake in tests)."""

    async def windows(self) -> list[dict[str, Any]]: ...
    async def snapshot(self, app: str | None, limit: int) -> Snapshot: ...
    async def act(self, target: str, action: str, value: str | None) -> str: ...


@dataclass
class DesktopSession:
    desktop: Desktop
    elements: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])


class _DesktopTool[A: ToolArgs](Tool[A]):
    untrusted: ClassVar[bool] = True
    timeout_s = 20

    def __init__(self, session: DesktopSession) -> None:
        self.session = session


class UiWindows(_DesktopTool[NoArgs]):
    name = "ui.windows"
    read_only = True
    description = "List the open app windows: app, title, and which one is active."
    usage = Usage(when="You need to know which apps are open before reading one.")
    args_model = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        try:
            windows = await self.session.desktop.windows()
        except Exception as exc:  # the accessibility bus can be missing or slow
            return ToolResult.fail(f"could not list windows: {exc}")
        return ToolResult.ok(f"{len(windows)} windows open", data={"windows": windows})


class TreeArgs(ToolArgs):
    app: str = Field(
        default="", description="App or window title to read; empty for the active one"
    )
    limit: int = Field(default=60, ge=10, le=150)


class UiTree(_DesktopTool[TreeArgs]):
    name = "ui.tree"
    read_only = True
    description = (
        "Read an app window: its buttons, menu items, fields, list and table entries, each "
        "numbered el_N, plus the window's own actions (like view.rename) for ui.act."
    )
    usage = Usage(
        when="Before acting in a desktop app (Files, Settings, Text Editor...), and after "
        "anything changed the window.",
        mistakes="Reusing el_N numbers from an older reading.",
        example='ui.tree(app="Files")',
    )
    args_model = TreeArgs
    observation_kind = "desktop_tree"

    async def run(self, args: TreeArgs, ctx: ToolContext) -> ToolResult:
        try:
            snap = await self.session.desktop.snapshot(args.app or None, args.limit)
        except LookupError as exc:
            return ToolResult.fail(str(exc))
        self.session.elements = {e["id"]: e for e in snap.elements}
        data = {
            "app": snap.app,
            "window": snap.window,
            "count": snap.count,
            "elements": snap.elements,
            "window_actions": snap.window_actions,
        }
        return ToolResult.ok(f"{snap.window or snap.app}: {len(snap.elements)} elements", data=data)


class ActArgs(ToolArgs):
    target: str = Field(
        pattern=r"^(el_\d+|window)$", description="An el_N from ui.tree, or 'window'"
    )
    action: str = Field(
        description="click, activate, select, focus or set_value for elements; for 'window', "
        "one of the window_actions from ui.tree"
    )
    value: str | None = Field(default=None, description="Text for set_value")


class UiAct(_DesktopTool[ActArgs]):
    name = "ui.act"
    description = (
        "Act in an app window: click, activate, select, focus or set the text of an element "
        "from ui.tree, or run one of the window's actions."
    )
    usage = Usage(
        when="You read the window with ui.tree and know what to press, pick or type.",
        mistakes="Clicking a list entry to select it: use select; typing with click: use "
        "set_value.",
        example='ui.act(target="el_5", action="select")',
    )
    args_model = ActArgs
    observation_kind = "desktop_tree"

    def tier_for(self, args: ActArgs, policy: Policy) -> Tier:
        label = args.action if args.target == "window" else _label(self.session, args.target)
        return (
            Tier.CONFIRM if RISKY.search(label.replace(".", " ").replace("-", " ")) else Tier.AUTO
        )

    def sentence(self, args: ActArgs) -> str:
        if args.target == "window":
            return f"Run “{args.action}” in the app window?"
        return f"{args.action.title()} “{_label(self.session, args.target) or args.target}”?"

    async def run(self, args: ActArgs, ctx: ToolContext) -> ToolResult:
        if args.action == "set_value" and args.value is None:
            return ToolResult.fail("set_value needs a value")
        try:
            done = await self.session.desktop.act(args.target, args.action, args.value)
        except LookupError as exc:
            return ToolResult.fail(str(exc))
        self.session.elements.clear()
        return ToolResult.ok(done.capitalize(), data={"done": done})


def _label(session: DesktopSession, target: str) -> str:
    return str(session.elements.get(target, {}).get("name", ""))[:80]


def desktop_tools(desktop: Desktop | None = None) -> list[Tool[Any]]:
    if desktop is None:
        from arcee.platform.atspi import AtspiDesktop

        desktop = AtspiDesktop()
    session = DesktopSession(desktop)
    return [UiWindows(session), UiTree(session), UiAct(session)]
