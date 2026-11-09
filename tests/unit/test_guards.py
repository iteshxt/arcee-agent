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


@pytest.mark.parametrize(
    "reply",
    [
        'User asks: "who is jason born tell me". Likely they mean Jason Bourne. '
        "We can answer from knowledge. Provide brief answer.",
        "The user wants to know the weather. I should provide a short answer.",
    ],
)
def test_the_models_reasoning_is_not_taken_for_the_answer(reply: str) -> None:
    review = review_reply(reply, [Message("user", "who is jason born")], [])

    assert review is not None
    assert review.detail == "reply was the model's own reasoning"


def test_an_answer_that_mentions_users_is_still_an_answer() -> None:
    reply = "Jason Bourne is a fictional CIA assassin; users of the films know him from 2002 on."

    assert review_reply(reply, [Message("user", "who is jason bourne")], []) is None


def test_a_reply_that_gives_up_without_looking_is_sent_back_to_the_tools() -> None:
    asked = [Message("user", "What's the weather like today?")]
    names = ["web_search", "web_weather"]
    for reply in (
        "I don't have access to real-time weather information.",
        "I need to know your location. Could you please tell me your city?",
        "I need to know which song you're referring to.",
        "Which Chrome window do you want to maximize? Please tell me its ID.",
    ):
        review = review_reply(reply, asked, names)
        assert review is not None
        assert "web_weather" in review.nudge
        assert "media_now_playing" in review.nudge
    # Once a tool has run, saying it could not find something is an honest answer.
    looked = [*asked, Message("tool", '{"status": "error"}')]
    assert review_reply("I can't check the weather right now.", looked, names) is None


TOOLS = ["shell_script", "shell_run", "voice_say", "fs_list"]


@pytest.mark.parametrize(
    ("request_text", "skipped"),
    [
        ("What is 2 to the power of 40, exactly?", "shell_script"),
        ("Calculate 17 * 23", "shell_script"),
        ("Read this out loud: good morning everyone", "voice_say"),
    ],
)
def test_answers_that_skip_their_tool_are_sent_back(request_text: str, skipped: str) -> None:
    review = review_reply("The answer.", [Message("user", request_text)], TOOLS)

    assert review is not None
    assert (review.kind, review.detail) == ("skipped", f"answered without {skipped}")


def test_once_the_tool_ran_the_answer_stands() -> None:
    messages = [
        Message("user", "What is 2 to the power of 40, exactly?"),
        Message("tool", '{"stdout": "1099511627776"}', tool_name="shell_script"),
    ]

    assert review_reply("It is 1,099,511,627,776.", messages, TOOLS) is None


def test_a_step_handoff_is_not_held_to_it() -> None:
    messages = [Message("user", "Find my last email from Rahul and read it out loud")]

    assert review_reply("Found it: subject Budget.", messages, TOOLS, narration=False) is None


def test_a_sum_the_script_never_printed_is_sent_back() -> None:
    messages = [
        Message("user", "What is 2 to the power of 40, exactly?"),
        Message("tool", '{"stdout": "1099511627776\\n"}', tool_name="shell_script"),
    ]

    review = review_reply("2^40 = 1,099,504,096", messages, TOOLS)

    assert review is not None
    assert review.detail == "reply stated 1,099,504,096, which no result printed"
