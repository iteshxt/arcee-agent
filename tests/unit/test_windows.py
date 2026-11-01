from pathlib import Path
from typing import Any

import pytest

from arcee.core.policy import Policy, Tier
from arcee.core.presence import is_visible
from arcee.core.toolsets import filters_for
from arcee.models.base import Message
from arcee.tools.base import NoArgs, ToolContext
from arcee.tools.windows import (
    AppClose,
    AppCloseArgs,
    WindowAct,
    WindowArgs,
    WindowList,
    find_window,
)

CTX = ToolContext(task_id="t", home=Path("/h"))
WINDOWS = [
    {"id": 1, "app_id": "org.gnome.Ptyxis", "title": "open the Calculator app", "focused": True},
    {"id": 2, "app_id": "org.gnome.Calculator", "title": "Calculator"},
    {"id": 3, "app_id": "google-chrome", "title": "YouTube - Google Chrome"},
    {"id": 4, "app_id": "google-chrome", "title": "Gmail - Google Chrome", "minimized": True},
]


class Shell:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.actions: list[tuple[int, str]] = []

    def available(self) -> bool:
        return self.running

    def windows(self) -> list[dict[str, Any]]:
        return WINDOWS

    def window_action(self, window_id: int, action: str) -> bool:
        self.actions.append((window_id, action))
        return True


def test_a_window_is_found_by_id_app_or_title_and_never_by_a_terminal_title() -> None:
    assert find_window("3", WINDOWS) == WINDOWS[2]
    assert find_window("Calculator", WINDOWS) == WINDOWS[1]
    assert find_window("Gmail", WINDOWS) == WINDOWS[3]
    assert find_window("Firefox", WINDOWS) is None


async def test_list_shows_ids_apps_and_states() -> None:
    result = await WindowList(Shell()).run(NoArgs(), CTX)

    assert result.data is not None
    assert result.data["windows"][0] == "1 org.gnome.Ptyxis: open the Calculator app (focused)"
    assert result.data["windows"][3] == "4 google-chrome: Gmail - Google Chrome (minimized)"


async def test_acting_on_a_window_and_closing_asks_first() -> None:
    shell = Shell()
    act = WindowAct(shell)

    done = await act.run(WindowArgs(window="Calculator", action="minimize"), CTX)

    assert done.summary == "Minimized Calculator"
    assert shell.actions == [(2, "minimize")]
    assert act.tier_for(WindowArgs(window="Calculator", action="close"), Policy()) is Tier.CONFIRM
    assert act.tier_for(WindowArgs(window="Calculator", action="focus"), Policy()) is Tier.AUTO


async def test_closing_an_app_closes_every_window_of_it() -> None:
    shell = Shell()
    tool = AppClose(shell)

    done = await tool.run(AppCloseArgs(name="chrome"), CTX)

    assert done.summary == "Closed 2 chrome windows"
    assert shell.actions == [(3, "close"), (4, "close")]
    assert tool.tier is Tier.CONFIRM
    assert tool.sentence(AppCloseArgs(name="Chrome")) == "Close every Chrome window?"


async def test_without_the_extension_the_tools_say_how_to_fix_it() -> None:
    result = await WindowList(Shell(running=False)).run(NoArgs(), CTX)

    assert "arcee-cli shell-extension" in str(result.error)


@pytest.mark.parametrize("request_text", ["minimise the calculator", "close chrome"])
def test_window_requests_get_the_window_tools_and_show_the_glow(request_text: str) -> None:
    assert "window.*" in filters_for([Message("user", request_text)])
    assert is_visible("window.act")
    assert is_visible("app.close")


def test_closing_a_tab_stays_in_the_browser() -> None:
    filters = filters_for([Message("user", "close the youtube tab in chrome")])

    assert "browser.*" in filters
    assert "window.*" not in filters
