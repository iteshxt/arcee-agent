"""The conversation: a new request is given the turns it continues."""

from arcee.context.conversation import IDLE_S, MAX_TURNS, Conversation, Turn
from arcee.core.toolsets import APPS, BROWSER, WEB, filters_for
from arcee.models.base import Completion, Message
from tests.unit.test_planner import call, make, plan


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_turns_render_oldest_first_with_what_was_done() -> None:
    talk = Conversation(Clock())
    talk.add(Turn("Open Chrome.", "Chrome is open.", "done", ("app.open: Opened Google Chrome",)))
    talk.add(Turn("What's the weather?", "22°C and hazy in Ludhiana.", "done"))

    text = talk.render()

    assert text.index("Open Chrome.") < text.index("What's the weather?")
    assert "You did: app.open: Opened Google Chrome" in text
    assert "You replied: Chrome is open." in text
    assert talk.requests() == ["Open Chrome.", "What's the weather?"]


def test_a_stopped_turn_says_so() -> None:
    talk = Conversation(Clock())
    talk.add(Turn("Open Gmail", "", "stopped"))
    assert "It ended: stopped" in talk.render()


def test_it_ends_when_closed_or_after_a_long_pause() -> None:
    clock = Clock()
    talk = Conversation(clock)
    talk.add(Turn("Open Chrome.", "Done.", "done"))
    assert talk.active

    clock.now += IDLE_S + 1
    assert not talk.active
    assert talk.render() == ""
    talk.add(Turn("Open YouTube", "Done.", "done"))  # a new conversation
    assert talk.requests() == ["Open YouTube"]

    talk.end()  # the user closed the pill
    assert talk.render() == ""


def test_only_the_last_turns_are_kept() -> None:
    talk = Conversation(Clock())
    for n in range(MAX_TURNS + 3):
        talk.add(Turn(f"request {n}", "ok", "done"))
    assert len(talk.turns) == MAX_TURNS
    assert talk.requests()[0] == "request 3"


def test_a_follow_up_gets_the_tools_of_the_turn_before() -> None:
    follow_up = [Message("user", "And tomorrow?")]
    assert not set(WEB) & set(filters_for(follow_up))
    assert set(WEB) <= set(filters_for(follow_up, earlier=["What's the weather like today?"]))
    opened = filters_for([Message("user", "Now open YouTube")], earlier=["Open Chrome."])
    assert set(APPS + BROWSER) <= set(opened)


async def test_the_core_and_the_planner_see_the_earlier_turns() -> None:
    talk = Conversation(Clock())
    talk.add(Turn("Open Chrome.", "Chrome is open.", "done", ("app.open: Opened Google Chrome",)))
    agent, planner, core, _, _ = make(
        [plan(("Open YouTube in the open Chrome", ["browser.navigate"]))],
        [call("browser_navigate", url="https://youtube.com"), Completion("YouTube is open.")],
    )

    await agent.run("Open YouTube", conversation=talk.render(), earlier=talk.requests())

    assert "1. The user asked: Open Chrome." in planner.requests[0].system
    assert "You did: app.open: Opened Google Chrome" in core.requests[0].system
    offered = {t["function"]["name"] for t in core.requests[0].tools}
    assert {"browser_navigate", "app_open"} <= offered
