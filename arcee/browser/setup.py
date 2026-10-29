"""`arcee-cli browser-setup`: register the native messaging host with Chrome.

Writes a launcher that runs the host with this Python, and Chrome's manifest
naming it and the Arcee extension's fixed ID. Loading the extension itself is
done by the user in chrome://extensions.
"""

import json
import os
import stat
import sys
from pathlib import Path
from typing import Final

from arcee.browser.host import EXTENSION_ID, HOST_NAME

EXTENSION_DIR: Final = Path(__file__).resolve().parents[2] / "browser-ext"


def chrome_hosts_dir() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "google-chrome" / "NativeMessagingHosts"


def install(data_dir: Path, hosts_dir: Path | None = None) -> tuple[Path, Path]:
    """Write the launcher and the host manifest; returns both paths."""
    launcher = data_dir / "browser-host"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m arcee.browser.host "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    manifest = (hosts_dir or chrome_hosts_dir()) / f"{HOST_NAME}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "name": HOST_NAME,
                "description": "Arcee browser bridge",
                "path": str(launcher),
                "type": "stdio",
                "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
            },
            indent=2,
        )
        + "\n"
    )
    return launcher, manifest
