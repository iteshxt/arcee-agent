"""Element actions: a generic press finds the element's press; other names must exist."""

from typing import Any

import pytest

from arcee.platform import atspi
from arcee.platform.atspi import _do_action  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def editable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(atspi, "_editable", lambda _node: True)


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
    offers = "has no action 'set-text'; it offers activate, select, focus, set_value"
    with pytest.raises(LookupError, match=offers):
        _do_action(node, "set-text")
    assert node.actions.done == []


class Selection:
    def __init__(self) -> None:
        self.picked: list[int] = []

    def selectChild(self, index: int) -> bool:
        self.picked.append(index)
        return True


class Parent:
    def __init__(self) -> None:
        self.selection = Selection()

    def querySelection(self) -> Selection:
        return self.selection


class Cell(Node):
    name = "report-draft.txt. File"

    def __init__(self) -> None:
        super().__init__("listitem.scroll-to")
        self.parent = Parent()

    def getIndexInParent(self) -> int:
        return 1


class Window:
    def __init__(self) -> None:
        self.actions = Actions(["view.rename", "view.copy"])

    def queryAction(self) -> Actions:
        return self.actions


def test_a_window_action_asked_on_an_item_selects_it_and_runs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(atspi, "SETTLE_S", 0)
    desktop = atspi.AtspiDesktop(shell=object())  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(desktop, "_bring_forward", lambda: None)
    cell, window = Cell(), Window()
    desktop._refs = atspi._Refs(window=window, elements={"el_2": cell})  # pyright: ignore[reportPrivateUsage]

    done = desktop._act("el_2", "view.rename", None)  # pyright: ignore[reportPrivateUsage]

    assert cell.parent.selection.picked == [1]
    assert window.actions.done == ["view.rename"]
    assert done == "window action view.rename on report-draft.txt. File"
