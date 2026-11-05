"""The unit tests' guard: nothing they run can open anything on the user's screen."""

import os
import subprocess
from pathlib import Path

import pytest

from arcee.tools.browser import BrowserError, launch_chrome


def test_programs_that_open_things_are_refused() -> None:
    for argv in (["gio", "launch", "google-chrome.desktop"], ["xdg-open", "https://youtube.com"]):
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        assert done.returncode != 0
        assert "blocked in tests" in done.stderr


def test_gio_still_does_everything_else(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    done = subprocess.run(
        ["gio", "info", str(tmp_path / "a.txt")], capture_output=True, check=False
    )
    assert done.returncode == 0


async def test_the_browser_tools_cannot_start_chrome() -> None:
    with pytest.raises(BrowserError, match="not started in unit tests"):
        await launch_chrome()


def test_the_runtime_folder_is_not_the_users() -> None:
    assert not os.environ["XDG_RUNTIME_DIR"].startswith(f"/run/user/{os.getuid()}")
