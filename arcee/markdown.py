"""Arcee's replies, written in Markdown by the models, as Pango markup to show.

GTK labels (the app) and the Shell's labels (the panel and the pill) both draw Pango
markup, so one small converter covers the Markdown models write: headings, bold,
italic, inline code and code blocks, bullet and numbered lists, quotes, links and
simple tables. Everything else is shown as the text it is, escaped. The extension has
the same converter in JavaScript (extension/arcee@iteshxt.me/markdown.js); a test
checks that both give the same markup.
"""

import html
import re
from typing import Final

_FENCE: Final = re.compile(r"^\s*(```|~~~)")
_RULE: Final = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_HEADING: Final = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE: Final = re.compile(r"^\s*\|(.*)\|\s*$")
_TABLE_RULE: Final = re.compile(r"^[\s|:-]+$")
_BULLET: Final = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED: Final = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE: Final = re.compile(r"^\s*>\s?(.*)$")
_CODE_SPAN: Final = re.compile(r"`([^`]+)`")
_LINK: Final = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD: Final = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_STRIKE: Final = re.compile(r"~~(.+?)~~")
_ITALIC: Final = re.compile(
    r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])|(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])"
)
_TAG: Final = re.compile(r"<[^>]+>")


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _words(text: str, links: bool) -> str:
    """Bold, italic, struck-through text and links, in text without code spans."""
    text = _escape(text)
    text = _LINK.sub(r'<a href="\2">\1</a>' if links else r"<u>\1</u>", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    return _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)


def _inline(text: str, links: bool) -> str:
    """One line: code spans as they are, the rest formatted."""
    out: list[str] = []
    last = 0
    for match in _CODE_SPAN.finditer(text):
        out.append(_words(text[last : match.start()], links))
        out.append(f"<tt>{_escape(match.group(1))}</tt>")
        last = match.end()
    out.append(_words(text[last:], links))
    return "".join(out)


def to_pango(text: str, *, links: bool = True) -> str:
    """Markdown as Pango markup. `links` off for labels that cannot open links (the
    Shell's), where they are underlined instead."""
    out: list[str] = []
    code = False
    for raw in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(raw):
            code = not code
            continue
        if code:
            out.append(f"<tt>{_escape(raw.rstrip())}</tt>")
            continue
        line = raw.rstrip()
        if not line.strip():
            if out and out[-1]:
                out.append("")
            continue
        if _RULE.match(line):
            if out and out[-1]:
                out.append("")
            continue
        if m := _HEADING.match(line):
            out.append(f"<b>{_inline(m.group(2), links)}</b>")
        elif m := _TABLE.match(line):
            if _TABLE_RULE.match(m.group(1)):
                continue  # |---|---|
            cells = [_inline(c.strip(), links) for c in m.group(1).split("|")]
            out.append("  ·  ".join(c for c in cells if c))
        elif m := _BULLET.match(line):
            indent = "    " * (len(m.group(1)) // 2)
            out.append(f"{indent}•  {_inline(m.group(2), links)}")
        elif m := _NUMBERED.match(line):
            indent = "    " * (len(m.group(1)) // 2)
            out.append(f"{indent}{m.group(2)}.  {_inline(m.group(3), links)}")
        elif m := _QUOTE.match(line):
            out.append(f"<i>{_inline(m.group(1), links)}</i>")
        else:
            out.append(_inline(line.strip(), links))
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def plain(text: str) -> str:
    """Markdown as plain text, for places that show no formatting."""
    return html.unescape(_TAG.sub("", to_pango(text, links=False)))
