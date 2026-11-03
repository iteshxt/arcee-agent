"""Replies in Markdown, shown as Pango markup in the app, the panel and the pill."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from arcee.markdown import plain, to_pango

EXTENSION = Path(__file__).parents[2] / "extension" / "arcee@iteshxt.me" / "markdown.js"

# A real reply (2026-11-04), as it reached the pill unformatted.
REPLY = """The folders that take up the most space in your computer are:

- **bin** - ~8 GB
- **dev** - ~8 GB

These folders are the largest. (The remaining folders like `swapfile`, `docs` are smaller.)"""

CASES = [
    REPLY,
    "# Today\n\n## Weather\nSunny, **22°C** and *dry*.",
    "1. Open Chrome\n2) Go to [YouTube](https://youtube.com)\n   - then search",
    "| Day | Max |\n|---|---:|\n| Wed | 32 |\n| Thu | 31 |",
    "Run this:\n\n```bash\nls -la ~/Downloads | head\n```\n\nDone.",
    "> Quoted *note*\n\n---\n\nFiles: a_b_c.txt, 3 * 4 = 12, <script> & ~~old~~ new",
    "snake_case_name and __init__ and **bold with `code` inside**",
]


def test_the_real_reply_is_formatted() -> None:
    assert to_pango(REPLY) == (
        "The folders that take up the most space in your computer are:\n"
        "\n"
        "•  <b>bin</b> - ~8 GB\n"
        "•  <b>dev</b> - ~8 GB\n"
        "\n"
        "These folders are the largest. (The remaining folders like <tt>swapfile</tt>, "
        "<tt>docs</tt> are smaller.)"
    )


def test_headings_lists_links_tables_and_code() -> None:
    assert to_pango("## Weather\nSunny, **22°C** and *dry*.") == (
        "<b>Weather</b>\nSunny, <b>22°C</b> and <i>dry</i>."
    )
    assert to_pango("1. Open\n   - nested") == "1.  Open\n    •  nested"
    assert to_pango("[YouTube](https://youtube.com)") == '<a href="https://youtube.com">YouTube</a>'
    assert to_pango("[YouTube](https://youtube.com)", links=False) == "<u>YouTube</u>"
    assert to_pango("| Day | Max |\n|---|---:|\n| Wed | 32 |") == "Day  ·  Max\nWed  ·  32"
    assert to_pango("```\na < b\n```") == "<tt>a &lt; b</tt>"


def test_what_is_not_markdown_stays_as_written() -> None:
    assert to_pango("a_b_c.txt, 3 * 4 = 12") == "a_b_c.txt, 3 * 4 = 12"
    assert to_pango("<script> & co") == "&lt;script&gt; &amp; co"
    assert to_pango("`**not bold**`") == "<tt>**not bold**</tt>"


def test_plain_text_for_places_without_formatting() -> None:
    assert plain("**Done**: moved `a.txt` & [the rest](https://x.example)") == (
        "Done: moved a.txt & the rest"
    )


@pytest.mark.parametrize("text", CASES)
def test_the_markup_is_valid_pango(text: str) -> None:
    gi = pytest.importorskip("gi")
    gi.require_version("Pango", "1.0")
    from gi.repository import Pango  # pyright: ignore[reportMissingImports]

    # Links (<a>) are GTK's own; the Shell's labels get <u>, plain Pango.
    Pango.parse_markup(to_pango(text, links=False), -1, "\0")  # raises on bad markup


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_extension_formats_the_same() -> None:
    script = (
        f"import {{toPango}} from {json.dumps(EXTENSION.as_uri())};"
        f"const cases = {json.dumps(CASES)};"
        "process.stdout.write(JSON.stringify(cases.map(c => toPango(c))));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == [to_pango(c, links=False) for c in CASES]
