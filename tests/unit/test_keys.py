import os

import pytest

from arcee.keys import Keyring, load_from_keyring, source


class Ring(Keyring):
    def __init__(self, **stored: str) -> None:
        self.stored = dict(stored)

    def lookup(self, env: str) -> str | None:
        return self.stored.get(env)

    def store(self, env: str, value: str) -> None:
        self.stored[env] = value

    def clear(self, env: str) -> None:
        self.stored.pop(env, None)


def test_keyring_keys_fill_only_what_the_environment_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "from-shell")
    monkeypatch.delenv("GROQ_API_KEY_TTS", raising=False)

    loaded = load_from_keyring(Ring(GROQ_API_KEY="from-app", GROQ_API_KEY_TTS="speech"))

    assert loaded == ["GROQ_API_KEY_TTS"]
    assert os.environ["GROQ_API_KEY"] == "from-shell"
    assert os.environ["GROQ_API_KEY_TTS"] == "speech"
    assert source("GROQ_API_KEY_TTS", set(loaded)) == "keyring"


def test_no_keyring_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY_TTS", raising=False)

    assert load_from_keyring(None) == []
    assert source("GROQ_API_KEY_TTS", set()) == "missing"
