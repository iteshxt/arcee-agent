"""Looking at the screen with a vision model: find things OCR cannot name, answer questions.

The text-only Core reads the screen through OCR (arcee/platform/screen.py), which
misses icons and anything without text. qwen3.8-27b on Groq can see. Asked for raw
coordinates it guessed wrong (x 200 for a button at 703 of 1000), but asked which
labelled grid cell holds a thing it answered right, so finding is done in two grid
rounds: an 8x6 grid over the screen picks a cell, a 6x6 grid over that cell zoomed
in picks a smaller one, and the point snaps to the centre of what is drawn there.
The calls share the Groq rate budget with the other models.
"""

import asyncio
import base64
import io
import os
import re
import statistics
from dataclasses import dataclass
from typing import Any, Final, cast

import httpx
from PIL import Image, ImageDraw, ImageFont

from arcee.models.budget import HeaderBudget

MODEL: Final = "qwen/qwen3.8-27b"
URL: Final = "https://api.groq.com/openai/v1/chat/completions"
KEY_ENV: Final = "GROQ_API_KEY"
# Wide enough to read, small enough to stay well inside 8,000 tokens a minute.
SEND_WIDTH: Final = 1280
COARSE: Final = (8, 6)
FINE: Final = (6, 6)
# How far past its cell the zoomed crop reaches, so a thing on a border is whole.
CROP_MARGIN: Final = 0.25
# A pixel this far from the region's usual grey belongs to what is drawn there.
INK_CONTRAST: Final = 40
GRID_COLOUR: Final = (255, 0, 255)
# A screenshot sent at SEND_WIDTH cost about 2,100 input tokens; Groq counts qwen's
# input tokens against 7,000 a minute.
IMAGE_TOKENS: Final = 2_400
MAX_RETRY_WAIT_S: Final = 60.0
CELL: Final = re.compile(r"\b([A-H])\s*([1-8])\b")


class VisionError(RuntimeError):
    """The vision model could not be reached or did not answer."""


@dataclass(frozen=True, slots=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def overlay_grid(image: Image.Image, cols: int, rows: int) -> Image.Image:
    """A copy with a labelled grid: columns A.., rows 1.., label at each cell's top left."""
    out = image.convert("RGB")
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default(size=max(14, out.width // 60))
    for c in range(cols):
        for r in range(rows):
            cell = grid_cell(c, r, cols, rows, Box(0, 0, out.width, out.height))
            draw.rectangle((cell.left, cell.top, cell.right, cell.bottom), outline=GRID_COLOUR)
            draw.text(
                (cell.left + 4, cell.top + 2), f"{chr(65 + c)}{r + 1}", fill=GRID_COLOUR, font=font
            )
    return out


def grid_cell(col: int, row: int, cols: int, rows: int, area: Box) -> Box:
    return Box(
        area.left + col * area.width // cols,
        area.top + row * area.height // rows,
        area.left + (col + 1) * area.width // cols,
        area.top + (row + 1) * area.height // rows,
    )


def parse_cell(answer: str, cols: int, rows: int) -> tuple[int, int] | None:
    """`F5` -> (5, 4); None for NONE or anything off the grid."""
    match = CELL.search(answer.upper())
    if match is None:
        return None
    col, row = ord(match[1]) - 65, int(match[2]) - 1
    return (col, row) if col < cols and row < rows else None


def ink_centre(image: Image.Image, area: Box) -> tuple[int, int]:
    """The centre of what is drawn in `area`, else its middle.

    The background is the usual grey of the ring around `area` (one area wide), which
    a button filling the area does not reach; inside the area, pixels unlike it are
    what is drawn.
    """
    middle = (area.left + area.right) // 2, (area.top + area.bottom) // 2
    grey = image.convert("L")
    ring = Box(
        max(0, area.left - area.width),
        max(0, area.top - area.height),
        min(image.width, area.right + area.width),
        min(image.height, area.bottom + area.height),
    )
    around = grey.crop((ring.left, ring.top, ring.right, ring.bottom))
    edge = [
        around.getpixel((x, y)) for x in range(around.width) for y in (0, around.height - 1)
    ] + [around.getpixel((x, y)) for y in range(around.height) for x in (0, around.width - 1)]
    if not edge:
        return middle
    background = statistics.median(float(cast(int, value)) for value in edge)
    inside = grey.crop((area.left, area.top, area.right, area.bottom))
    values = list(inside.tobytes())  # one byte per pixel, row by row
    ink = [i for i, value in enumerate(values) if abs(value - background) > INK_CONTRAST]
    if not ink:
        return middle
    xs = [i % inside.width for i in ink]
    ys = [i // inside.width for i in ink]
    return area.left + round(statistics.mean(xs)), area.top + round(statistics.mean(ys))


def _scaled(image: Image.Image, width: int) -> Image.Image:
    if image.width <= width:
        return image
    height = round(image.height * width / image.width)
    return image.resize((width, height), Image.Resampling.LANCZOS)  # pyright: ignore[reportUnknownMemberType]


class Vision:
    """qwen3.8-27b on Groq, through the shared rate budget."""

    def __init__(
        self, budget: HeaderBudget | None = None, client: httpx.AsyncClient | None = None
    ) -> None:
        self._budget = budget
        self._client = client

    async def ask(self, image: Image.Image, question: str, max_tokens: int = 400) -> str:
        """Any question about the screenshot, answered in words."""
        return await self._send(_scaled(image, SEND_WIDTH), question, max_tokens)

    async def locate(self, image: Image.Image, what: str) -> tuple[int, int] | None:
        """Where `what` is in `image` (its pixels), or None when it is not there."""
        whole = Box(0, 0, image.width, image.height)
        cols, rows = COARSE
        sent = _scaled(image, SEND_WIDTH)
        answer = await self._send(overlay_grid(sent, cols, rows), _grid_question(what, cols, rows))
        coarse = parse_cell(answer, cols, rows)
        if coarse is None:
            return None
        cell = grid_cell(*coarse, cols, rows, whole)
        margin_x, margin_y = round(cell.width * CROP_MARGIN), round(cell.height * CROP_MARGIN)
        area = Box(
            max(0, cell.left - margin_x),
            max(0, cell.top - margin_y),
            min(image.width, cell.right + margin_x),
            min(image.height, cell.bottom + margin_y),
        )
        crop = image.crop((area.left, area.top, area.right, area.bottom))
        zoom = 768 / crop.width
        size = (768, round(crop.height * zoom))
        crop = crop.resize(size, Image.Resampling.LANCZOS)  # pyright: ignore[reportUnknownMemberType]
        cols, rows = FINE
        answer = await self._send(overlay_grid(crop, cols, rows), _grid_question(what, cols, rows))
        fine = parse_cell(answer, cols, rows)
        target = grid_cell(*fine, cols, rows, area) if fine else area
        return ink_centre(image, target)

    async def _send(self, image: Image.Image, question: str, max_tokens: int = 40) -> str:
        key = os.environ.get(KEY_ENV)
        if not key:
            raise VisionError(f"set {KEY_ENV} to let Arcee look at the screen")
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=85)
        picture = base64.b64encode(buffer.getvalue()).decode()
        body = {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{picture}"},
                        },
                    ],
                }
            ],
        }
        if self._budget is not None:
            wait = self._budget.wait_s(MODEL, IMAGE_TOKENS + max_tokens)
            if wait > 0:
                await asyncio.sleep(wait)
        client = self._client or httpx.AsyncClient(timeout=60)
        try:
            response = await client.post(URL, json=body, headers={"Authorization": f"Bearer {key}"})
            if response.status_code == 429:
                # Over the minute's input tokens: wait as long as Groq says, then once more.
                retry = float(response.headers.get("retry-after") or MAX_RETRY_WAIT_S)
                await asyncio.sleep(min(retry, MAX_RETRY_WAIT_S))
                response = await client.post(
                    URL, json=body, headers={"Authorization": f"Bearer {key}"}
                )
        except httpx.HTTPError as exc:
            raise VisionError(f"could not reach Groq: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if self._budget is not None:
            self._budget.observe(MODEL, response.headers)
        data = cast(dict[str, Any], response.json())
        if response.status_code != 200:
            error = cast(dict[str, Any], data.get("error") or {})
            raise VisionError(
                f"the vision model refused: {error.get('message', response.status_code)}"
            )
        if self._budget is not None:
            usage = cast(dict[str, Any], data.get("usage") or {})
            self._budget.use(MODEL, int(usage.get("total_tokens", 0)))
        message = cast(dict[str, Any], data["choices"][0]["message"])
        return str(message.get("content") or "").strip()


def _grid_question(what: str, cols: int, rows: int) -> str:
    last_col, last_row = chr(64 + cols), rows
    return (
        f"The screenshot has a magenta grid: columns A to {last_col}, rows 1 to {last_row}, "
        f"each cell labelled at its top left. Which cell holds the centre of: {what}? "
        "Reply with the cell label only, like C4, or NONE if it is not on the screen."
    )
