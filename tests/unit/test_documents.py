"""Documents read and made, recent files, drives, photos, printing."""

import importlib.util
import json
import shutil
import zipfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import DOCS, filters_for
from arcee.documents import extract_text, markdown_html
from arcee.models.base import Message
from arcee.tools.base import NoArgs, ToolContext
from arcee.tools.commands import Done
from arcee.tools.documents import (
    DocArgs,
    DocCreate,
    FsDrives,
    FsRecent,
    ImageArgs,
    ImageEdit,
    PrintArgs,
    PrintFile,
    RecentArgs,
    drives,
)
from arcee.tools.files import FsCopy, FsRead, MoveArgs, ReadArgs

TESSERACT = shutil.which("tesseract") is not None
# PDFs are drawn with the system's Pango and Cairo (python3-cairo, PyGObject): CI's
# plain Python has neither.
CAIRO = all(importlib.util.find_spec(name) is not None for name in ("cairo", "gi"))


def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(task_id="t", home=tmp_path)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(path: Path, *paragraphs: str) -> Path:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
        )
    return path


async def test_a_word_file_is_read_without_libreoffice(tmp_path: Path) -> None:
    path = docx(tmp_path / "contract.docx", "Notice period: 30 days.", "Signed in Jaipur.")
    result = await FsRead().run(ReadArgs(path="contract.docx"), ctx(tmp_path))
    assert result.data is not None
    assert result.data["content"] == "Notice period: 30 days.\nSigned in Jaipur."
    assert path.exists()


async def test_slides_are_read_in_order(tmp_path: Path) -> None:
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    path = tmp_path / "deck.pptx"
    with zipfile.ZipFile(path, "w") as z:
        for n, words in ((2, "Second"), (1, "First"), (10, "Tenth")):
            z.writestr(
                f"ppt/slides/slide{n}.xml",
                f'<p:sld xmlns:p="p" xmlns:a="{a}">'
                f"<a:p><a:r><a:t>{words}</a:t></a:r></a:p></p:sld>",
            )
    text, how = await extract_text(path)
    assert how == "office"
    assert text == "--- Slide 1 ---\nFirst\n\n--- Slide 2 ---\nSecond\n\n--- Slide 10 ---\nTenth"


async def test_a_broken_document_says_so(tmp_path: Path) -> None:
    (tmp_path / "bad.docx").write_bytes(b"not a zip")
    result = await FsRead().run(ReadArgs(path="bad.docx"), ctx(tmp_path))
    assert result.error is not None
    assert "not a readable .docx file" in result.error


def test_markdown_becomes_a_document_page() -> None:
    page = markdown_html(
        "# Leave\n\nDear **Sir**,\nI request *leave*.\n\n- one\n- two\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |",
        "Leave",
    )
    assert "<h1>Leave</h1>" in page
    assert "<p>Dear <b>Sir</b>,<br>I request <i>leave</i>.</p>" in page
    assert "<ul><li>one</li><li>two</li></ul>" in page
    assert "<th>a</th>" in page
    assert "<td>2</td>" in page


@pytest.mark.parametrize(
    "suffix",
    [
        ".docx",
        pytest.param(".pdf", marks=pytest.mark.skipif(not CAIRO, reason="no Pango/Cairo")),
    ],
)
async def test_a_document_is_made_and_read_back(tmp_path: Path, suffix: str) -> None:
    tool = DocCreate()
    args = DocArgs(
        path=f"~/Documents/Letter{suffix}",
        content="# Leave application\n\nI request leave on **12 November**.",
    )
    assert tool.tier_for(args, Policy()) is Tier.CONFIRM
    made = await tool.run(args, ctx(tmp_path))
    assert made.status == "ok", made.error
    read = await FsRead().run(ReadArgs(path=f"~/Documents/Letter{suffix}"), ctx(tmp_path))
    assert read.data is not None
    assert "Leave application" in read.data["content"]
    assert "12 November" in read.data["content"]
    again = await tool.run(args, ctx(tmp_path))
    assert again.error is not None
    assert "already exists" in again.error


@pytest.mark.skipif(not TESSERACT, reason="Tesseract is not installed")
async def test_a_photo_of_text_is_read_by_ocr(tmp_path: Path) -> None:
    image = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(image).text(
        (20, 60), "TOTAL 450 RUPEES", fill="black", font=ImageFont.load_default(size=60)
    )
    image.save(tmp_path / "receipt.png")
    result = await FsRead().run(ReadArgs(path="receipt.png"), ctx(tmp_path))
    assert result.data is not None
    assert "TOTAL 450 RUPEES" in result.data["content"]
    assert result.data["read_by"].startswith("OCR")


XBEL = """<?xml version="1.0" encoding="UTF-8"?>
<xbel version="1.0" xmlns:bookmark="http://www.freedesktop.org/standards/desktop-bookmarks"
      xmlns:mime="http://www.freedesktop.org/standards/shared-mime-info">
  <bookmark href="file://{home}/notes.md" added="2026-11-07T08:00:00Z"
            modified="2026-11-07T09:30:00Z" visited="2026-11-07T09:30:00Z">
    <info><metadata owner="http://freedesktop.org">
      <mime:mime-type type="text/markdown"/>
      <bookmark:applications>
        <bookmark:application name="Text Editor" exec="x"/>
      </bookmark:applications>
    </metadata></info>
  </bookmark>
  <bookmark href="file://{home}/ticket%20home.pdf" added="2026-11-08T05:00:00Z"
            modified="2026-11-08T05:00:00Z" visited="2026-11-08T05:00:00Z">
    <info><metadata owner="http://freedesktop.org">
      <mime:mime-type type="application/pdf"/>
    </metadata></info>
  </bookmark>
  <bookmark href="https://example.com" added="2026-11-08T05:00:00Z"/>
</xbel>
"""


async def test_recent_files_newest_first_and_by_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    share = tmp_path / ".local" / "share"
    share.mkdir(parents=True)
    (share / "recently-used.xbel").write_text(XBEL.format(home=tmp_path))
    (tmp_path / "notes.md").write_text("x")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    tool = FsRecent(today=lambda: date(2026, 11, 8))

    everything = await tool.run(RecentArgs(), ctx(tmp_path))
    pdfs = await tool.run(RecentArgs(kind="pdf"), ctx(tmp_path))
    yesterday = await tool.run(RecentArgs(when="yesterday"), ctx(tmp_path))

    assert everything.data is not None
    assert everything.data["count"] == 2
    assert everything.data["files"][0].endswith("~/ticket home.pdf (no longer there)")
    assert pdfs.data is not None
    assert pdfs.data["count"] == 1
    assert yesterday.data is not None
    (line,) = yesterday.data["files"]
    assert line.endswith("~/notes.md in Text Editor")


LSBLK = json.dumps(
    {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "rm": False,
                "hotplug": False,
                "children": [
                    {"name": "nvme0n1p2", "mountpoint": "/", "rm": False, "hotplug": False}
                ],
            },
            {
                "name": "sda",
                "rm": True,
                "hotplug": True,
                "size": "29G",
                "children": [
                    {
                        "name": "sda1",
                        "label": "PENDRIVE",
                        "mountpoint": "/media/me/PENDRIVE",
                        "size": "29G",
                        "fsavail": "12G",
                        "rm": False,
                        "hotplug": False,
                    }
                ],
            },
        ]
    }
)


def test_only_plugged_in_drives_are_named() -> None:
    assert drives(LSBLK, "me") == [
        {"name": "PENDRIVE", "path": "/media/me/PENDRIVE", "size": "29G", "free": "12G"}
    ]


class Fixed:
    def __init__(self, replies: dict[str, Done]) -> None:
        self.replies = replies
        self.ran: list[str] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        limit_s: float = 15,
        stdin: bytes | None = None,
        quiet: bool = False,
    ) -> Done:
        line = " ".join(argv)
        self.ran.append(line)
        return next((d for k, d in self.replies.items() if line.startswith(k)), Done(0, "", ""))

    def has(self, program: str) -> bool:
        return True


async def test_drives_tool_says_when_none_is_plugged_in(tmp_path: Path) -> None:
    tool = FsDrives(Fixed({"lsblk": Done(0, '{"blockdevices": []}', "")}))
    result = await tool.run(NoArgs(), ctx(tmp_path))
    assert result.summary == "No USB drive or SD card is plugged in"


async def test_copying_outside_home_needs_a_mounted_drive(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    result = await FsCopy().run(
        MoveArgs(src="~/a.txt", dst=f"/media/{tmp_path.name}/NOPE/a.txt"), ctx(tmp_path)
    )
    assert result.error == "both paths must be inside your home folder or on a plugged-in drive"


async def test_photos_are_resized_and_converted_into_new_files(tmp_path: Path) -> None:
    Image.new("RGBA", (2000, 1000), "red").save(tmp_path / "sunset.png")
    tool = ImageEdit()
    result = await tool.run(
        ImageArgs(paths=["sunset.png"], width=1080, format="jpg"), ctx(tmp_path)
    )
    again = await tool.run(ImageArgs(paths=["sunset.png"], width=1080, format="jpg"), ctx(tmp_path))
    assert result.data == {"made": ["~/sunset-1080px.jpg"]}
    assert again.data == {"made": ["~/sunset-1080px-2.jpg"]}
    with Image.open(tmp_path / "sunset-1080px.jpg") as made:
        assert (made.size, made.format) == ((1080, 540), "JPEG")
    assert (tmp_path / "sunset.png").exists()
    nothing = await tool.run(ImageArgs(paths=["sunset.png"]), ctx(tmp_path))
    assert nothing.error == "say a width, a height or a format"


async def test_printing_asks_and_needs_a_printer(tmp_path: Path) -> None:
    (tmp_path / "ticket.pdf").write_bytes(b"%PDF")
    none = PrintFile(Fixed({"lpstat": Done(1, "", "lpstat: No destinations added.")}))
    result = await none.run(PrintArgs(path="ticket.pdf"), ctx(tmp_path))
    assert result.error == "no printer is set up: add one in Settings → Printers first"
    office = Fixed({"lpstat": Done(0, "printer HP is idle.\nsystem default destination: HP\n", "")})
    tool = PrintFile(office)
    args = PrintArgs(path="ticket.pdf", copies=2)
    assert tool.tier_for(args, Policy()) is Tier.CONFIRM
    assert tool.sentence(args) == "Print 2 copies of ticket.pdf?"
    await tool.run(args, ctx(tmp_path))
    assert office.ran[-1] == f"lp -n 2 -- {tmp_path / 'ticket.pdf'}"


@pytest.mark.parametrize(
    "request_",
    [
        "Create a Word document with this letter",
        "Resize this photo to 1080 pixels wide",
        "Convert these HEIC photos to JPG",
        "Print this document",
        "Add up the receipts in my Downloads",
    ],
)
def test_document_requests_offer_the_document_tools(request_: str) -> None:
    assert set(DOCS) <= set(filters_for([Message("user", request_)]))
