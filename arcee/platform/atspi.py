# pyright: basic, reportMissingImports=false
"""The desktop accessibility tree over AT-SPI (spec section 9, level 2).

`pyatspi` has no type information, so this module is the only one that touches
it, at basic type checking; everything it returns is plain data. Every call runs
on one dedicated thread: AT-SPI is synchronous D-Bus, and a slow or hung app
must not block Arcee's event loop (spec section 12).
"""

import asyncio
import concurrent.futures
import time
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
                    frames.append((app.name, frame))
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
            wanted = app.lower()
            for name, frame in frames:
                if wanted in name.lower() or wanted in (frame.name or "").lower():
                    return name, frame
            raise LookupError(f"no open window matches {app!r}; see ui.windows")
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
        else:
            _do_action(node, action)
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
    actions = node.queryAction()
    names = [actions.getName(i) for i in range(actions.nActions)]
    for candidate in (wanted, "click", "activate", "press", "default.activate"):
        if candidate in names:
            actions.doAction(names.index(candidate))
            return
    if names:
        actions.doAction(0)
        return
    raise LookupError(f"{node.name or 'the element'} has no actions; try select or focus")


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
