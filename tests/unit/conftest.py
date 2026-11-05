import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from arcee.browser.client import BrowserError
from arcee.tools import browser as browser_tools

# Programs that open apps, files or pages on the user's screen. In unit tests each is a
# stub that refuses and notes the attempt in $ARCEE_TEST_LAUNCHES. Pointing the browser
# tools away from Chrome's extension was not enough: finding it "not running", they
# started the user's Chrome themselves with `gio launch` (2026-11-06).
LAUNCHERS = (
    "xdg-open",
    "gtk-launch",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "firefox",
)
REAL_GIO = shutil.which("gio") or "/usr/bin/gio"


def _stubs(folder: Path, log: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    refuse = f'echo "$0 $*" >> "{log}"; echo "blocked in tests: $0" >&2; exit 1'
    for name in LAUNCHERS:
        (folder / name).write_text(f"#!/bin/sh\n{refuse}\n")
    # gio: `launch` and `open` show things; `trash`, `info` and the rest go to the real one.
    (folder / "gio").write_text(
        f'#!/bin/sh\ncase "$1" in launch|open) {refuse} ;; esac\nexec "{REAL_GIO}" "$@"\n'
    )
    for stub in folder.iterdir():
        stub.chmod(0o755)


# Before any test module is imported: tools built at import time (a test module's
# registry) must not see the user's runtime folder, session bus or programs either.
_ISOLATED = (
    "PATH",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
)
_REAL = {name: os.environ.get(name) for name in (*_ISOLATED, "ARCEE_DATA_DIR")}
_SESSION = Path(tempfile.mkdtemp(prefix="arcee-unit-"))
atexit.register(shutil.rmtree, _SESSION, True)
(_SESSION / "run").mkdir()
_stubs(_SESSION / "bin", Path(os.environ.get("ARCEE_TEST_LAUNCHES") or _SESSION / "launches.log"))
os.environ["PATH"] = f"{_SESSION / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
os.environ["XDG_RUNTIME_DIR"] = str(_SESSION / "run")
os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={_SESSION / 'no-bus'}"
os.environ["XDG_DATA_HOME"] = str(_SESSION / "data")
os.environ["XDG_CONFIG_HOME"] = str(_SESSION / "config")
os.environ.pop("ARCEE_DATA_DIR", None)


async def _no_chrome() -> None:
    raise BrowserError("Chrome is not started in unit tests")


# The browser tools start Chrome when its extension does not answer, then wait 30 s
# for it: in unit tests that fails at once. Patched before the tests build their tools.
browser_tools.launch_chrome = _no_chrome


@pytest.fixture(autouse=True)
def no_real_desktop(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Tests never reach the user's desktop: not their Chrome (the bridge's socket is in
    the runtime folder, and nothing can start it), not GNOME Shell or their media
    players (the session bus), not their history, settings or wake word. A test that
    drove the real browser tools had opened YouTube in the user's Chrome on every run
    (2026-11-06)."""
    if request.node.get_closest_marker("real_session"):
        for name, value in _REAL.items():  # the user's session, as it was
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return
    home = tmp_path_factory.mktemp("home")
    stubs = home / "bin"
    log = Path(os.environ.get("ARCEE_TEST_LAUNCHES") or home / "launches.log")
    _stubs(stubs, log)
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "run"))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={home / 'no-bus'}")
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.delenv("ARCEE_DATA_DIR", raising=False)
    (home / "run").mkdir()
