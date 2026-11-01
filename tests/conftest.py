import pytest

from arcee import keys


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read or write the user's GNOME Keyring."""
    monkeypatch.setattr(keys, "keyring", lambda: None)
