"""API keys the user enters in the app, kept in their GNOME Keyring (libsecret).

The keyring is encrypted with the login password and unlocked at login, so arceed
can read the keys without them sitting in a plain file. Precedence: a key already
in the environment wins, then the keyring, then .env files (see arcee.config), so
a key entered in the app replaces one left in an old .env.
"""

import os
from dataclasses import dataclass
from typing import Final, Literal, Protocol

Source = Literal["environment", "keyring", "file", "missing"]


@dataclass(frozen=True, slots=True)
class Key:
    env: str
    title: str
    purpose: str
    get_url: str


KEYS: Final = (
    Key(
        "GROQ_API_KEY",
        "Groq key for thinking",
        "Runs gpt-oss and qwen, the models that read your requests and decide.",
        "https://console.groq.com/keys",
    ),
    Key(
        "GROQ_API_KEY_TTS",
        "Groq key for speech",
        "Turns what you say to the mic into text. A second key keeps its allowance apart.",
        "https://console.groq.com/keys",
    ),
)
SCHEMA_NAME: Final = "me.iteshxt.Arcee.Key"


class Keyring(Protocol):
    """Where keys are kept (libsecret; a dict in tests)."""

    def lookup(self, env: str) -> str | None: ...
    def store(self, env: str, value: str) -> None: ...
    def clear(self, env: str) -> None: ...


def keyring() -> Keyring | None:
    """The GNOME Keyring, or None where there is none (a bare TTY, a test box)."""
    try:
        from arcee.platform.secret import SecretKeyring

        return SecretKeyring()
    except Exception:
        return None


def load_from_keyring(ring: Keyring | None = None) -> list[str]:
    """Put keyring keys into the environment where none is set; returns their names."""
    ring = ring if ring is not None else keyring()
    loaded: list[str] = []
    if ring is None:
        return loaded
    for key in KEYS:
        if os.environ.get(key.env):
            continue
        try:
            value = ring.lookup(key.env)
        except Exception:  # a locked or missing keyring: the files may still have it
            return loaded
        if value:
            os.environ[key.env] = value
            loaded.append(key.env)
    return loaded


def source(env: str, from_keyring: set[str]) -> Source:
    """Where the key Arcee uses comes from, for the app to show."""
    if env in from_keyring:
        return "keyring"
    if os.environ.get(env):
        return "environment" if env in _START_ENV else "file"
    return "missing"


# What the environment held before any file or keyring was read.
_START_ENV: Final = frozenset(name for name, value in os.environ.items() if value)
