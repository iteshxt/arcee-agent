from pathlib import Path

from arcee.core.policy import Tier
from arcee.settings import Settings, load, save


def test_settings_round_trip_and_become_a_policy(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    save(Settings(core="local", confirm_all=True, always_ask=["browser.click"]), path)

    settings = load(path)
    policy = settings.policy()

    assert settings == Settings(core="local", confirm_all=True, always_ask=["browser.click"])
    assert policy.confirm_all
    assert policy.tier_overrides == {"browser.click": Tier.CONFIRM}


def test_missing_or_broken_settings_are_the_defaults(tmp_path: Path) -> None:
    broken = tmp_path / "settings.json"
    broken.write_text('{"core": "skynet", "confirm_all": "yes", "always_ask": 3')

    assert load(tmp_path / "none.json") == Settings()
    assert load(broken) == Settings()


def test_unknown_values_fall_back_one_by_one(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"core": "skynet", "confirm_all": "yes", "always_ask": ["fs.trash"]}')

    assert load(path) == Settings(core="groq", confirm_all=False, always_ask=["fs.trash"])


def test_arcee_remembers_it_turned_the_extension_on(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    save(Settings(extension_turned_on=True), path)

    assert load(path).extension_turned_on is True
    assert Settings().extension_turned_on is False
