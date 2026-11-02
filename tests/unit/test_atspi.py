"""Element actions: a generic press finds the element's press; other names must exist."""

from typing import Any

import pytest

from arcee.platform.atspi import _do_action  # pyright: ignore[reportPrivateUsage]


class Actions:
    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.done: list[str] = []
        self.nActions = len(names)

    def getName(self, i: int) -> str:
        return self.names[i]

    def doAction(self, i: int) -> bool:
        self.done.append(self.names[i])
        return True


class Node:
    name = "New Filename"

    def __init__(self, *names: str) -> None:
        self.actions = Actions(list(names))

    def queryAction(self) -> Any:
        return self.actions


def test_a_named_action_runs_that_action() -> None:
    node = Node("activate", "view.popup-menu")
    _do_action(node, "view.popup-menu")
    assert node.actions.done == ["view.popup-menu"]


def test_click_runs_the_press_the_element_has() -> None:
    node = Node("activate")
    _do_action(node, "click")
    assert node.actions.done == ["activate"]


def test_an_unknown_action_is_refused_not_swapped_for_another() -> None:
    # set-text on Files' rename field ran its activate and kept the old name.
    node = Node("activate")
    with pytest.raises(LookupError, match="has no action 'set-text'; it offers activate"):
        _do_action(node, "set-text")
    assert node.actions.done == []
