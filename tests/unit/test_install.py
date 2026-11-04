"""Installed from the .deb, the installers write nothing into the home folder."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from arcee import install


@pytest.fixture
def packaged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    monkeypatch.setattr(install, "PACKAGED", True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ran: list[list[str]] = []

    def run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(install.subprocess, "run", run)
    return ran


def written(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def test_the_package_holds_the_shell_extension(packaged: list[list[str]], tmp_path: Path) -> None:
    report = install.shell_extension()

    assert "installed for every user by the package" in report.lines[0]
    assert any("log out" in line for line in report.next)
    assert packaged == []
    assert written(tmp_path) == []


def test_the_package_holds_the_service(packaged: list[list[str]], tmp_path: Path) -> None:
    report = install.daemon()

    assert "installed by the package" in report.lines[0]
    assert packaged == [["systemctl", "--user", "daemon-reload"]]
    assert written(tmp_path) == []


def test_the_package_registers_the_chrome_host(packaged: list[list[str]], tmp_path: Path) -> None:
    report = install.browser_bridge()

    assert "registered for every user by the package" in report.lines[0]
    assert any("Load unpacked" in line for line in report.next)
    assert written(tmp_path) == []


def test_a_checkout_puts_the_icon_in_the_users_icon_theme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    report = install.Report()

    install.install_icon(report)

    theme = tmp_path / "icons" / "hicolor"
    assert (theme / "256x256" / "apps" / "me.iteshxt.Arcee.png").is_file()
    assert (theme / "16x16" / "apps" / "me.iteshxt.Arcee.png").is_file()
    assert (theme / "scalable" / "apps" / "me.iteshxt.Arcee.svg").is_file()


def test_a_checkout_starts_arceed_at_login_when_listening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ran: list[list[str]] = []

    def run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(install.subprocess, "run", run)

    install.daemon()

    unit = (tmp_path / "config" / "systemd" / "user" / "arceed-listen.service").read_text()
    assert "WantedBy=graphical-session.target" in unit
    assert "-m arcee.settings listening" in unit  # only when "Hey Arcee" is on
    assert "ExecStart=/usr/bin/systemctl --user start arceed.service" in unit
    assert ["systemctl", "--user", "enable", "arceed-listen.service"] in ran
