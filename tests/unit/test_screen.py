from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from arcee.core.policy import Policy, Tier
from arcee.platform.screen import (
    GnomeScreen,
    Shot,
    TextLine,
    keyvals,
    merge_lines,
    parse_tsv,
)
from arcee.platform.shell import ShellUnavailableError
from arcee.tools.base import ToolContext
from arcee.tools.screen import (
    ClickArgs,
    KbdKey,
    KbdType,
    KeyArgs,
    ReadArgs,
    ScreenClick,
    ScreenRead,
    ScreenScroll,
    ScreenSession,
    ScrollArgs,
    TypeArgs,
)

CTX = ToolContext(task_id="t_1", home=Path("/home/u"))
HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


def word(block: int, line: int, left: int, top: int, text: str, conf: float = 90) -> str:
    return f"5\t1\t{block}\t1\t{line}\t1\t{left}\t{top}\t{len(text) * 20}\t30\t{conf}\t{text}"


class FakeScreen:
    def __init__(self, lines: list[TextLine], *, available: bool = True) -> None:
        self.lines = lines
        self.available = available
        self.calls: list[tuple[str, Any]] = []

    def _check(self) -> None:
        if not self.available:
            raise ShellUnavailableError("the Arcee shell extension is not running")

    async def capture(self) -> Shot:
        self._check()
        return Shot(Path("/run/arcee/screen.png"), 1280, 800)

    async def read(self, shot: Shot) -> list[TextLine]:
        return self.lines

    async def focus(self, title: str) -> bool:
        self.calls.append(("focus", title))
        return title == "Calculator"

    async def click(self, x: int, y: int, button: int, count: int) -> None:
        self._check()
        self.calls.append(("click", (x, y, button, count)))

    async def scroll(self, x: int, y: int, steps: int) -> None:
        self.calls.append(("scroll", (x, y, steps)))

    async def keys(self, combo: list[int]) -> None:
        self.calls.append(("keys", combo))

    async def type(self, text: str) -> None:
        self.calls.append(("type", text))


LINES = [TextLine("Cancel", 520, 470, 110, 30), TextLine("Delete forever", 650, 470, 110, 30)]


def test_tsv_words_become_lines_in_screen_coordinates() -> None:
    tsv = "\n".join(
        [
            HEADER,
            word(1, 1, 1100, 120, "Type"),
            word(1, 1, 1200, 120, "here"),
            word(2, 1, 1040, 940, "Cancel"),
            word(3, 1, 600, 600, "eee", conf=20),  # an icon read as letters
            word(4, 1, 10, 10, "|"),
        ]
    )

    lines = parse_tsv(tsv, factor=0.5)  # OCR ran on a doubled image

    assert lines == [TextLine("Type here", 550, 60, 90, 15), TextLine("Cancel", 520, 470, 60, 15)]
    assert lines[0].center == (595, 67)


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("Return", [0xFF0D]),
        ("ctrl+s", [0xFFE3, ord("s")]),
        ("ctrl+shift+T", [0xFFE3, 0xFFE1, ord("t")]),
        ("alt + F4", [0xFFE9, 0xFFC1]),
        ("ctrl+plus", [0xFFE3, 0x2B]),
    ],
)
def test_key_names(combo: str, expected: list[int]) -> None:
    assert keyvals(combo) == expected


@pytest.mark.parametrize("combo", ["", "ctrl+", "hyper+a", "ctrl+banana"])
def test_unknown_keys_are_refused(combo: str) -> None:
    with pytest.raises(ValueError, match=r"key|modifier"):
        keyvals(combo)


class TestTools:
    async def test_read_numbers_lines_with_their_centres(self) -> None:
        session = ScreenSession(FakeScreen(LINES))

        result = await ScreenRead(session).run(ReadArgs(), CTX)

        assert result.data == {
            "screen": "1280x800",
            "lines": ["t_1 [575,485] Cancel", "t_2 [705,485] Delete forever"],
        }
        assert ScreenRead(session).changes_things(ReadArgs()) is False
        assert ScreenRead(session).changes_things(ReadArgs(window="Calculator")) is True

    async def test_read_brings_the_named_window_forward(self) -> None:
        screen = FakeScreen(LINES)

        missing = await ScreenRead(ScreenSession(screen)).run(ReadArgs(window="Nope"), CTX)

        assert missing.status == "error"
        assert "no window titled 'Nope'" in str(missing.error)

    async def test_click_a_line_at_its_centre_then_forget_the_numbers(self) -> None:
        screen = FakeScreen(LINES)
        session = ScreenSession(screen)
        await ScreenRead(session).run(ReadArgs(), CTX)

        result = await ScreenClick(session).run(ClickArgs(target="t_1", double=True), CTX)

        assert result.summary == "Clicked Cancel"
        assert screen.calls[-1] == ("click", (575, 485, 1, 2))
        stale = await ScreenClick(session).run(ClickArgs(target="t_1"), CTX)
        assert "not from the latest screen.read" in str(stale.error)

    async def test_risky_text_and_bare_points_need_confirmation(self) -> None:
        session = ScreenSession(FakeScreen(LINES))
        await ScreenRead(session).run(ReadArgs(), CTX)
        click = ScreenClick(session)

        assert click.tier_for(ClickArgs(target="t_1"), Policy()) is Tier.AUTO
        assert click.tier_for(ClickArgs(target="t_2"), Policy()) is Tier.CONFIRM
        assert click.sentence(ClickArgs(target="t_2")) == "Click “Delete forever” on the screen?"
        point = ClickArgs(x=10, y=20)
        assert click.tier_for(point, Policy()) is Tier.CONFIRM
        assert click.sentence(point) == "Click at (10, 20) on the screen?"

    def test_a_click_needs_a_target_or_a_whole_point(self) -> None:
        for args in ({}, {"x": 10}, {"target": "t_1", "x": 1, "y": 2}):
            with pytest.raises(ValidationError, match="target t_N, or both x and y"):
                ClickArgs.model_validate(args)

    async def test_scroll_mid_screen_when_no_target(self) -> None:
        screen = FakeScreen(LINES)
        session = ScreenSession(screen)
        await ScreenRead(session).run(ReadArgs(), CTX)

        await ScreenScroll(session).run(ScrollArgs(direction="up", steps=3), CTX)

        assert screen.calls[-1] == ("scroll", (640, 400, -3))

    @pytest.mark.parametrize(
        ("keys", "tier"),
        [
            ("Tab", Tier.AUTO),
            ("ctrl+s", Tier.AUTO),
            ("Return", Tier.CONFIRM),
            ("ctrl+Return", Tier.CONFIRM),
            ("ctrl + w", Tier.CONFIRM),
            ("alt+F4", Tier.CONFIRM),
            ("Delete", Tier.CONFIRM),
        ],
    )
    def test_keys_that_submit_delete_or_close_need_confirmation(
        self, keys: str, tier: Tier
    ) -> None:
        assert KbdKey(ScreenSession(FakeScreen([]))).tier_for(KeyArgs(keys=keys), Policy()) is tier

    async def test_keys_and_typing_reach_the_screen(self) -> None:
        screen = FakeScreen([])
        session = ScreenSession(screen)

        await KbdKey(session).run(KeyArgs(keys="ctrl+s"), CTX)
        await KbdType(session).run(TypeArgs(text="Quarterly report"), CTX)
        bad = await KbdKey(session).run(KeyArgs(keys="ctrl+banana"), CTX)

        assert screen.calls == [("keys", [0xFFE3, ord("s")]), ("type", "Quarterly report")]
        assert "unknown key 'banana'" in str(bad.error)

    def test_typing_takes_one_line(self) -> None:
        with pytest.raises(ValidationError, match="no line breaks"):
            TypeArgs(text="rm -rf ~\n")

    async def test_without_the_extension_tools_fail_with_the_fix(self) -> None:
        session = ScreenSession(FakeScreen([], available=False))

        result = await ScreenRead(session).run(ReadArgs(), CTX)

        assert result.status == "error"
        assert "extension is not running" in str(result.error)


class FakeBridge:
    """The extension's control states, played in order."""

    def __init__(self, *states: str) -> None:
        self.states = list(states)
        self.clicks: list[tuple[int, int]] = []

    def begin_control(self, reason: str) -> str:
        return self.states.pop(0)

    def control_state(self) -> str:
        return self.states.pop(0)

    def click(self, x: int, y: int, button: int, count: int) -> None:
        self.clicks.append((x, y))


async def test_input_waits_while_the_user_has_paused_arcee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("arcee.platform.screen.PAUSE_POLL_S", 0)
    bridge = FakeBridge("paused", "paused", "active")

    await GnomeScreen(bridge).click(10, 20, 1, 1)  # pyright: ignore[reportArgumentType]

    assert bridge.clicks == [(10, 20)]


async def test_a_stop_during_a_pause_ends_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arcee.platform.screen.PAUSE_POLL_S", 0)
    bridge = FakeBridge("paused", "off")

    with pytest.raises(ShellUnavailableError, match="the user stopped Arcee"):
        await GnomeScreen(bridge).click(10, 20, 1, 1)  # pyright: ignore[reportArgumentType]
    assert bridge.clicks == []


def test_far_apart_words_on_one_row_are_separate_lines() -> None:
    # A label and a button on one row: Tesseract calls it one line.
    tsv = "\n".join(
        [
            HEADER,
            word(1, 1, 100, 100, "Arcee"),
            word(1, 1, 200, 100, "working"),
            word(1, 1, 400, 100, "Stop"),
        ]
    )

    lines = parse_tsv(tsv, factor=1)

    assert [line.text for line in lines] == ["Arcee working", "Stop"]
    assert lines[1].center == (440, 115)


def test_lines_read_twice_are_kept_once() -> None:
    lines = [
        TextLine("Quit Arc", 930, 308, 60, 16),
        TextLine("Quit Arcee", 930, 308, 70, 16),
        TextLine("Today", 930, 210, 40, 16),
    ]

    assert merge_lines(lines) == [lines[2], lines[1]]
