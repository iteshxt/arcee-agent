"""Screen and keyboard tools (spec section 11, `screen.*` and `kbd.*`; decision 0009).

The fallback when neither the page (browser.*) nor the window tree (ui.*) can be
read: `screen.read` reads the screen with OCR and numbers each line of text
(`t_N`) with its position; `screen.click` clicks one, or a point. Typing goes to
the focused window. Clicking a point with no text, or text like delete, send or
pay, and pressing Enter, Delete or a window-closing shortcut need confirmation.
"""

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal, Protocol

from pydantic import Field, model_validator

from arcee.core.policy import Policy, Tier
from arcee.platform.screen import Shot, TextLine, keyvals
from arcee.platform.shell import ShellUnavailableError
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.desktop import RISKY

MAX_LINES: Final = 150
BUTTONS: Final = {"left": 1, "middle": 2, "right": 3}
# Keys whose press can submit, delete or close something.
RISKY_KEYS: Final = frozenset({"return", "enter", "delete"})
RISKY_COMBOS: Final = frozenset(
    {"ctrl+w", "ctrl+q", "alt+f4", "ctrl+shift+w", "ctrl+shift+q", "shift+delete", "super+l"}
)


class Screen(Protocol):
    """Screenshots, OCR and input (the GNOME extension; a fake in tests)."""

    async def capture(self) -> Shot: ...
    async def read(self, shot: Shot) -> list[TextLine]: ...
    async def focus(self, title: str) -> bool: ...
    async def click(self, x: int, y: int, button: int, count: int) -> None: ...
    async def scroll(self, x: int, y: int, steps: int) -> None: ...
    async def keys(self, combo: list[int]) -> None: ...
    async def type(self, text: str) -> None: ...


@dataclass
class ScreenSession:
    screen: Screen
    # The last reading: t_N -> its line. Cleared by anything that may move the text.
    lines: dict[str, TextLine] = field(default_factory=dict[str, TextLine])
    size: tuple[int, int] = (0, 0)


class _ScreenTool[A: ToolArgs](Tool[A]):
    timeout_s = 40

    def __init__(self, session: ScreenSession) -> None:
        self.session = session


class ReadArgs(ToolArgs):
    window: str = Field(
        default="", description="Bring the window with this title forward first; empty: as is"
    )


class ScreenRead(_ScreenTool[ReadArgs]):
    name = "screen.read"
    read_only = True
    untrusted: ClassVar[bool] = True
    description = (
        "Read the whole screen as text: each line numbered t_N with its position [x,y], for "
        "screen.click. For apps whose window tree (ui.tree) shows too little."
    )
    usage = Usage(
        when="ui.tree cannot see what you need (a game, a canvas, a remote desktop, an app "
        "without accessibility), or you need what is on screen right now.",
        avoid="Web pages: browser.get_elements. Desktop apps with a tree: ui.tree.",
        mistakes="Reusing t_N numbers after a click or typing changed the screen.",
        example='screen.read(window="Calculator")',
    )
    args_model = ReadArgs

    def changes_things(self, args: ReadArgs) -> bool:
        return bool(args.window)  # bringing a window forward changes the screen

    async def run(self, args: ReadArgs, ctx: ToolContext) -> ToolResult:
        try:
            if args.window and not await self.session.screen.focus(args.window):
                return ToolResult.fail(f"no window titled {args.window!r}; see ui.windows")
            shot = await self.session.screen.capture()
            lines = (await self.session.screen.read(shot))[:MAX_LINES]
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        self.session.lines = {f"t_{i}": line for i, line in enumerate(lines, start=1)}
        self.session.size = (shot.screen_width, shot.screen_height)
        data = {
            "screen": f"{shot.screen_width}x{shot.screen_height}",
            "lines": [
                f"{target} [{line.center[0]},{line.center[1]}] {line.text}"
                for target, line in self.session.lines.items()
            ],
        }
        return ToolResult.ok(f"Screen: {len(lines)} lines of text", data=data)


class ClickArgs(ToolArgs):
    target: str | None = Field(
        default=None, pattern=r"^t_\d+$", description="A t_N from screen.read"
    )
    x: int | None = Field(default=None, ge=0, description="Or a point: x in screen pixels")
    y: int | None = Field(default=None, ge=0)
    button: Literal["left", "right", "middle"] = "left"
    double: bool = False

    @model_validator(mode="after")
    def _one_place(self) -> ClickArgs:
        if (self.target is None) == (self.x is None or self.y is None):
            raise ValueError("give a target t_N, or both x and y")
        return self


class ScreenClick(_ScreenTool[ClickArgs]):
    name = "screen.click"
    description = "Click a line of text from screen.read (t_N), or a point (x, y) on the screen."
    usage = Usage(
        when="You read the screen with screen.read and know what to click.",
        avoid="Pages and apps with a tree: browser.click or ui.act are surer.",
        mistakes="Clicking a t_N from before the screen changed; read again first.",
        example='screen.click(target="t_12")',
    )
    args_model = ClickArgs

    def tier_for(self, args: ClickArgs, policy: Policy) -> Tier:
        # A bare point says nothing about what it will press.
        if args.target is None:
            return Tier.CONFIRM
        return Tier.CONFIRM if RISKY.search(_text(self.session, args.target)) else Tier.AUTO

    def sentence(self, args: ClickArgs) -> str:
        if args.target is None:
            return f"Click at ({args.x}, {args.y}) on the screen?"
        return f"Click “{_text(self.session, args.target) or args.target}” on the screen?"

    async def run(self, args: ClickArgs, ctx: ToolContext) -> ToolResult:
        if args.target is not None:
            line = self.session.lines.get(args.target)
            if line is None:
                return ToolResult.fail(f"{args.target} is not from the latest screen.read")
            (x, y), label = line.center, line.text
        else:
            assert args.x is not None
            assert args.y is not None
            x, y, label = args.x, args.y, f"({args.x}, {args.y})"
        count = 2 if args.double else 1
        try:
            await self.session.screen.click(x, y, BUTTONS[args.button], count)
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        self.session.lines.clear()
        return ToolResult.ok(f"Clicked {label}", data={"clicked": label, "at": [x, y]})


class ScrollArgs(ToolArgs):
    direction: Literal["up", "down"] = "down"
    steps: int = Field(default=5, ge=1, le=30, description="Wheel steps")
    target: str | None = Field(
        default=None, pattern=r"^t_\d+$", description="Scroll where this t_N is; else mid-screen"
    )


class ScreenScroll(_ScreenTool[ScrollArgs]):
    name = "screen.scroll"
    description = "Scroll with the mouse wheel over a line from screen.read, or mid-screen."
    usage = Usage(when="What you need is above or below what screen.read showed.")
    args_model = ScrollArgs

    async def run(self, args: ScrollArgs, ctx: ToolContext) -> ToolResult:
        line = self.session.lines.get(args.target) if args.target else None
        width, height = self.session.size
        x, y = line.center if line else (width // 2, height // 2)
        steps = args.steps if args.direction == "down" else -args.steps
        try:
            await self.session.screen.scroll(x, y, steps)
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        self.session.lines.clear()
        return ToolResult.ok(f"Scrolled {args.direction}")


class TypeArgs(ToolArgs):
    text: str = Field(min_length=1, max_length=2000, description="Text to type; no line breaks")

    @model_validator(mode="after")
    def _one_line(self) -> TypeArgs:
        if re.search(r"[\r\n]", self.text):
            raise ValueError("no line breaks: type each line, and press Enter with kbd.key")
        return self


class KbdType(_ScreenTool[TypeArgs]):
    name = "kbd.type"
    description = "Type text into the focused window, as if on the keyboard."
    usage = Usage(
        when="A field in a desktop app has focus (you clicked it) and needs text.",
        avoid="Browser fields: browser.fill. Fields in ui.tree: ui.act with set_value.",
        mistakes="Typing before clicking the field, so the text goes elsewhere.",
        example='kbd.type(text="Quarterly report")',
    )
    args_model = TypeArgs

    async def run(self, args: TypeArgs, ctx: ToolContext) -> ToolResult:
        try:
            await self.session.screen.type(args.text)
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        self.session.lines.clear()
        return ToolResult.ok(f"Typed {len(args.text)} characters")


class KeyArgs(ToolArgs):
    keys: str = Field(
        description='One key or combination: "Return", "Tab", "ctrl+s", "alt+left"',
        max_length=40,
    )


class KbdKey(_ScreenTool[KeyArgs]):
    name = "kbd.key"
    description = "Press a key or key combination in the focused window."
    usage = Usage(
        when="A shortcut does what you need (ctrl+s saves), or to confirm, move or cancel.",
        mistakes="Pressing Enter on a dialog you have not read.",
        example='kbd.key(keys="ctrl+s")',
    )
    args_model = KeyArgs

    def tier_for(self, args: KeyArgs, policy: Policy) -> Tier:
        combo = "+".join(part.strip() for part in args.keys.lower().split("+"))
        risky = combo in RISKY_COMBOS or combo.rsplit("+", 1)[-1] in RISKY_KEYS
        return Tier.CONFIRM if risky else Tier.AUTO

    def sentence(self, args: KeyArgs) -> str:
        return f"Press {args.keys} in the focused window?"

    async def run(self, args: KeyArgs, ctx: ToolContext) -> ToolResult:
        try:
            combo = keyvals(args.keys)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        try:
            await self.session.screen.keys(combo)
        except ShellUnavailableError as exc:
            return ToolResult.fail(str(exc))
        self.session.lines.clear()
        return ToolResult.ok(f"Pressed {args.keys}")


def _text(session: ScreenSession, target: str) -> str:
    line = session.lines.get(target)
    return line.text[:80] if line else ""


def screen_tools(screen: Screen | None = None) -> list[Tool[Any]]:
    if screen is None:
        from arcee.platform.screen import GnomeScreen

        screen = GnomeScreen()
    session = ScreenSession(screen)
    return [
        ScreenRead(session),
        ScreenClick(session),
        ScreenScroll(session),
        KbdType(session),
        KbdKey(session),
    ]
