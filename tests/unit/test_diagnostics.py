import pytest

from arcee.app import diagnostics
from arcee.app.diagnostics import Check, keys, programs, run_all


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
