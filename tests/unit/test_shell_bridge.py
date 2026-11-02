from typing import Any

from arcee.platform.shell import ShellBridge, ShellPresence, ShellUnavailableError


class Missing(ShellBridge):
    def _connect(self) -> Any:
        raise RuntimeError("no session bus")


def test_without_the_extension_nothing_is_focused_or_listed() -> None:
    bridge = Missing()

    assert bridge.available() is False
    assert bridge.focus_title("Files") is False
    assert bridge.windows() == []


class Busy(ShellBridge):
    """The extension is there, but GNOME Shell is too busy to answer in time."""

    def available(self) -> bool:
        return True

    def begin_control(self, reason: str) -> str:
        raise ShellUnavailableError("BeginControl failed: Timeout was reached")


async def test_a_shell_too_busy_to_show_the_glow_does_not_fail_the_request() -> None:
    # While Chrome cold-started, BeginControl timed out and "Open Chrome" failed.
    presence = ShellPresence(Busy())

    await presence.acting()

    assert presence.held is False
