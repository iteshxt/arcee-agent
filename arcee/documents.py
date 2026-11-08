"""Documents as text, and text as documents (FILE-012, DOC-003, DOC-007).

Reading: PDFs by pdftotext (poppler-utils, on every Ubuntu desktop); Word, Writer and
PowerPoint files by their own XML, unzipped here (no LibreOffice start-up, about 3 s);
spreadsheets and old .doc/.xls/.rtf by LibreOffice when it is installed; photos and
scans by Tesseract (receipts, a photographed page). Writing: Markdown made into HTML
here, then into .docx, .odt or .pdf by LibreOffice.
"""

import asyncio
import html
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Final
from xml.etree import ElementTree

PDF: Final = frozenset({".pdf"})
OFFICE_XML: Final = frozenset({".docx", ".pptx", ".odt", ".odp"})
OFFICE_OTHER: Final = frozenset({".xlsx", ".xls", ".ods", ".doc", ".rtf", ".ppt", ".csv"})
IMAGES: Final = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"})
DOCUMENTS: Final = PDF | OFFICE_XML | OFFICE_OTHER | IMAGES
EXTRACT_TIMEOUT_S: Final = 60.0
_W: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A: Final = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_TEXT_NS: Final = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


class DocumentError(Exception):
    pass


def kind_of(path: Path) -> str | None:
    """ "pdf", "office", "image", or None for a file read as it is."""
    suffix = path.suffix.lower()
    if suffix in PDF:
        return "pdf"
    if suffix in OFFICE_XML | OFFICE_OTHER - {".csv"}:
        return "office"
    if suffix in IMAGES:
        return "image"
    return None


async def _run(*argv: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise DocumentError(f"{argv[0]} is not installed") from exc
    try:
        out, err = await asyncio.wait_for(process.communicate(), EXTRACT_TIMEOUT_S)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise DocumentError(f"{argv[0]} took over {EXTRACT_TIMEOUT_S:.0f} s") from exc
    if process.returncode != 0:
        lines = err.decode(errors="replace").strip().splitlines()
        raise DocumentError(lines[-1] if lines else f"{argv[0]} failed")
    return out.decode(errors="replace")


def _docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    lines: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


def _pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.findall(r"\d+", n)[-1]),
        )
        out: list[str] = []
        for name in slides:
            n = int(re.findall(r"\d+", name)[-1])
            root = ElementTree.fromstring(z.read(name))
            texts = ["".join(t.text or "" for t in p.iter(f"{_A}t")) for p in root.iter(f"{_A}p")]
            out.append(f"--- Slide {n} ---\n" + "\n".join(t for t in texts if t))
    return "\n\n".join(out)


def _odf(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("content.xml"))
    heads = (f"{_TEXT_NS}p", f"{_TEXT_NS}h")
    lines: list[str] = ["".join(node.itertext()) for node in root.iter() if node.tag in heads]
    return "\n".join(lines)


async def _libreoffice(path: Path) -> str:
    office = shutil.which("soffice") or shutil.which("libreoffice")
    spreadsheet = path.suffix.lower() in (".xlsx", ".xls", ".ods")
    part = "LibreOffice Calc" if spreadsheet else "LibreOffice Writer"
    if office is None or not _has_writer(office, "sc" if spreadsheet else "sw"):
        raise DocumentError(f"reading this kind of file needs {part}, which is not installed")
    target = "csv" if spreadsheet else "txt:Text (encoded):UTF8"
    with tempfile.TemporaryDirectory(prefix="arcee-doc-") as tmp:
        # Its own profile: a LibreOffice the user has open would otherwise take the job.
        profile = Path(tmp, "profile").as_uri()
        await _run(
            office,
            f"-env:UserInstallation={profile}",
            "--headless",
            "--convert-to",
            target,
            "--outdir",
            tmp,
            str(path),
        )
        return await asyncio.to_thread(_only_file, Path(tmp))


def _only_file(folder: Path) -> str:
    made = [p for p in folder.iterdir() if p.is_file()]
    if not made:
        raise DocumentError("LibreOffice made nothing from it")
    return made[0].read_text(errors="replace")


async def extract_text(path: Path, *, pages: int = 50) -> tuple[str, str]:
    """`path`'s text and how it was read ("pdf", "office", "ocr")."""
    suffix = path.suffix.lower()
    if suffix in PDF:
        text = await _run("pdftotext", "-layout", "-l", str(pages), str(path), "-")
        if text.strip():
            return text, "pdf"
        return await _ocr_pdf(path, pages), "ocr"  # a scanned PDF: pictures of pages
    try:
        if suffix == ".docx":
            return await asyncio.to_thread(_docx, path), "office"
        if suffix == ".pptx":
            return await asyncio.to_thread(_pptx, path), "office"
        if suffix in (".odt", ".odp"):
            return await asyncio.to_thread(_odf, path), "office"
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise DocumentError(f"not a readable {suffix} file ({exc})") from exc
    if suffix in OFFICE_OTHER:
        return await _libreoffice(path), "office"
    if suffix in IMAGES:
        return await _run("tesseract", str(path), "-", "--psm", "3"), "ocr"
    raise DocumentError(f"cannot read {suffix} files")


async def _ocr_pdf(path: Path, pages: int) -> str:
    with tempfile.TemporaryDirectory(prefix="arcee-ocr-") as tmp:
        last = str(min(pages, 10))
        await _run("pdftoppm", "-r", "200", "-l", last, "-png", str(path), f"{tmp}/p")
        images = await asyncio.to_thread(lambda: sorted(Path(tmp).glob("p*.png")))
        texts: list[str] = [
            await _run("tesseract", str(image), "-", "--psm", "3") for image in images
        ]
        return "\n\f".join(texts)


# --- Markdown as documents -------------------------------------------------------------------
#
# One parser for the Markdown models write, into blocks; then each format from them:
# HTML (LibreOffice's way in), .docx (its XML written here, no LibreOffice needed), and
# PDF (laid out by Pango, drawn by Cairo: GTK's own libraries, on every GNOME desktop).

Run = tuple[str, bool, bool, bool]  # text, bold, italic, code
_RUNS: Final = re.compile(r"\*\*(.+?)\*\*|(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])|`([^`]+)`")
_LIST: Final = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_TABLE_SEP: Final = re.compile(r"^[\s|:-]+$")


def runs(text: str) -> list[Run]:
    """**bold**, *italic* and `code` in a line, as runs."""
    out: list[Run] = []
    at = 0
    for m in _RUNS.finditer(text):
        if m.start() > at:
            out.append((text[at : m.start()], False, False, False))
        if m[1] is not None:
            out.append((m[1], True, False, False))
        elif m[2] is not None:
            out.append((m[2], False, True, False))
        else:
            out.append((m[3], False, False, True))
        at = m.end()
    if at < len(text):
        out.append((text[at:], False, False, False))
    return out


def blocks(text: str) -> list[tuple[str, Any]]:
    """Markdown as blocks: ("h1".."h6", runs), ("p", runs), ("li", (marker, runs)),
    ("quote", runs), ("pre", text), ("table", rows of cells)."""
    out: list[tuple[str, Any]] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(("pre", "\n".join(code)))
        elif m := re.match(r"^(#{1,6})\s+(.*)", line):
            out.append((f"h{len(m[1])}", runs(m[2].strip().rstrip("#").strip())))
        elif m := _LIST.match(line):
            marker = "•" if m[1] in "-*+" else m[1].rstrip(")").rstrip(".") + "."
            out.append(("li", (marker, runs(line[m.end() :].strip()))))
        elif (
            line.strip().startswith("|")
            and i + 1 < len(lines)
            and _TABLE_SEP.match(lines[i + 1])
            and "-" in lines[i + 1]
        ):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _TABLE_SEP.match(lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(("table", rows))
            continue
        elif line.startswith(">"):
            out.append(("quote", runs(line.lstrip("> ").strip())))
        elif line.strip():
            para = [line.strip()]
            while (
                i + 1 < len(lines)
                and lines[i + 1].strip()
                and not re.match(r"^(#|\s*([-*+]|\d+[.)])\s|>|```|\|)", lines[i + 1])
            ):
                i += 1
                para.append(lines[i].strip())
            # Lines are kept: a letter's "Yours sincerely,\nItesh" is two lines.
            out.append(("p", runs("\n".join(para))))
        i += 1
    return out


def _html_runs(items: list[Run]) -> str:
    out: list[str] = []
    for text, bold, italic, code in items:
        piece = html.escape(text, quote=False).replace("\n", "<br>")
        piece = f"<code>{piece}</code>" if code else piece
        piece = f"<i>{piece}</i>" if italic else piece
        out.append(f"<b>{piece}</b>" if bold else piece)
    return "".join(out)


def markdown_html(text: str, title: str = "") -> str:
    """A whole HTML page from Markdown."""
    body: list[str] = []
    items: list[str] = []
    for kind, value in blocks(text):
        if kind != "li" and items:
            body.append("<ul>" + "".join(items) + "</ul>")
            items = []
        if kind.startswith("h"):
            body.append(f"<{kind}>{_html_runs(value)}</{kind}>")
        elif kind == "p":
            body.append(f"<p>{_html_runs(value)}</p>")
        elif kind == "li":
            items.append(f"<li>{_html_runs(value[1])}</li>")
        elif kind == "quote":
            body.append(f"<blockquote>{_html_runs(value)}</blockquote>")
        elif kind == "pre":
            body.append(f"<pre>{html.escape(value)}</pre>")
        elif kind == "table":
            head = "".join(f"<th>{html.escape(c)}</th>" for c in value[0])
            rest = "".join(
                "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>"
                for row in value[1:]
            )
            body.append(f"<table border='1'><tr>{head}</tr>{rest}</table>")
    if items:
        body.append("<ul>" + "".join(items) + "</ul>")
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}"
        f"</title></head><body>{''.join(body)}</body></html>"
    )


# .docx: the parts Word needs, and nothing else. Headings use Word's own Heading styles
# (so its navigation pane and table of contents see them).
_XML: Final = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_OOXML: Final = "http://schemas.openxmlformats.org"
_ML: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml"
_DOCX_TYPES: Final = (
    f'{_XML}<Types xmlns="{_OOXML}/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/word/document.xml" ContentType="{_ML}.document.main+xml"/>'
    f'<Override PartName="/word/styles.xml" ContentType="{_ML}.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" '
    'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)
_RELATIONSHIPS: Final = f"{_OOXML}/package/2006/relationships"
_DOCX_RELS: Final = (
    f'{_XML}<Relationships xmlns="{_RELATIONSHIPS}">'
    f'<Relationship Id="rId1" Target="word/document.xml" '
    f'Type="{_OOXML}/officeDocument/2006/relationships/officeDocument"/>'
    f'<Relationship Id="rId2" Target="docProps/core.xml" '
    f'Type="{_OOXML}/package/2006/relationships/metadata/core-properties"/>'
    "</Relationships>"
)
_DOCX_DOC_RELS: Final = (
    f'{_XML}<Relationships xmlns="{_RELATIONSHIPS}">'
    f'<Relationship Id="rId1" Target="styles.xml" '
    f'Type="{_OOXML}/officeDocument/2006/relationships/styles"/>'
    "</Relationships>"
)


def _docx_styles() -> str:
    sizes = {1: 36, 2: 30, 3: 26, 4: 24, 5: 22, 6: 22}
    heads = "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
        f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        f'<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/>'
        f'<w:outlineLvl w:val="{n - 1}"/></w:pPr>'
        f'<w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr></w:style>'
        for n, size in sizes.items()
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{_W[1:-1]}">'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" '
        'w:cs="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
        '<w:spacing w:after="160" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
        '</w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        f'<w:name w:val="Normal"/><w:qFormat/></w:style>{heads}</w:styles>'
    )


def _docx_runs(items: list[Run]) -> str:
    out: list[str] = []
    for text, bold, italic, code in items:
        props = ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "")
        if code:
            props += '<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/>'
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        lines = [
            f'<w:t xml:space="preserve">{html.escape(line, quote=False)}</w:t>'
            for line in text.split("\n")
        ]
        out.append(f"<w:r>{rpr}{'<w:br/>'.join(lines)}</w:r>")
    return "".join(out)


def _docx_paragraph(items: list[Run], props: str = "") -> str:
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{_docx_runs(items)}</w:p>"


def _docx_table(rows: list[list[str]]) -> str:
    cells = "".join(
        "<w:tr>"
        + "".join(
            f"<w:tc>{_docx_paragraph(runs(cell) if r else [(cell, True, False, False)])}</w:tc>"
            for cell in row
        )
        + "</w:tr>"
        for r, row in enumerate(rows)
    )
    border = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>{border}'
        f"</w:tblBorders></w:tblPr>{cells}</w:tbl><w:p/>"
    )


def _docx_body(text: str) -> str:
    out: list[str] = []
    for kind, value in blocks(text):
        if kind.startswith("h"):
            out.append(_docx_paragraph(value, f'<w:pStyle w:val="Heading{kind[1]}"/>'))
        elif kind == "p":
            out.append(_docx_paragraph(value))
        elif kind == "li":
            marker, items = value
            indent = '<w:spacing w:after="60"/><w:ind w:left="720" w:hanging="360"/>'
            out.append(_docx_paragraph([(marker + "\t", False, False, False), *items], indent))
        elif kind == "quote":
            quoted = [(t, b, True, c) for t, b, _, c in value]
            out.append(_docx_paragraph(quoted, '<w:ind w:left="720"/>'))
        elif kind == "pre":
            tight = '<w:spacing w:after="0"/>'
            out.extend(
                _docx_paragraph([(line, False, False, True)], tight) for line in value.split("\n")
            )
        elif kind == "table":
            out.append(_docx_table(value))
    return "".join(out)


def write_docx(markdown: str, target: Path, title: str = "") -> None:
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{_W[1:-1]}">'
        f'<w:body>{_docx_body(markdown)}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" '
        'w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>'
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{html.escape(title)}</dc:title><dc:creator>Arcee</dc:creator>"
        "</cp:coreProperties>"
    )
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _DOCX_TYPES)
        z.writestr("_rels/.rels", _DOCX_RELS)
        z.writestr("word/_rels/document.xml.rels", _DOCX_DOC_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", _docx_styles())
        z.writestr("docProps/core.xml", core)


async def make_document(markdown: str, target: Path, title: str = "") -> Path:
    """Write `markdown` as `target`: .docx and .pdf made here; .odt by LibreOffice."""
    fmt = target.suffix.lower().lstrip(".")
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "docx":
        await asyncio.to_thread(write_docx, markdown, target, title or target.stem)
        return target
    if fmt == "pdf":
        from arcee.document_pdf import write_pdf

        await asyncio.to_thread(write_pdf, blocks(markdown), target, title or target.stem)
        return target
    if fmt != "odt":
        raise DocumentError("documents can be .docx, .odt or .pdf")
    office = shutil.which("soffice") or shutil.which("libreoffice")
    if office is None or not _has_writer(office):
        raise DocumentError("an .odt needs LibreOffice Writer; make a .docx instead")
    with tempfile.TemporaryDirectory(prefix="arcee-doc-") as tmp:
        source = Path(tmp, target.stem + ".html")
        page = markdown_html(markdown, title or target.stem)
        await asyncio.to_thread(source.write_text, page, encoding="utf-8")
        profile = Path(tmp, "profile").as_uri()
        await _run(
            office,
            f"-env:UserInstallation={profile}",
            "--headless",
            "--writer",
            "--convert-to",
            "odt",
            "--outdir",
            tmp,
            str(source),
        )
        made = Path(tmp, f"{target.stem}.odt")
        if not await asyncio.to_thread(made.exists):
            raise DocumentError("LibreOffice made nothing")
        await asyncio.to_thread(shutil.move, made, target)
    return target


def _has_writer(office: str, part: str = "sw") -> bool:
    """Whether LibreOffice has Writer ("sw") or Calc ("sc"): libreoffice-core alone
    opens no document at all."""
    program = Path(office).resolve().parent
    return any(program.glob(f"lib{part}*.so"))
