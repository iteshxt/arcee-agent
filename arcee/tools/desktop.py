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

from pydantic import Field, field_validator

from arcee.core.policy import Policy, Tier
from arcee.platform.atspi import Snapshot
from arcee.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolResult, Usage

RISKY: Final = re.compile(
    r"\b(delete|trash|remove|erase|discard|send|submit|pay|buy|purchase|format|uninstall|"
    r"empty|reset|sign out|log ?out|shut ?down|restart|overwrite|replace)\b",
    re.IGNORECASE,
)


# Fewer elements than this and the tree is unlikely to show what the user sees.
SPARSE_TREE: Final = 5
# Elements read at least: one line each, cheap enough to read a whole window at once.
MIN_TREE: Final = 100
# Text fields reported after an action.
MAX_SHOWN: Final = 8


class Desktop(Protocol):
    """The accessibility backend (AT-SPI on GNOME; a fake in tests)."""

    async def windows(self) -> list[dict[str, Any]]: ...
    async def snapshot(self, app: str | None, limit: int) -> Snapshot: ...
    async def act(self, target: str, action: str, value: str | None) -> str: ...


@dataclass
class DesktopSession:
    desktop: Desktop
    elements: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    # The app last read with ui.tree, read again after each action.
    app: str | None = None


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
    limit: int = Field(default=MIN_TREE, ge=10, le=150)


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
            # Never fewer than MIN_TREE: a small read missed the buttons and was read again.
            snap = await self.session.desktop.snapshot(args.app or None, max(args.limit, MIN_TREE))
        except LookupError as exc:
            return ToolResult.fail(str(exc))
        self.session.elements = {e["id"]: e for e in snap.elements}
        self.session.app = snap.app
        data = {
            "app": snap.app,
            "window": snap.window,
            "count": snap.count,
            # One line per element, like browser.get_elements: as objects, Calculator's
            # 62 buttons filled most of a call to the rate-limited model.
            "elements": [tree_line(e) for e in snap.elements],
            "window_actions": snap.window_actions,
        }
        if len(snap.elements) < SPARSE_TREE:
            # Canvases, games and apps without accessibility show little here; the
            # screen itself still has their text.
            title = snap.window or snap.app
            data["hint"] = (
                f"This window's tree shows little. To see it as on screen: "
                f'screen.read(window="{title}"), then screen.click.'
            )
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

    @field_validator("action")
    @classmethod
    def _plain_set_value(cls, action: str) -> str:
        # Models write set-text, set_text, type...: all mean set_value. Left as they were,
        # set-text reached an element as an action name it did not have.
        return SET_VALUE_ALIASES.get(action.strip().lower(), action.strip())


# Names models use for setting an element's text or value.
SET_VALUE_ALIASES: Final[dict[str, str]] = dict.fromkeys(
    ("set-text", "set_text", "settext", "set-value", "setvalue", "type", "fill"), "set_value"
)


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
        data: dict[str, Any] = {"done": done}
        data.update(await self._after())
        return ToolResult.ok(done.capitalize(), data=data)

    async def _after(self) -> dict[str, Any]:
        """What the window shows now: its text fields, read again with no model call.

        Told to work out 2 plus 2 in Calculator, the model could not see that its
        clicks had landed, read the window again and again, and pressed + twice. The
        fresh reading also keeps the labels behind el_N current, so a risky click is
        judged by what is really there.
        """
        before = [(i, e.get("name")) for i, e in self.session.elements.items()]
        try:
            snap = await self.session.desktop.snapshot(self.session.app, MIN_TREE)
        except LookupError:
            self.session.elements.clear()
            return {"note": "The window closed or changed; see ui.windows."}
        self.session.elements = {e["id"]: e for e in snap.elements}
        after: dict[str, Any] = {
            "shows": [tree_line(e) for e in snap.elements if e.get("value")][:MAX_SHOWN]
        }
        if [(i, e.get("name")) for i, e in self.session.elements.items()] != before:
            after["note"] = (
                "The window changed and its elements were numbered again: read it with "
                "ui.tree before using an el_N."
            )
        return after


def tree_line(element: dict[str, Any]) -> str:
    """`el_5 push button: 7 [click]`, with a value, states and actions only when set."""
    line = f"{element.get('id')} {element.get('role')}: {element.get('name', '')}"
    if element.get("value"):
        line += f' value="{element["value"]}"'
    if element.get("states"):
        line += f" ({', '.join(element['states'])})"
    if element.get("actions"):
        line += f" [{', '.join(element['actions'])}]"
    return line


def _label(session: DesktopSession, target: str) -> str:
    return str(session.elements.get(target, {}).get("name", ""))[:80]


def desktop_tools(desktop: Desktop | None = None) -> list[Tool[Any]]:
    if desktop is None:
        from arcee.platform.atspi import AtspiDesktop

        desktop = AtspiDesktop()
    session = DesktopSession(desktop)
    return [UiWindows(session), UiTree(session), UiAct(session)]
