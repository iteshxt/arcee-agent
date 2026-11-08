"""The clipboard, screenshots kept for the user, recordings (no real desktop)."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import CAPTURE, filters_for
from arcee.models.base import Message
from arcee.tools.base import NoArgs, ToolContext
from arcee.tools.capture import (
    ClipboardGet,
    ClipboardSet,
    ClipboardSetArgs,
    RecordArgs,
    RecordAudio,
    SaveArgs,
    ScreenSave,
    _Recording,
)
from arcee.tools.commands import Done


class Clip:
    def __init__(self, types: Done, text: str = "") -> None:
        self.types = types
        self.text = text
        self.ran: list[tuple[str, bytes | None, bool]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        limit_s: float = 15,
        stdin: bytes | None = None,
        quiet: bool = False,
    ) -> Done:
        self.ran.append((" ".join(argv), stdin, quiet))
        if "--list-types" in argv:
            return self.types
        return Done(0, self.text, "")

    def has(self, program: str) -> bool:
        return True


def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(task_id="t", home=tmp_path)


async def test_the_clipboards_text_is_read(tmp_path: Path) -> None:
    clip = Clip(Done(0, "text/html\ntext/plain;charset=utf-8\n", ""), "hello there")
    tool = ClipboardGet(clip)
    result = await tool.run(NoArgs(), ctx(tmp_path))
    assert result.data == {"text": "hello there", "characters": 11}
    assert clip.ran[-1][0] == "wl-paste --no-newline --type text/plain;charset=utf-8"
    assert tool.untrusted
    assert tool.personal == "your clipboard"


async def test_an_image_or_nothing_is_said_so(tmp_path: Path) -> None:
    image = await ClipboardGet(Clip(Done(0, "image/png\n", ""))).run(NoArgs(), ctx(tmp_path))
    empty = await ClipboardGet(Clip(Done(1, "", "Nothing is copied\n"))).run(
        NoArgs(), ctx(tmp_path)
    )
    assert image.summary == "The clipboard holds image/png, not text"
    assert empty.summary == "The clipboard is empty"


async def test_copying_hands_the_text_to_wl_copy_without_waiting_on_it(tmp_path: Path) -> None:
    clip = Clip(Done(0, "", ""))
    result = await ClipboardSet(clip).run(ClipboardSetArgs(text="petri@example.com"), ctx(tmp_path))
    assert clip.ran == [("wl-copy --type text/plain;charset=utf-8", b"petri@example.com", True)]
    assert result.summary == "Copied: petri@example.com"


@dataclass
class Shot:
    path: Path


class FakeScreen:
    def __init__(self, png: Path) -> None:
        self.png = png
        self.focused: list[str] = []

    async def capture(self) -> Shot:
        return Shot(self.png)

    async def focus(self, title: str) -> bool:
        self.focused.append(title)
        return title == "Chrome"


async def test_a_screenshot_is_kept_in_pictures(tmp_path: Path) -> None:
    png = tmp_path / "screen.png"
    png.write_bytes(b"\x89PNG")
    screen = FakeScreen(png)
    tool = ScreenSave(lambda: screen)
    result = await tool.run(SaveArgs(window="Chrome"), ctx(tmp_path))
    assert result.data is not None
    saved = Path(result.data["saved"])
    assert saved.parent == tmp_path / "Pictures" / "Screenshots"
    assert saved.name.startswith("Screenshot From ")
    assert saved.read_bytes() == b"\x89PNG"  # noqa: ASYNC240
    missing = await tool.run(SaveArgs(window="Zoom"), ctx(tmp_path))
    assert missing.error == "no window matches “Zoom”"


class FakeAudio(RecordAudio):
    def __init__(self) -> None:
        super().__init__()
        self.stopped = 0

    async def _start(self, path: Path) -> _Recording:
        async def stop() -> None:
            self.stopped += 1

        return _Recording(path, 0.0, stop)


async def test_a_recording_asks_first_then_starts_and_stops(tmp_path: Path) -> None:
    tool = FakeAudio()
    start = RecordArgs(action="start", name="Lecture")
    assert tool.tier_for(start, Policy()) is Tier.CONFIRM
    assert tool.tier_for(RecordArgs(action="stop"), Policy()) is Tier.AUTO
    started = await tool.run(start, ctx(tmp_path))
    again = await tool.run(start, ctx(tmp_path))
    stopped = await tool.run(RecordArgs(action="stop"), ctx(tmp_path))
    assert started.data is not None
    assert started.data["file"] == str(tmp_path / "Music" / "Recordings" / "Lecture.ogg")
    assert again.error is not None
    assert "already recording" in again.error
    assert stopped.summary.startswith("Saved ")
    assert tool.stopped == 1
    nothing = await tool.run(RecordArgs(action="stop"), ctx(tmp_path))
    assert nothing.summary == "Nothing was being recorded"


@pytest.mark.parametrize(
    "request_",
    [
        "What did I just copy?",
        "Copy my email address",
        "Take a screenshot and save it to Pictures",
        "Record my screen",
        "Record a voice memo",
        "stop recording",
    ],
)
def test_capture_requests_offer_the_capture_tools(request_: str) -> None:
    assert set(CAPTURE) <= set(filters_for([Message("user", request_)]))
