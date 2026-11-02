import pytest

from arcee.app import diagnostics
from arcee.app.diagnostics import Check, extension_check, keys, programs, run_all


async def test_keys_say_which_is_missing_and_where_to_put_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GROQ_API_KEY_TTS", raising=False)

    chat, speech = await keys()

    assert chat.ok
    assert not speech.ok
    assert (speech.fix, speech.fix_action) == ("add it on the Models page", "keys")


async def test_missing_programs_come_with_the_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: None)

    found = await programs()

    assert [c.name for c in found] == ["tesseract", "pw-record", "gio"]
    assert found[0] == Check(
        "tesseract", False, "missing (it reads the screen)", "sudo apt install tesseract-ocr"
    )


async def test_a_crashing_check_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> Check:
        raise RuntimeError("boom")

    async def fine() -> list[Check]:
        return [Check("a", True, "ok")]

    monkeypatch.setattr(diagnostics, "CHECKS", (broken, fine))

    assert await run_all() == [
        Check("broken", False, "check failed: boom"),
        Check("a", True, "ok"),
    ]


@pytest.mark.parametrize(
    ("info", "installed", "detail", "fix_action"),
    [
        (None, False, "not installed", "shell_extension"),
        # Just installed from the .deb: GNOME has not loaded it yet.
        (None, True, "installed, but GNOME loads new extensions at login", "shell_extension"),
        ({}, True, "installed, but GNOME loads new extensions at login", "shell_extension"),
        ({"state": 2.0}, True, "installed but turned off", "shell_extension"),
        ({"state": 3.0, "error": "SyntaxError"}, True, "failed to start: SyntaxError", ""),
        ({"state": 4.0}, True, "not made for this GNOME version", ""),
    ],
)
def test_the_extension_check_says_what_is_in_the_way(
    info: dict[str, object] | None, installed: bool, detail: str, fix_action: str
) -> None:
    check = extension_check(info, installed)

    assert not check.ok
    assert (check.detail, check.fix_action) == (detail, fix_action)


def test_off_means_loaded_by_gnome_but_not_running() -> None:
    from arcee.app.diagnostics import is_off, is_on

    assert is_off({"state": 2.0})
    assert is_off({"state": 6.0})
    assert not is_off(None)
    assert not is_off({})
    assert is_on({"state": 1.0})
    assert not is_on({"state": 2.0})
