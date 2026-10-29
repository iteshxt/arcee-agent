import pytest

from arcee.core.guards import NARRATION_NUDGE, narrated_tool, review_reply, ungrounded
from arcee.models.base import Message

LISTING = '{"status": "ok", "data": {"path": "~/Downloads", "entries": [{"name": "setup.deb"}]}}'
IP_OUTPUT = '{"data": {"stdout": "inet 192.168.1.37/24 brd 192.168.1.255"}}'


def test_narration_names_a_tool() -> None:
    assert narrated_tool("I'll call fs_search next.", ["fs_list", "fs_search"]) == "fs_search"
    assert narrated_tool("Your files are listed below.", ["fs_list"]) is None


@pytest.mark.parametrize(
    ("reply", "missing"),
    [
        ("Your IP is 192.168.1.37.", []),
        ("Your IP is 192.168.1.73.", ["192.168.1.73"]),
        ("The biggest file is ~/Downloads/setup.deb.", []),
        ("The biggest file is /home/user/Downloads/setup.deb.", []),
        ("The biggest file is ~/Downloads/other.iso.", ["~/Downloads/other.iso", "other.iso"]),
        ("It is in your Downloads folder.", []),
        ("There are 3 files, e.g. setup.deb.", []),
    ],
)
def test_ungrounded_facts(reply: str, missing: list[str]) -> None:
    assert ungrounded(reply, f"{LISTING}\n{IP_OUTPUT}", tools_ran=True) == missing


def test_file_names_are_not_checked_in_chat() -> None:
    assert ungrounded("Arcee reads README.md files too.", "hi", tools_ran=False) == []
    assert ungrounded("Your IP is 10.0.0.2.", "what is my ip?", tools_ran=False) == ["10.0.0.2"]


def test_review_prefers_narration_then_grounding() -> None:
    messages = [Message("user", "biggest file?"), Message("tool", LISTING, tool_name="fs_list")]

    narration = review_reply("Let me use fs_list again", messages, ["fs_list"])
    grounding = review_reply("It is ~/Downloads/x.iso", messages, ["fs_list"])

    assert narration is not None
    assert (narration.kind, narration.nudge) == ("narration", NARRATION_NUDGE)
    assert grounding is not None
    assert grounding.kind == "grounding"
    assert "'~/Downloads/x.iso'" in grounding.nudge
    assert review_reply("It is setup.deb", messages, ["fs_list"]) is None
    assert review_reply("It is ~/x.iso", messages, ["fs_list"], grounding=False) is None
