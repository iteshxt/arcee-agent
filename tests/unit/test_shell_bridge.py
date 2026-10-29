from typing import Any

from arcee.platform.shell import ShellBridge


class Missing(ShellBridge):
    def _connect(self) -> Any:
        raise RuntimeError("no session bus")


def test_without_the_extension_nothing_is_focused_or_listed() -> None:
    bridge = Missing()

    assert bridge.available() is False
    assert bridge.focus_title("Files") is False
    assert bridge.windows() == []
