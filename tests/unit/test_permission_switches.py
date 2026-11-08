"""Permissions → "Ask before these too": every switch names real tools that act."""

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("gi") is None, reason="the app needs PyGObject"
)


def test_every_switch_covers_known_tools_that_act() -> None:
    from arcee.app.window import ASKABLE, ask_tools
    from arcee.tools.builtin import builtin_registry
    from arcee.tools.reminders import reminder_tools

    names = {t.name for t in builtin_registry(shell_limits=False)}
    names |= {t.name for t in reminder_tools(None)}  # type: ignore[arg-type]
    names |= {"control.set_timer", "ui.act", "screen.click", "kbd.type"}
    for switch, _title, _about in ASKABLE:
        for tool in ask_tools(switch):
            assert tool in names, f"{switch} names an unknown tool {tool}"


def test_one_switch_stands_for_all_the_settings() -> None:
    from arcee.app.window import ask_tools

    assert "settings.brightness" in ask_tools("settings.volume")
    assert ask_tools("print.file") == ("print.file",)
