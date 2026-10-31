"""Showing that Arcee is acting on the computer, and yielding to the user (decision 0010).

Before an action the user can see (in Chrome, in an app window, on the screen) Arcee
takes control: the screen glows and a Stop pill shows. If the user has taken the mouse
or keyboard, Arcee waits until they resume. Background work (files, web searches,
system facts) needs neither.
"""

from typing import Final, Protocol

VISIBLE_NAMESPACES: Final = frozenset({"browser", "ui", "screen", "kbd"})
VISIBLE_TOOLS: Final = frozenset({"app.open", "fs.open_with_default"})


def is_visible(tool_name: str) -> bool:
    """Whether running `tool_name` shows on the user's screen."""
    return tool_name in VISIBLE_TOOLS or tool_name.split(".", 1)[0] in VISIBLE_NAMESPACES


class Presence(Protocol):
    """How Arcee shows it is in control (the shell extension; nothing without it)."""

    @property
    def held(self) -> bool: ...
    async def acting(self) -> None: ...
    async def release(self) -> None: ...


class NoPresence:
    """No extension, tests and evals: nothing to show."""

    held = False

    async def acting(self) -> None:
        return None

    async def release(self) -> None:
        return None
