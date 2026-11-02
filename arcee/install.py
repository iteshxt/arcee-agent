"""Putting Arcee's parts in place for this user: no sudo, nothing outside the home folder.

Installed from the .deb (packaging/build-deb.sh), the package already holds the
extension, the service, D-Bus activation, the launcher and the Chrome host for every
user, so each step only says so and does what is left for this user (turn the
extension on, restart the service).

Used by `arcee-cli` (shell-extension, daemon-install, browser-setup) and by the
app's first-run setup and Diagnostics buttons. Each step returns what it did, in
lines for the user, and removes what an older Arcee left under its old names
(arcee@arcee.dev and org.arcee.*, before the names moved under iteshxt.me).
"""

import contextlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from arcee.browser.host import EXTENSION_ID
from arcee.browser.setup import EXTENSION_DIR, chrome_hosts_dir
from arcee.browser.setup import install as install_host
from arcee.context.store import data_dir

PROJECT: Final = Path(__file__).resolve().parents[1]
# Where the .deb installs the arcee package; a checkout runs from anywhere else.
PACKAGE_ROOT: Final = Path("/usr/lib/arcee")
PACKAGED: Final = PROJECT == PACKAGE_ROOT
SHELL_EXTENSION: Final = "arcee@iteshxt.me"
APP_ID: Final = "me.iteshxt.Arcee"
AGENT_BUS_NAME: Final = "me.iteshxt.Arcee.Agent"
# Names an older Arcee installed under.
OLD_SHELL_EXTENSION: Final = "arcee@arcee.dev"
OLD_FILES: Final = (
    ("dbus-1/services", "org.arcee.Agent.service"),
    ("applications", "org.arcee.App.desktop"),
)
OLD_HOST: Final = "org.arcee.browser.json"


@dataclass
class Report:
    """What a step did, in lines for the user; `next` is what they still have to do."""

    lines: list[str] = field(default_factory=list[str])
    next: list[str] = field(default_factory=list[str])


def _data() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def shell_extension() -> Report:
    """Link the GNOME Shell extension into the user's extensions and compile its settings."""
    report = Report()
    if PACKAGED:
        report.lines.append(f"installed for every user by the package: {SHELL_EXTENSION}")
        report.next += [
            "GNOME loads new extensions at login: log out and back in,",
            f"then turn it on: gnome-extensions enable {SHELL_EXTENSION}",
        ]
        return report
    source = PROJECT / "extension" / SHELL_EXTENSION
    extensions = _data() / "gnome-shell" / "extensions"
    target = extensions / SHELL_EXTENSION
    extensions.mkdir(parents=True, exist_ok=True)
    # The stop shortcut lives in the extension's settings schema, compiled in place.
    subprocess.run(["glib-compile-schemas", "--strict", str(source / "schemas")], check=True)
    if target.is_symlink() or target.exists():
        report.lines.append(f"already installed: {target}")
    else:
        target.symlink_to(source, target_is_directory=True)
        report.lines.append(f"linked {target} -> {source}")
    old = extensions / OLD_SHELL_EXTENSION
    if old.is_symlink():
        subprocess.run(
            ["gnome-extensions", "disable", OLD_SHELL_EXTENSION], check=False, capture_output=True
        )
        old.unlink()
        report.lines.append(f"removed the old extension {OLD_SHELL_EXTENSION}")
    report.next += [
        "GNOME loads new extensions at login: log out and back in,",
        f"then turn it on: gnome-extensions enable {SHELL_EXTENSION}",
    ]
    return report


def enable_shell_extension() -> bool:
    """Turn the extension on (once GNOME has loaded it, after a login)."""
    done = subprocess.run(
        ["gnome-extensions", "enable", SHELL_EXTENSION], check=False, capture_output=True
    )
    return done.returncode == 0


def daemon() -> Report:
    """The systemd user unit, D-Bus activation and app launcher for arceed and the app."""
    report = Report()
    if PACKAGED:
        report.lines.append("the service and the app launcher are installed by the package")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
        report.next.append(
            "arceed starts on the panel's first request (log: journalctl --user -u arceed)"
        )
        return report
    bin_dir = Path(sys.executable).parent
    arceed = bin_dir / "arceed"
    if not arceed.exists():
        report.lines.append(f"{arceed} is missing; run `uv sync` first")
        return report
    unit = _config() / "systemd" / "user" / "arceed.service"
    activation = _data() / "dbus-1" / "services" / f"{AGENT_BUS_NAME}.service"
    launcher = _data() / "applications" / f"{APP_ID}.desktop"
    install_icon(report)
    files = {
        unit: (
            "[Unit]\n"
            "Description=Arcee agent\n"
            "PartOf=graphical-session.target\n\n"
            "[Service]\n"
            "Type=dbus\n"
            f"BusName={AGENT_BUS_NAME}\n"
            f"ExecStart={arceed}\n"
            f"WorkingDirectory={PROJECT}\n"
            "TimeoutStopSec=2\n"
            "Restart=on-failure\n"
        ),
        activation: (
            "[D-BUS Service]\n"
            f"Name={AGENT_BUS_NAME}\n"
            f"Exec={arceed}\n"
            "SystemdService=arceed.service\n"
        ),
        launcher: (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Arcee\n"
            "Comment=An agent for your desktop, by Itesh Tomar\n"
            f"Exec={bin_dir / 'arcee-app'}\n"
            f"Icon={APP_ID}\n"
            "Categories=Utility;\n"
            f"StartupWMClass={APP_ID}\n"
        ),
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        report.lines.append(f"wrote {path}")
    for folder, name in OLD_FILES:
        old = _data() / folder / name
        if old.exists():
            old.unlink()
            report.lines.append(f"removed the old {old}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
    # A running arceed under the old name would keep it; stop it so the new one starts.
    subprocess.run(["systemctl", "--user", "restart", "arceed"], check=False, capture_output=True)
    report.next.append(
        "arceed starts on the panel's first request (log: journalctl --user -u arceed)"
    )
    return report


def install_icon(report: Report) -> None:
    """The app's icon in the user's icon theme under the app's id, as the .deb puts it in
    the system's: the app grid, the dock and the window all find it by that name."""
    art = PROJECT / "art"
    sizes = {px: art / f"arcee-{px}.png" for px in (64, 128, 256, 512)}
    small = PROJECT / "browser-ext" / "icons"
    sizes |= {px: small / f"icon-{px}.png" for px in (16, 32, 48)}
    theme = _data() / "icons" / "hicolor"
    for px, source in sizes.items():
        if source.is_file():
            target = theme / f"{px}x{px}" / "apps" / f"{APP_ID}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    if (art / "arcee.svg").is_file():
        scalable = theme / "scalable" / "apps" / f"{APP_ID}.svg"
        scalable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(art / "arcee.svg", scalable)
    # GNOME notices new icons when the theme's folder changes; touch it to be sure.
    theme.touch()
    report.lines.append(f"installed the icon in {theme}")


def browser_bridge() -> Report:
    """Register the native messaging host with Chrome; the extension is loaded by hand."""
    report = Report()
    if PACKAGED:
        report.lines.append("the Chrome host is registered for every user by the package")
    else:
        launcher, manifest = install_host(data_dir())
        report.lines += [f"registered the Chrome host: {manifest}", f"launcher: {launcher}"]
    old = chrome_hosts_dir() / OLD_HOST
    with contextlib.suppress(FileNotFoundError):
        old.unlink()
        report.lines.append(f"removed the old {old}")
    report.next += [
        "Load the Chrome extension once (Chrome allows no other way for unpacked ones):",
        "  1. Open chrome://extensions and turn on Developer mode (top right).",
        f"  2. Click 'Load unpacked' and choose {EXTENSION_DIR}",
        f"  3. Check that its ID is {EXTENSION_ID}; if it was loaded before, "
        "click its reload icon.",
    ]
    return report
