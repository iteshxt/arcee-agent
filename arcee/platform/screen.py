"""The screen as text, and input on it (spec section 9, levels 3 and 4; decision 0009).

The default Core models read no images, so the screen is read with OCR: the
extension's screenshot is greyscaled and doubled (at its own size Tesseract
missed a dialog's "Type here", "Cancel" and "OK"), then read in sparse-text
mode, and the words are joined into lines with their boxes. Boxes come back in
screen coordinates, the ones the extension clicks in: the screenshot can be
larger than the screen under fractional or HiDPI scaling.
"""

import asyncio
import itertools
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from arcee.platform.shell import ShellBridge, ShellUnavailableError

OCR_SCALE: Final = 2
# Words further apart than this many text heights belong to different things.
WORD_GAP_HEIGHTS: Final = 1.5
# Lines Tesseract is less sure of than this are mostly icons read as letters.
MIN_CONFIDENCE: Final = 50.0
MIN_WORD_CONFIDENCE: Final = 30.0
OCR_TIMEOUT_S: Final = 30.0
CONTROL_REASON: Final = "Arcee is working"
# How often to look whether the user has resumed Arcee.
PAUSE_POLL_S: Final = 0.5
# Time for GNOME's animations after bringing a window forward, before a screenshot.
FOCUS_SETTLE_S: Final = 0.6

# Key names to X keysyms. Single printable characters map to their code point.
MODIFIERS: Final = {
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "shift": 0xFFE1,
    "alt": 0xFFE9,
    "super": 0xFFEB,
    "meta": 0xFFEB,
}
NAMED_KEYS: Final = {
    "return": 0xFF0D,
    "enter": 0xFF0D,
    "tab": 0xFF09,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "backspace": 0xFF08,
    "delete": 0xFFFF,
    "insert": 0xFF63,
    "home": 0xFF50,
    "end": 0xFF57,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "page_up": 0xFF55,
    "pageup": 0xFF55,
    "page_down": 0xFF56,
    "pagedown": 0xFF56,
    "space": 0x20,
    "plus": 0x2B,
    "minus": 0x2D,
    "menu": 0xFF67,
    **{f"f{n}": 0xFFBE + n - 1 for n in range(1, 13)},
}


def keyvals(combo: str) -> list[int]:
    """`ctrl+shift+t` -> keysyms, modifiers first. Raises ValueError for an unknown key."""
    names = [part.strip().lower() for part in combo.split("+")]
    if not names or any(not name for name in names):
        raise ValueError(f"cannot read the key combination {combo!r}")
    *mods, key = names
    out: list[int] = []
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f"unknown modifier {mod!r}; use ctrl, shift, alt or super")
        out.append(MODIFIERS[mod])
    if key in MODIFIERS:
        out.append(MODIFIERS[key])
    elif key in NAMED_KEYS:
        out.append(NAMED_KEYS[key])
    elif len(key) == 1 and 0x20 < ord(key) < 0x7F:
        out.append(ord(key))
    else:
        raise ValueError(f"unknown key {key!r}")
    return out


@dataclass(frozen=True, slots=True)
class TextLine:
    """A line of text on the screen and its box, in screen coordinates."""

    text: str
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(frozen=True, slots=True)
class Shot:
    """A screenshot: the image, and the screen size its pixels map onto."""

    path: Path
    screen_width: int
    screen_height: int


def parse_tsv(tsv: str, *, factor: float, min_confidence: float = MIN_CONFIDENCE) -> list[TextLine]:
    """Tesseract's TSV as lines, boxes multiplied by `factor` (image -> screen)."""
    lines: dict[tuple[str, ...], list[list[str]]] = {}
    for row in tsv.splitlines()[1:]:
        cells = row.split("\t")
        # Borders and icons come back as low-confidence marks ("|", "J"): drop them
        # before they join a line and move its box.
        if (
            len(cells) == 12
            and float(cells[10]) >= MIN_WORD_CONFIDENCE
            and re.search(r"\w", cells[11])
        ):
            lines.setdefault(tuple(cells[1:5]), []).append(cells)
    out: list[TextLine] = []
    for words in (group for line in lines.values() for group in _split_at_gaps(line)):
        confidence = sum(float(w[10]) for w in words) / len(words)
        text = " ".join(w[11].strip() for w in words)
        if confidence < min_confidence or len(re.findall(r"\w", text)) < 2:
            continue
        left = min(int(w[6]) for w in words)
        top = min(int(w[7]) for w in words)
        right = max(int(w[6]) + int(w[8]) for w in words)
        bottom = max(int(w[7]) + int(w[9]) for w in words)
        out.append(
            TextLine(
                text,
                round(left * factor),
                round(top * factor),
                round((right - left) * factor),
                round((bottom - top) * factor),
            )
        )
    # Reading order: top to bottom, then left to right within a band of rows.
    out.sort(key=lambda line: (line.y // 12, line.x))
    return out


def _split_at_gaps(words: list[list[str]]) -> list[list[list[str]]]:
    """Split a Tesseract line where words sit far apart: separate controls on one row.

    A search box, a label and a Stop button in a row came back as one line, whose
    centre was none of them.
    """
    words = sorted(words, key=lambda w: int(w[6]))
    groups = [[words[0]]]
    for previous, word in itertools.pairwise(words):
        gap = int(word[6]) - (int(previous[6]) + int(previous[8]))
        height = max(int(previous[9]), int(word[9]))
        if gap > WORD_GAP_HEIGHTS * height:
            groups.append([])
        groups[-1].append(word)
    return groups


async def read_text(shot: Shot) -> list[TextLine]:
    """OCR the screenshot into lines in screen coordinates."""
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="arcee-ocr-") as folder:
        scaled = Path(folder) / "screen.png"

        def prepare() -> int:
            with Image.open(shot.path) as image:
                grey = image.convert("L")
                size = (grey.width * OCR_SCALE, grey.height * OCR_SCALE)
                grey.resize(size, Image.Resampling.LANCZOS).save(  # pyright: ignore[reportUnknownMemberType]
                    scaled
                )
                return image.width

        image_width = await asyncio.to_thread(prepare)
        process = await asyncio.create_subprocess_exec(
            "tesseract",
            str(scaled),
            "stdout",
            "--psm",
            "11",
            "tsv",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), OCR_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            raise
    factor = shot.screen_width / (image_width * OCR_SCALE)
    return parse_tsv(out.decode(errors="replace"), factor=factor)


class GnomeScreen:
    """Screenshots and input through the Arcee shell extension, OCR with Tesseract.

    Each call first takes control of the screen, which shows the glow (decision
    0010); while the user has paused Arcee it waits, and a stop ends the call.
    """

    def __init__(self, bridge: ShellBridge | None = None) -> None:
        self._bridge = bridge or ShellBridge()

    async def capture(self) -> Shot:
        shot = await self._in_control(self._bridge.screenshot)
        return Shot(Path(shot["path"]), int(shot["width"]), int(shot["height"]))

    async def read(self, shot: Shot) -> list[TextLine]:
        return await read_text(shot)

    async def focus(self, title: str) -> bool:
        focused = await asyncio.to_thread(self._bridge.focus_title, title)
        if focused:
            await asyncio.sleep(FOCUS_SETTLE_S)  # the window and overview animations
        return focused

    async def click(self, x: int, y: int, button: int, count: int) -> None:
        await self._in_control(self._bridge.click, x, y, button, count)

    async def scroll(self, x: int, y: int, steps: int) -> None:
        await self._in_control(self._bridge.scroll, x, y, steps)

    async def keys(self, combo: list[int]) -> None:
        await self._in_control(self._bridge.key_combo, combo)

    async def type(self, text: str) -> None:
        await self._in_control(self._bridge.type_text, text)

    async def _in_control[T](self, call: Callable[..., T], *args: Any) -> T:
        """Run `call` while holding control, waiting out a pause."""
        while True:
            state = await asyncio.to_thread(self._bridge.begin_control, CONTROL_REASON)
            while state == "paused":
                await asyncio.sleep(PAUSE_POLL_S)
                state = await asyncio.to_thread(self._bridge.control_state)
            if state == "off":
                raise ShellUnavailableError("the user stopped Arcee")
            try:
                return await asyncio.to_thread(call, *args)
            except ShellUnavailableError as exc:
                if "paused" not in str(exc):  # paused between the check and the call
                    raise
