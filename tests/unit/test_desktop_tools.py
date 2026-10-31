from pathlib import Path
from typing import Any

import pytest

from arcee.core.policy import Policy, Tier
from arcee.core.routing import start_tier
from arcee.core.toolsets import DESKTOP, filters_for
from arcee.models.base import Message
from arcee.platform.atspi import Snapshot
from arcee.tools.base import NoArgs, ToolContext
from arcee.tools.desktop import ActArgs, TreeArgs, desktop_tools

CTX = ToolContext(task_id="t", home=Path("/h"))
SNAP = Snapshot(
    app="org.gnome.Nautilus",
    window="Home",
    elements=[
        {"id": "el_1", "role": "table cell", "name": "report.pdf", "actions": ["select"]},
        {"id": "el_2", "role": "button", "name": "Move to Trash", "actions": ["click"]},
        {"id": "el_3", "role": "entry", "name": "Name", "states": ["editable"]},
    ],
    window_actions=["view.rename", "view.move-to-trash", "win.new-window"],
    count=3,
)


class FakeDesktop:
    def __init__(self) -> None:
        self.acts: list[tuple[str, str, str | None]] = []

    async def windows(self) -> list[dict[str, Any]]:
        return [{"app": "org.gnome.Nautilus", "window": "Home", "active": True}]

    async def snapshot(self, app: str | None, limit: int) -> Snapshot:
        if app == "nowhere":
            raise LookupError("no open window matches 'nowhere'; see ui.windows")
        return SNAP

    async def act(self, target: str, action: str, value: str | None) -> str:
        self.acts.append((target, action, value))
        return f"{action} {target}"


@pytest.fixture
def tools() -> tuple[Any, Any, Any, FakeDesktop]:
    desktop = FakeDesktop()
    windows, tree, act = desktop_tools(desktop)
    return windows, tree, act, desktop


async def test_tree_numbers_elements_and_lists_window_actions(tools: Any) -> None:
    windows, tree, _, _ = tools

    listed = await windows.run(NoArgs(), CTX)
    read = await tree.run(TreeArgs(app="Files"), CTX)
    missing = await tree.run(TreeArgs(app="nowhere"), CTX)

    assert listed.summary == "1 windows open"
    assert read.summary == "Home: 3 elements"
    assert read.data is not None
    assert read.data["window_actions"] == ["view.rename", "view.move-to-trash", "win.new-window"]
    assert missing.error == "no open window matches 'nowhere'; see ui.windows"


async def test_risky_elements_and_window_actions_need_confirmation(tools: Any) -> None:
    _, tree, act, _ = tools
    await tree.run(TreeArgs(), CTX)

    assert act.tier_for(ActArgs(target="el_1", action="select"), Policy()) is Tier.AUTO
    assert act.tier_for(ActArgs(target="el_2", action="click"), Policy()) is Tier.CONFIRM
    assert act.tier_for(ActArgs(target="window", action="view.rename"), Policy()) is Tier.AUTO
    trash = ActArgs(target="window", action="view.move-to-trash")
    assert act.tier_for(trash, Policy()) is Tier.CONFIRM
    assert act.sentence(trash) == "Run “view.move-to-trash” in the app window?"
    assert act.sentence(ActArgs(target="el_2", action="click")) == "Click “Move to Trash”?"


async def test_act_passes_through_and_forgets_stale_numbers(tools: Any) -> None:
    _, tree, act, desktop = tools
    await tree.run(TreeArgs(), CTX)

    done = await act.run(ActArgs(target="el_3", action="set_value", value="beach.jpg"), CTX)
    empty = await act.run(ActArgs(target="el_3", action="set_value"), CTX)

    assert done.summary == "Set_value el_3"
    assert desktop.acts == [("el_3", "set_value", "beach.jpg")]
    assert empty.error == "set_value needs a value"
    assert act.session.elements == {}


@pytest.mark.parametrize(
    ("request_text", "desktop"),
    [
        ("In the Files app, rename old.txt to new.txt", True),
        ("Click the Save button in Text Editor", True),
        ("How much RAM is free?", False),
    ],
)
def test_desktop_tools_come_with_desktop_requests(request_text: str, desktop: bool) -> None:
    filters = filters_for([Message("user", request_text)])

    assert (DESKTOP[0] in filters) is desktop


async def test_a_thin_tree_points_to_reading_the_screen(tools: Any) -> None:
    _, tree, _, _ = tools

    read = await tree.run(TreeArgs(), CTX)

    assert read.data is not None
    assert read.data["hint"] == (
        "This window's tree shows little. To see it as on screen: "
        'screen.read(window="Home"), then screen.click.'
    )


@pytest.mark.parametrize(
    "request_text",
    [
        "open the Calculator app and work out 12 times 34 using its buttons",
        "click the Save button",
        "press ctrl+s in the text editor",
        "in Files, rename old.txt to new.txt",
    ],
)
def test_desktop_requests_get_the_desktop_tools_and_the_large_model(request_text: str) -> None:
    assert set(DESKTOP) <= set(filters_for([Message("user", request_text)]))
    assert start_tier(request_text) == 1
