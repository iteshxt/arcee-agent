"""Arranging windows (halves, screens, workspaces) and the notifications missed."""

from pathlib import Path
from typing import Any

import pytest

from arcee.core.toolsets import WINDOWS, filters_for
from arcee.models.base import Message
from arcee.tools.base import NoArgs, ToolContext
from arcee.tools.windows import (
    MoveArgs,
    NotificationsList,
    TileArgs,
    WindowMove,
    WindowScreens,
    WindowTile,
)

CTX = ToolContext(task_id="t", home=Path("/h"))
OPEN = [
    {"id": 3, "app_id": "google-chrome", "title": "YouTube - Google Chrome"},
    {"id": 5, "app_id": "org.gnome.Ptyxis", "title": "Terminal"},
]


class Shell:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def available(self) -> bool:
        return True

    def windows(self) -> list[dict[str, Any]]:
        return OPEN

    def window_action(self, window_id: int, action: str) -> bool:
        return True

    def tile_window(self, window_id: int, where: str) -> bool:
        self.calls.append(("tile", window_id, where))
        return True

    def move_window(self, window_id: int, monitor: int, workspace: int) -> bool:
        self.calls.append(("move", window_id, monitor, workspace))
        return True

    def monitors(self) -> dict[str, Any]:
        return {
            "monitors": [
                {"index": 0, "primary": True, "width": 1920, "height": 1080, "connector": "eDP-1"},
                {
                    "index": 1,
                    "primary": False,
                    "width": 2560,
                    "height": 1440,
                    "connector": "HDMI-A-1",
                },
            ],
            "workspaces": 3,
            "active_workspace": 0,
        }

    def notifications(self) -> list[dict[str, Any]]:
        return [
            {"app": "Slack", "title": "Rahul", "body": "lunch?", "time": 1_700_000_000},
            {"app": "Calendar", "title": "Standup", "body": "", "time": 1_700_000_600},
        ]


async def test_two_windows_side_by_side() -> None:
    shell = Shell()
    tool = WindowTile(shell)  # pyright: ignore[reportArgumentType]
    left = await tool.run(TileArgs(window="Chrome", where="left"), CTX)
    await tool.run(TileArgs(window="terminal", where="right"), CTX)
    assert shell.calls == [("tile", 3, "left"), ("tile", 5, "right")]
    assert left.summary == "Put YouTube - Google Chrome on the left"


async def test_a_window_goes_to_the_second_screen_counted_from_one() -> None:
    shell = Shell()
    tool = WindowMove(shell)  # pyright: ignore[reportArgumentType]
    moved = await tool.run(MoveArgs(window="Chrome", monitor=2), CTX)
    nowhere = await tool.run(MoveArgs(window="Chrome"), CTX)
    assert shell.calls == [("move", 3, 1, -1)]
    assert moved.summary == "Moved YouTube - Google Chrome to screen 2"
    assert nowhere.error == "say a monitor or a workspace to move it to"


async def test_the_screens_are_named_from_one() -> None:
    result = await WindowScreens(Shell()).run(NoArgs(), CTX)  # pyright: ignore[reportArgumentType]
    assert result.data == {
        "screens": ["screen 1: 1920x1080 eDP-1 (main)", "screen 2: 2560x1440 HDMI-A-1"],
        "workspaces": 3,
        "current_workspace": 1,
    }


async def test_missed_notifications_newest_first() -> None:
    tool = NotificationsList(Shell())  # pyright: ignore[reportArgumentType]
    result = await tool.run(NoArgs(), CTX)
    assert result.data is not None
    first, second = result.data["notifications"]
    assert first.endswith("Calendar: Standup")
    assert second.endswith("Slack: Rahul — lunch?")
    assert tool.untrusted
    assert tool.personal == "your notifications"


@pytest.mark.parametrize(
    "said",
    [
        "Put Chrome on the left and the terminal on the right",
        "Open Files on my second screen",
        "Move Spotify to workspace 2",
        "What notifications did I miss?",
    ],
)
def test_arranging_and_notifications_bring_the_window_tools(said: str) -> None:
    assert set(WINDOWS) <= set(filters_for([Message("user", said)]))
