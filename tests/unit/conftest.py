import pytest


@pytest.fixture(autouse=True)
def no_real_desktop(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Tests never reach the user's desktop: not their Chrome (the bridge's socket is in
    the runtime folder), not GNOME Shell or their media players (the session bus), not
    their history, settings or wake word. A test that drove the real browser tools had
    opened YouTube in the user's Chrome on every run (2026-11-06)."""
    if request.node.get_closest_marker("real_session"):
        return
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "run"))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={home / 'no-bus'}")
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.delenv("ARCEE_DATA_DIR", raising=False)
    (home / "run").mkdir()
