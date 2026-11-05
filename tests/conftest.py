import pytest

from arcee import keys


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read or write the user's GNOME Keyring."""
    monkeypatch.setattr(keys, "keyring", lambda: None)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real_session: needs the user's systemd session (and touches nothing else)"
    )
