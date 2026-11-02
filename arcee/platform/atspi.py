# pyright: basic, reportMissingImports=false
"""The desktop accessibility tree over AT-SPI (spec section 9, level 2).

`pyatspi` has no type information, so this module is the only one that touches
it, at basic type checking; everything it returns is plain data. Every call runs
on one dedicated thread: AT-SPI is synchronous D-Bus, and a slow or hung app
must not block Arcee's event loop (spec section 12).
"""

import asyncio
import concurrent.futures
import functools
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from arcee.platform.shell import ShellBridge

MAX_DEPTH: Final = 30
# Roles a person can act on or read as content; labels and plain panels are skipped.
INTERACTIVE: Final = frozenset(
    {
        "push button", "button", "toggle button", "check box", "radio button", "menu item",
        "check menu item", "radio menu item", "entry", "text", "password text", "list item",
        "table cell", "combo box", "link", "page tab", "spin button", "slider", "switch",
        "tree item", "menu", "search", "icon",
    }
)  # fmt: skip
# GTK window actions that are noise for an agent (scripts, extension hooks).
_NOISY_ACTION: Final = ("extension_", "script_", "open-scripts-folder")
SETTLE_S: Final = 0.3


@dataclass
class Snapshot:
    app: str
    window: str
    elements: list[dict[str, Any]]
    window_actions: list[str]
    count: int


@dataclass
class _Refs:
    """el_N to live accessibles, and the window, from the latest snapshot."""

    elements: dict[str, Any] = field(default_factory=dict)
    window: Any = None


def pick_frame(wanted: str, frames: Sequence[tuple[str, str]]) -> int | None:
    """Which of (app name, window title) `wanted` names: an exact app or title first,
    then an app whose name contains it, and only then a title that does.

    Titles say anything: a terminal's title held the command that asked for "the
    Calculator app", so a title match alone picked the terminal over Calculator.
    """
    wanted = wanted.lower()
    tests = (
        lambda app, title: wanted in (app.lower(), title.lower()),
        lambda app, title: wanted in app.lower(),
        lambda app, title: wanted in title.lower(),
    )
    for test in tests:
        for index, (app, title) in enumerate(frames):
            if test(app, title):
                return index
    return None


@functools.cache
def app_label(app_id: str) -> str:
    """`org.gnome.Nautilus` -> `org.gnome.Nautilus (Files)`: the name people and models
    use, from the app's .desktop file. Asked for "Files", the tree lookup failed and a
    task ran out of steps re-reading a window it had already renamed a file in."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        info = Gio.DesktopAppInfo.new(f"{app_id}.desktop")
    except ImportError, ValueError, TypeError:
        return app_id
    name = info.get_name() if info is not None else ""
    return f"{app_id} ({name})" if name and name.lower() != app_id.lower() else app_id


async def focused_field() -> Any:
    """The focused editable field in the active window, as a FocusedField, or None."""
    from arcee.tools.screen import FocusedField

    def find() -> FocusedField | None:
        try:
            import pyatspi
        except ImportError:
            return None
        try:
            desktop = pyatspi.Registry.getDesktop(0)
            for i in range(desktop.childCount):
                app = desktop.getChildAtIndex(i)
                if app is None:
                    continue
                for j in range(app.childCount):
                    frame = app.getChildAtIndex(j)
                    if frame is None or not frame.getState().contains(pyatspi.STATE_ACTIVE):
                        continue
                    node = _focused(frame, pyatspi, 0)
                    if node is None:
                        return None
                    text = node.queryText()
                    content = text.getText(0, text.characterCount)
                    if text.getNSelections() == 0:
                        return FocusedField(content, text.caretOffset, text.caretOffset)
                    start, end = text.getSelection(0)
                    return FocusedField(content, start, end)
        except Exception:  # accessibility off, or the field went away: type as asked
            return None
        return None

    return await asyncio.get_running_loop().run_in_executor(_FOCUS_POOL, find)


_FOCUS_POOL: Final = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="atspi-focus")


def _focused(node: Any, pyatspi: Any, depth: int) -> Any:
    if node is None or depth > MAX_DEPTH:
        return None
    state = node.getState()
    if not state.contains(pyatspi.STATE_SHOWING):
        return None
    if state.contains(pyatspi.STATE_FOCUSED) and state.contains(pyatspi.STATE_EDITABLE):
        return node
    for i in range(node.childCount):
        if (found := _focused(node.getChildAtIndex(i), pyatspi, depth + 1)) is not None:
            return found
    return None


class AtspiDesktop:
    def __init__(self, shell: ShellBridge | None = None) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="atspi")
        self._refs = _Refs()
        self._shell = shell or ShellBridge()

    async def _run(self, func: Any, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, func, *args)

    async def windows(self) -> list[dict[str, Any]]:
        return await self._run(self._windows)

    async def snapshot(self, app: str | None, limit: int) -> Snapshot:
        return await self._run(self._snapshot, app, limit)

    async def act(self, target: str, action: str, value: str | None) -> str:
        return await self._run(self._act, target, action, value)

    # --- runs on the AT-SPI thread --------------------------------------------------

    def _frames(self) -> list[tuple[str, Any]]:
        import pyatspi

        desktop = pyatspi.Registry.getDesktop(0)
        frames: list[tuple[str, Any]] = []
        for i in range(desktop.childCount):
            app = desktop.getChildAtIndex(i)
            if app is None or app.name in ("gnome-shell", "mutter-x11-frames"):
                continue
            for j in range(app.childCount):
                frame = app.getChildAtIndex(j)
                if frame is not None and frame.getState().contains(pyatspi.STATE_SHOWING):
                    frames.append((app_label(app.name), frame))
        return frames

    def _windows(self) -> list[dict[str, Any]]:
        import pyatspi

        return [
            {
                "app": app,
                "window": frame.name,
                "active": frame.getState().contains(pyatspi.STATE_ACTIVE),
            }
            for app, frame in self._frames()
            if frame.name  # unnamed frames are helper windows (Chrome has several)
        ]

    def _pick(self, app: str | None) -> tuple[str, Any]:
        import pyatspi

        frames = self._frames()
        if app:
            if (
                found := pick_frame(app, [(name, frame.name or "") for name, frame in frames])
            ) is None:
                raise LookupError(f"no open window matches {app!r}; see ui.windows")
            return frames[found]
        for name, frame in frames:
            if frame.getState().contains(pyatspi.STATE_ACTIVE):
                return name, frame
        if frames:
            return frames[0]
        raise LookupError("no app windows are open")

    def _snapshot(self, app: str | None, limit: int) -> Snapshot:
        import pyatspi

        app_name, frame = self._pick(app)
        found: list[Any] = []

        def walk(node: Any, depth: int) -> None:
            if node is None or depth > MAX_DEPTH or len(found) >= limit * 3:
                return
            try:
                state = node.getState()
                if not state.contains(pyatspi.STATE_SHOWING):
                    return
                role = node.getRoleName()
                if role in INTERACTIVE and (node.name or state.contains(pyatspi.STATE_EDITABLE)):
                    found.append(node)
                for i in range(node.childCount):
                    walk(node.getChildAtIndex(i), depth + 1)
            except Exception:  # a node that vanished mid-walk is skipped
                return

        for i in range(frame.childCount):
            walk(frame.getChildAtIndex(i), 1)
        refs = _Refs(window=frame)
        elements: list[dict[str, Any]] = []
        for node in found[:limit]:
            element_id = f"el_{len(elements) + 1}"
            refs.elements[element_id] = node
            elements.append(_describe(node, element_id))
        self._refs = refs
        return Snapshot(app_name, frame.name or "", elements, _window_actions(frame), len(found))

    def _act(self, target: str, action: str, value: str | None) -> str:
        self._bring_forward()
        if target == "window":
            return self._window_action(action)
        node = self._refs.elements.get(target)
        if node is None:
            raise LookupError(f"{target} is not in the last ui.tree reading; read the window again")
        name = node.name or target
        if action == "focus":
            node.queryComponent().grabFocus()
        elif action == "set_value":
            _set_value(node, value or "")
        elif action == "select":
            node.parent.querySelection().selectChild(node.getIndexInParent())
        elif _offers(node, action) or action in GENERIC_PRESSES:
            _do_action(node, action)
        elif self._refs.window is not None and action in _window_actions(self._refs.window):
            # "view.rename" on a file: the window's actions work on what is selected, so
            # select it and run the window's action (models asked for it on the item).
            node.parent.querySelection().selectChild(node.getIndexInParent())
            time.sleep(SETTLE_S)
            return f"{self._window_action(action)} on {name}"
        else:
            _do_action(node, action)  # refuses, naming what the element offers
        time.sleep(SETTLE_S)  # let the app redraw before the next reading
        return f"{action} {name}"

    def _bring_forward(self) -> None:
        """Make the window active first: GTK closes popovers and dialogs in inactive
        windows at once, and only the shell extension can raise a window on Wayland."""
        import pyatspi

        frame = self._refs.window
        if frame is None or frame.getState().contains(pyatspi.STATE_ACTIVE):
            return
        if frame.name and self._shell.focus_title(frame.name):
            time.sleep(SETTLE_S)

    def _window_action(self, action: str) -> str:
        frame = self._refs.window
        if frame is None:
            raise LookupError("read a window with ui.tree first")
        actions = frame.queryAction()
        names = [actions.getName(i) for i in range(actions.nActions)]
        if action not in names:
            raise LookupError(f"the window has no action {action!r}")
        actions.doAction(names.index(action))
        time.sleep(SETTLE_S)
        return f"window action {action}"


GENERIC_PRESSES: Final = ("click", "activate", "press")


def _describe(node: Any, element_id: str) -> dict[str, Any]:
    import pyatspi

    state = node.getState()
    item: dict[str, Any] = {
        "id": element_id,
        "role": node.getRoleName(),
        "name": (node.name or "")[:120],
    }
    try:
        text = node.queryText()
        content = text.getText(0, min(text.characterCount, 200))
        if content and content != node.name:
            item["value"] = content
    except NotImplementedError:
        pass
    flags = {
        "editable": pyatspi.STATE_EDITABLE,
        "selected": pyatspi.STATE_SELECTED,
        "checked": pyatspi.STATE_CHECKED,
        "focused": pyatspi.STATE_FOCUSED,
    }
    states = [name for name, flag in flags.items() if state.contains(flag)]
    if states:
        item["states"] = states
    try:
        actions = node.queryAction()
        names = [actions.getName(i) for i in range(actions.nActions)]
        item["actions"] = [n for n in names if not n.startswith(_NOISY_ACTION)][:6]
    except NotImplementedError:
        pass
    try:
        node.parent.querySelection()
        item.setdefault("actions", []).append("select")
    except NotImplementedError, AttributeError:
        pass
    return item


def _window_actions(frame: Any) -> list[str]:
    try:
        actions = frame.queryAction()
    except NotImplementedError:
        return []
    names = [actions.getName(i) for i in range(actions.nActions)]
    return [n for n in names if not any(noise in n for noise in _NOISY_ACTION)][:80]


def _do_action(node: Any, wanted: str) -> None:
    """Run the element's action `wanted`. A generic press (click, activate, press) runs
    whatever press the element has; any other name must be one the element offers.

    Falling back for every name ran the wrong thing and reported success: `set-text`
    on Files' rename field ran its `activate`, which kept the old name.
    """
    actions = node.queryAction()
    names = [actions.getName(i) for i in range(actions.nActions)]
    if wanted in names:
        actions.doAction(names.index(wanted))
        return
    if wanted in GENERIC_PRESSES:
        for candidate in (*GENERIC_PRESSES, "default.activate"):
            if candidate in names:
                actions.doAction(names.index(candidate))
                return
        if names:
            actions.doAction(0)
            return
    extras = ["select", "focus"]
    if _editable(node):
        extras.append("set_value")
    offered = ", ".join([*names, *extras])
    raise LookupError(f"{node.name or 'the element'} has no action {wanted!r}; it offers {offered}")


def _offers(node: Any, action: str) -> bool:
    try:
        actions = node.queryAction()
    except NotImplementedError:
        return False
    return action in (actions.getName(i) for i in range(actions.nActions))


def _editable(node: Any) -> bool:
    import pyatspi

    return node.getState().contains(pyatspi.STATE_EDITABLE)


def _set_value(node: Any, value: str) -> None:
    try:
        node.queryEditableText().setTextContents(value)
        return
    except NotImplementedError:
        pass
    try:
        node.queryValue().currentValue = float(value)
    except (NotImplementedError, ValueError) as exc:
        raise LookupError(f"{node.name or 'the element'} does not take a value") from exc
