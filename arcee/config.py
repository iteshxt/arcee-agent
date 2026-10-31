"""Settings from the environment: API keys live in a git-ignored .env, never in the repo."""

import os
from pathlib import Path

# The project folder (for a source checkout), and the user's config folder (for the
# daemon, which systemd starts elsewhere).
PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"


def user_env() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "arcee" / "env"


def load_env(*paths: Path) -> None:
    """Read KEY=VALUE lines from `paths` (default: ./.env, the project's .env and
    ~/.config/arcee/env), never overriding what the environment already has."""
    for path in paths or (Path(".env"), PROJECT_ENV, user_env()):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name and not name.startswith("#"):
                os.environ.setdefault(name.strip(), value.strip().strip("'\""))
