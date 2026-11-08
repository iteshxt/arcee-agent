# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false
"""What documents need from GTK's own libraries: Markdown blocks as an A4 PDF, laid
out by Pango and drawn by Cairo; and images Pillow cannot open (HEIC), by GdkPixbuf."""

import html
from pathlib import Path
from typing import Any

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Pango, PangoCairo  # noqa: E402

from arcee.documents import Run  # noqa: E402


def _pango_runs(items: list[Run]) -> str:
    out = []
    for text, bold, italic, code in items:
        piece = html.escape(text, quote=False)
        piece = f"<tt>{piece}</tt>" if code else piece
        piece = f"<i>{piece}</i>" if italic else piece
        out.append(f"<b>{piece}</b>" if bold else piece)
    return "".join(out)


def write_pdf(parts: list[tuple[str, Any]], target: Path, title: str = "") -> None:
    """`parts` (documents.blocks) as an A4 PDF at `target`."""
    width, height, margin = 595.0, 842.0, 64.0  # A4 in points
    surface = cairo.PDFSurface(str(target), width, height)
    if title:
        surface.set_metadata(cairo.PDFMetadata.TITLE, title)
    surface.set_metadata(cairo.PDFMetadata.CREATOR, "Arcee")
    cr = cairo.Context(surface)
    sizes = {"h1": 20, "h2": 16, "h3": 14, "h4": 12, "h5": 11, "h6": 11}
    y = margin

    def paragraph(
        markup: str, size: float, indent: float = 0, after: float = 8, columns: int = 0
    ) -> None:
        nonlocal y
        layout = PangoCairo.create_layout(cr)
        if columns > 1:
            step = (width - 2 * margin) / columns
            tabs = Pango.TabArray.new(columns - 1, True)
            for n in range(columns - 1):
                tabs.set_tab(n, Pango.TabAlign.LEFT, round(step * (n + 1)))
            layout.set_tabs(tabs)
        font = Pango.FontDescription.from_string(f"Sans {size}")
        layout.set_font_description(font)
        layout.set_width(int((width - 2 * margin - indent) * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_markup(markup, -1)
        it = layout.get_iter()
        while True:
            _, logical = it.get_line_extents()
            line_h = logical.height / Pango.SCALE
            if y + line_h > height - margin:
                cr.show_page()
                y = margin
            baseline = it.get_baseline() / Pango.SCALE - logical.y / Pango.SCALE
            cr.move_to(margin + indent + logical.x / Pango.SCALE, y + baseline)
            PangoCairo.show_layout_line(cr, it.get_line_readonly())
            y += line_h
            if not it.next_line():
                break
        y += after

    for kind, value in parts:
        if kind in sizes:
            y += 6
            paragraph(f"<b>{_pango_runs(value)}</b>", sizes[kind], after=6)
        elif kind == "p":
            paragraph(_pango_runs(value), 11)
        elif kind == "li":
            paragraph(f"{value[0]}  {_pango_runs(value[1])}", 11, indent=18, after=3)
        elif kind == "quote":
            paragraph(f"<i>{_pango_runs(value)}</i>", 11, indent=24)
        elif kind == "pre":
            paragraph(f"<tt>{html.escape(value)}</tt>", 9.5, indent=12)
        elif kind == "table":
            for r, row in enumerate(value):
                cells = "\t".join(html.escape(c) for c in row)
                markup = f"<b>{cells}</b>" if r == 0 else cells
                paragraph(markup, 10.5, after=2, columns=len(value[0]))
            y += 6
    surface.finish()


def pixbuf_image(path: Path) -> Any:
    """An image GDK's loaders can read (HEIC through libheif), as a Pillow image."""
    from PIL import Image

    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
    mode = "RGBA" if pixbuf.get_has_alpha() else "RGB"
    size = (pixbuf.get_width(), pixbuf.get_height())
    return Image.frombytes(mode, size, pixbuf.get_pixels(), "raw", mode, pixbuf.get_rowstride())
