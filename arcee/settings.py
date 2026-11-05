"""The user's choices, set in the Arcee app and read by arceed and arcee-cli.

Kept in ~/.config/arcee/settings.json. Only choices that make Arcee more careful
are offered for actions: asking before everything, or always asking before a
given tool. Nothing here can let a tool skip a confirmation it would otherwise ask.
Personal data going to a cloud model (decision 0017) is a privacy choice, not an
action's: the user may let it go without asking.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

from arcee.core.policy import Policy, Tier

Core = Literal["groq", "local"]
# Mail, calendar and contacts to a cloud model: ask each time, always, or never.
Sharing = Literal["ask", "always", "off"]
SHARINGS: Final[tuple[Sharing, ...]] = ("ask", "always", "off")
CORES: Final[tuple[Core, ...]] = ("groq", "local")


def settings_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "arcee" / "settings.json"


@dataclass
class Settings:
    # Which models think: Groq's gpt-oss (decision 0008) or the local Ollama ladder.
    core: Core = "groq"
    # Ask before every action, even ones that are normally automatic.
    confirm_all: bool = False
    # Tools that always ask first, e.g. "browser.click".
    always_ask: list[str] = field(default_factory=list[str])
    # The first-run setup was finished (or skipped) once.
    setup_done: bool = False
    # Listen for "hey Arcee" (decision 0015): the wake word here, the request to Groq.
    # Off until the user turns it on.
    listen: bool = False
    # Learn from finished tasks (their summaries and facts) and bring back what matters
    # (decision 0014). Off: nothing new is remembered and nothing is recalled.
    memory: bool = True
    # Arcee turned its GNOME Shell extension on once (after the login that loaded it).
    # Never again after that: an extension the user turns off stays off.
    extension_turned_on: bool = False
    # Personal data (mail, calendar, contacts) read for a task, to a cloud model: "ask"
    # each time (the user's choice, 2026-11-04), "always", or "off" (never).
    personal_to_cloud: Sharing = "ask"

    def policy(self) -> Policy:
        return Policy(
            tier_overrides=dict.fromkeys(self.always_ask, Tier.CONFIRM),
            confirm_all=self.confirm_all,
        )


def load(path: Path | None = None) -> Settings:
    """The saved settings; the defaults when there are none or the file is unreadable."""
    path = path or settings_path()
    try:
        raw: dict[str, Any] = json.loads(path.read_text())
    except OSError, ValueError:
        return Settings()
    core = raw.get("core")
    asks = raw.get("always_ask")
    sharing = raw.get("personal_to_cloud")
    return Settings(
        core=core if core in CORES else "groq",
        confirm_all=raw.get("confirm_all") is True,
        always_ask=[str(name) for name in cast(list[Any], asks)] if isinstance(asks, list) else [],
        setup_done=raw.get("setup_done") is True,
        extension_turned_on=raw.get("extension_turned_on") is True,
        memory=raw.get("memory") is not False,
        listen=raw.get("listen") is True,
        personal_to_cloud=sharing if sharing in SHARINGS else "ask",
    )


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), indent=1))
    temporary.replace(path)


def save_listen(on: bool, path: Path | None = None) -> None:
    """Only "Listen for Hey Arcee", the rest as saved: arceed and the panel switch it
    while the app may hold the other settings."""
    settings = load(path)
    if settings.listen != on:
        settings.listen = on
        save(settings, path)


if __name__ == "__main__":
    # `python -m arcee.settings listening`: exit 0 when "Listen for Hey Arcee" is on. The
    # login unit (arceed-listen.service) starts arceed only then.
    import sys

    sys.exit(0 if sys.argv[1:] == ["listening"] and load().listen else 1)
