"""Recent files, drives, documents, images and printing (the FILE and DOC tasks).

`fs.recent` reads GNOME's own list of files recently opened or saved (every GTK app,
the file chooser, Files, Text Editor write to it), so "what was I working on
yesterday?" needs no search of the disk. `fs.drives` names the USB drives and SD cards
plugged in, which fs.copy may write to. `doc.create` makes a Word, Writer or PDF
document from Markdown; `image.edit` resizes and converts photos into new files;
`print.file` prints, after a yes.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Final, Literal
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

from pydantic import Field

from arcee.context.periods import span
from arcee.core.policy import Policy, Tier
from arcee.documents import DocumentError, make_document
from arcee.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.commands import Commands, SystemCommands
from arcee.tools.files import display, resolve

RECENT_LIMIT: Final = 30
_BOOKMARK_NS: Final = "{http://www.freedesktop.org/standards/desktop-bookmarks}"
_MIME_NS: Final = "{http://www.freedesktop.org/standards/shared-mime-info}"


def recent_path(home: Path) -> Path:
    data = os.environ.get("XDG_DATA_HOME")
    return Path(data) / "recently-used.xbel" if data else home / ".local/share/recently-used.xbel"


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_files(xbel: Path) -> list[dict[str, Any]]:
    """GNOME's recently used files, newest first: {path, when, type, apps}."""
    try:
        root = ElementTree.parse(xbel).getroot()
    except OSError, ElementTree.ParseError:
        return []
    found: list[dict[str, Any]] = []
    for mark in root.iter("bookmark"):
        href = mark.get("href", "")
        if not href.startswith("file://"):
            continue
        times = [_when(mark.get(k)) for k in ("modified", "visited", "added")]
        when = max((t for t in times if t is not None), default=None)
        if when is None:
            continue
        mime = next(mark.iter(f"{_MIME_NS}mime-type"), None)
        apps = [a.get("name", "") for a in mark.iter(f"{_BOOKMARK_NS}application")]
        found.append(
            {
                "path": Path(unquote(urlparse(href).path)),
                "when": when,
                "type": mime.get("type", "") if mime is not None else "",
                "apps": [a for a in apps if a],
            }
        )
    return sorted(found, key=lambda f: f["when"], reverse=True)


class RecentArgs(ToolArgs):
    when: str = Field(
        default="", description='"today", "yesterday", "this week", ...; empty for the latest'
    )
    kind: str = Field(
        default="", description="Only this kind: pdf, image, document, video, text, or a word"
    )


_KINDS: Final = {
    "pdf": ("application/pdf",),
    "image": ("image/",),
    "photo": ("image/",),
    "video": ("video/",),
    "music": ("audio/",),
    "text": ("text/",),
    "document": ("application/vnd.openxmlformats", "application/vnd.oasis", "application/msword"),
}


class FsRecent(Tool[RecentArgs]):
    name = "fs.recent"
    description = "Files recently opened or saved on this computer, newest first."
    usage = Usage(
        when='"What was I working on yesterday?", "show my recent files", "the PDF I opened '
        'this morning".',
        avoid="What the user asked Arcee (memory.history).",
        example='fs.recent(when="yesterday")',
    )
    args_model = RecentArgs
    read_only: ClassVar[bool] = True
    timeout_s = 10

    def __init__(self, today: Any = None) -> None:
        self._today = today

    async def run(self, args: RecentArgs, ctx: ToolContext) -> ToolResult:
        files = await asyncio.to_thread(recent_files, recent_path(ctx.home))
        label = "recently"
        if args.when:
            today = self._today() if self._today else datetime.now().date()
            days = span(args.when, today)
            if days is None:
                return ToolResult.fail(f"“{args.when}” is not a time fs.recent knows")
            start = datetime.combine(days.first, datetime.min.time()).astimezone()
            end = datetime.combine(days.last + timedelta(days=1), datetime.min.time()).astimezone()
            files = [f for f in files if start <= f["when"] < end]
            label = days.label
        if args.kind:
            prefixes = _KINDS.get(args.kind.lower().rstrip("s"))
            key = args.kind.lower()
            files = [
                f
                for f in files
                if (prefixes and f["type"].startswith(prefixes))
                or (not prefixes and (key in f["type"] or f["path"].suffix.lower() == f".{key}"))
            ]
        lines: list[str] = []
        for f in files[:RECENT_LIMIT]:
            local = f["when"].astimezone().strftime("%a %d %b %H:%M")
            gone = "" if f["path"].exists() else " (no longer there)"
            app = f" in {f['apps'][-1]}" if f["apps"] else ""
            lines.append(f"{local} {display(f['path'], ctx)}{app}{gone}")
        data: dict[str, Any] = {"when": label, "count": len(files), "files": lines}
        return ToolResult.ok(f"{len(files)} files used {label}", data=data)


# --- fs.drives ------------------------------------------------------------------------------


def drives(lsblk_json: str, user: str) -> list[dict[str, Any]]:
    """Mounted removable drives (USB sticks, SD cards, external disks) from lsblk."""
    found: list[dict[str, Any]] = []

    def walk(devices: list[dict[str, Any]], removable: bool) -> None:
        for dev in devices:
            outer = removable or bool(dev.get("rm")) or bool(dev.get("hotplug"))
            mount = dev.get("mountpoint") or ""
            if outer and mount.startswith((f"/media/{user}/", f"/run/media/{user}/")):
                found.append(
                    {
                        "name": dev.get("label") or Path(mount).name,
                        "path": mount,
                        "size": dev.get("size"),
                        "free": dev.get("fsavail"),
                    }
                )
            walk(dev.get("children") or [], outer)

    walk(json.loads(lsblk_json or "{}").get("blockdevices") or [], False)
    return found


class FsDrives(Tool[NoArgs]):
    name = "fs.drives"
    description = "The USB drives, SD cards and external disks plugged in, with free space."
    usage = Usage(
        when='"Back up my Documents to the USB drive", "is my pen drive plugged in?": find '
        "its path, then fs.copy to it.",
        example="fs.drives()",
    )
    args_model = NoArgs
    read_only: ClassVar[bool] = True
    timeout_s = 10

    def __init__(self, commands: Commands | None = None) -> None:
        self._commands = commands or SystemCommands()

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        done = await self._commands.run(
            ["lsblk", "-J", "-o", "NAME,LABEL,MOUNTPOINT,SIZE,FSAVAIL,RM,HOTPLUG"]
        )
        if not done.ok:
            return ToolResult.fail(f"could not list the drives: {done.why()}")
        found = drives(done.out, ctx.home.name)
        if not found:
            return ToolResult.ok("No USB drive or SD card is plugged in", data={"drives": []})
        return ToolResult.ok(f"{len(found)} drives plugged in", data={"drives": found})


# --- doc.create -----------------------------------------------------------------------------


class DocArgs(ToolArgs):
    path: str = Field(description="The new document, ending .docx, .odt or .pdf")
    content: str = Field(
        min_length=1,
        max_length=200_000,
        description="The text in Markdown: # headings, paragraphs, - lists, **bold**, tables",
    )
    title: str = Field(default="", description="The document's title (its properties)")


class DocCreate(Tool[DocArgs]):
    name = "doc.create"
    description = "Make a Word (.docx), LibreOffice (.odt) or PDF document from text."
    usage = Usage(
        when='"Create a Word document with this letter", "save the summary as a PDF".',
        avoid="Plain text or Markdown files: fs.write.",
        example='doc.create(path="~/Documents/Letter.docx", content="# Leave application\\n...")',
    )
    args_model = DocArgs
    tier = Tier.CONFIRM
    timeout_s = 90

    def tier_for(self, args: DocArgs, policy: Policy) -> Tier:
        return Tier.AUTO if policy.in_workspace(args.path) else Tier.CONFIRM

    def sentence(self, args: DocArgs) -> str:
        return f"Create {args.path}?"

    async def run(self, args: DocArgs, ctx: ToolContext) -> ToolResult:
        target = resolve(args.path, ctx)
        if ctx.home not in target.parents:
            return ToolResult.fail("documents can be made inside your home folder only")
        if target.exists():
            return ToolResult.fail(f"{display(target, ctx)} already exists; choose another name")
        try:
            made = await make_document(args.content, target, args.title)
        except DocumentError as exc:
            return ToolResult.fail(str(exc))
        size = made.stat().st_size
        return ToolResult.ok(
            f"Created {display(made, ctx)}", data={"path": display(made, ctx), "bytes": size}
        )


# --- image.edit -----------------------------------------------------------------------------


class ImageArgs(ToolArgs):
    paths: list[str] = Field(min_length=1, max_length=200, description="The images")
    width: int | None = Field(default=None, ge=16, le=20_000, description="New width in px")
    height: int | None = Field(default=None, ge=16, le=20_000, description="New height in px")
    format: Literal["", "jpg", "png", "webp"] = Field(
        default="", description="Convert to this; empty keeps the format (HEIC becomes jpg)"
    )
    quality: int = Field(default=88, ge=30, le=100, description="JPEG/WebP quality")


def _open_image(path: Path) -> Any:
    from PIL import Image

    try:
        return Image.open(path)
    except Exception:
        # HEIC and others Pillow cannot read: GDK's loaders (libheif) can.
        from arcee.document_pdf import pixbuf_image

        return pixbuf_image(path)


def edit_image(source: Path, args: ImageArgs) -> Path:
    """A new file beside `source`, resized and/or converted; never overwrites."""
    from PIL import Image, ImageOps

    opened: Image.Image = _open_image(source)
    image: Image.Image = ImageOps.exif_transpose(opened) or opened
    if args.width or args.height:
        w, h = image.size
        width = args.width or round(w * (args.height or h) / h)
        height = args.height or round(h * width / w)
        image = image.resize((width, height))  # pyright: ignore[reportUnknownMemberType]
    fmt = args.format or source.suffix.lower().lstrip(".")
    fmt = {"jpeg": "jpg", "heic": "jpg", "heif": "jpg", "tif": "png", "tiff": "png"}.get(fmt, fmt)
    if fmt not in ("jpg", "png", "webp"):
        fmt = "png"
    tag = f"-{image.size[0]}px" if (args.width or args.height) else ""
    target = source.with_name(f"{source.stem}{tag}.{fmt}")
    n = 2
    while target.exists():
        target = source.with_name(f"{source.stem}{tag}-{n}.{fmt}")
        n += 1
    if fmt == "jpg" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    save: dict[str, Any] = {"quality": args.quality} if fmt in ("jpg", "webp") else {}
    image.save(target, "JPEG" if fmt == "jpg" else fmt.upper(), **save)
    return target


class ImageEdit(Tool[ImageArgs]):
    name = "image.edit"
    description = "Resize photos or convert them (HEIC, PNG, WebP to JPG...) into new files."
    usage = Usage(
        when='"Resize this photo to 1080 pixels wide", "convert these HEIC photos to JPG".',
        mistakes="Giving both width and height stretches the picture: give one.",
        example='image.edit(paths=["~/Pictures/a.heic"], format="jpg")',
    )
    args_model = ImageArgs
    timeout_s = 120

    async def run(self, args: ImageArgs, ctx: ToolContext) -> ToolResult:
        if not (args.width or args.height or args.format):
            return ToolResult.fail("say a width, a height or a format")
        made: list[str] = []
        problems: dict[str, str] = {}
        for raw in args.paths:
            source = resolve(raw, ctx)
            if not source.is_file():
                problems[raw] = "not a file"
                continue
            if ctx.home not in source.parents:
                problems[raw] = "outside your home folder"
                continue
            try:
                made.append(display(await asyncio.to_thread(edit_image, source, args), ctx))
            except Exception as exc:
                problems[raw] = f"{type(exc).__name__}: {exc}"
        data: dict[str, Any] = {"made": made}
        if problems:
            data["problems"] = problems
        if not made:
            return ToolResult.fail(f"no image was made: {problems}")
        return ToolResult.ok(f"Made {len(made)} images", data=data)


# --- print.file -----------------------------------------------------------------------------


class PrintArgs(ToolArgs):
    path: str = Field(default="", description="The file to print; empty to list the printers")
    copies: int = Field(default=1, ge=1, le=50)
    printer: str = Field(default="", description="A printer's name; empty for the default")


class PrintFile(Tool[PrintArgs]):
    name = "print.file"
    description = "Print a file (PDF, text, image), or list the printers and whether they are on."
    usage = Usage(
        when='"Print this document", "is my printer online?" (no path).',
        mistakes="Word and Writer files: make a PDF of them first (doc.create, or LibreOffice).",
        example='print.file(path="~/Documents/ticket.pdf")',
    )
    args_model = PrintArgs
    timeout_s = 30

    def __init__(self, commands: Commands | None = None) -> None:
        self._commands = commands or SystemCommands()

    def tier_for(self, args: PrintArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM if args.path else Tier.AUTO

    def sentence(self, args: PrintArgs) -> str:
        copies = f"{args.copies} copies of " if args.copies > 1 else ""
        where = f" on {args.printer}" if args.printer else ""
        return f"Print {copies}{args.path}{where}?"

    async def run(self, args: PrintArgs, ctx: ToolContext) -> ToolResult:
        status = await self._commands.run(["lpstat", "-p", "-d"])
        text = status.out + status.err
        printers = [line.split()[1] for line in text.splitlines() if line.startswith("printer ")]
        if not printers:
            return ToolResult.fail("no printer is set up: add one in Settings → Printers first")
        if not args.path:
            lines = [line for line in text.splitlines() if line.startswith(("printer", "system"))]
            return ToolResult.ok(f"{len(printers)} printers", data={"printers": lines})
        path = resolve(args.path, ctx)
        if not path.is_file():
            return ToolResult.fail(f"{display(path, ctx)} is not a file")
        argv = ["lp", "-n", str(args.copies)]
        if args.printer:
            argv += ["-d", args.printer]
        done = await self._commands.run([*argv, "--", str(path)])
        if not done.ok:
            return ToolResult.fail(f"could not print: {done.why()}")
        return ToolResult.ok(
            f"Sent {display(path, ctx)} to the printer", data={"job": done.out.strip()}
        )


def document_tools(commands: Commands | None = None) -> list[Tool[Any]]:
    return [FsRecent(), FsDrives(commands), DocCreate(), ImageEdit(), PrintFile(commands)]
