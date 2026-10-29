import re
from datetime import datetime
from pathlib import Path

import pytest

from arcee.context.examples import Example, ExampleLibrary, render, tokens
from arcee.core.agent import Agent
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.policy import Policy
from arcee.eval.cases import load
from arcee.models.base import Message, ModelProfile
from arcee.models.fake import FakeAdapter
from arcee.tools.builtin import builtin_registry
from tests.unit.test_agent import User

LIBRARY = ExampleLibrary.load()
TOOLS = builtin_registry(shell_limits=False)
CASES = Path(__file__).parents[1] / "eval" / "cases.toml"


def test_packaged_examples_are_well_formed() -> None:
    ids = [e.id for e in LIBRARY.examples]
    names = {tool.name for tool in TOOLS}

    assert len(ids) >= 25
    assert len(ids) == len(set(ids))
    for example in LIBRARY.examples:
        assert example.tags, example.id
        assert example.why, example.id
        if call := re.match(r"([a-z_]+\.[a-z_]+)\(", example.action):
            assert call[1] in names, example.id
        else:
            assert example.action.startswith("Reply"), example.id


def test_examples_do_not_copy_eval_cases() -> None:
    """The eval stays held out: no example may share most of its words with a case."""
    for case in load(CASES):
        request = set(tokens(case.request))
        for example in LIBRARY.examples:
            overlap = len(request & set(tokens(example.situation))) / max(len(request), 1)
            assert overlap < 0.6, (case.id, example.id)


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("How much RAM is free?", ["ram-from-system-status"]),
        ("Is the computer online?", ["online-check"]),
        ("How many photos are in ~/Pictures/trip?", ["count-by-type"]),
        ("What is 2 to the power of 64? Work it out exactly.", ["calculation-in-script"]),
        ("Tell me a joke", []),
    ],
)
def test_search_matches_intent_not_objects(request_text: str, expected: list[str]) -> None:
    assert [e.id for e in LIBRARY.search(request_text)] == expected


def test_render() -> None:
    example = Example("x", "The user asks X.", "tool.do()", "because", "x")

    assert render([]) == ""
    assert render([example]) == (
        "Similar situations and the right move:\n- The user asks X. -> tool.do() (because)"
    )


def test_custom_library_from_text() -> None:
    entry = '[[example]]\nid="{0}"\nsituation="s"\naction="Reply: x"\nwhy="w"\ntags="{1}"\n'
    library = ExampleLibrary.load(
        entry.format("hi", "hello greeting")
        + entry.format("ram", "memory ram")
        + entry.format("disk", "disk space")
    )

    assert library.scored("hello there, greeting")[0][1].id == "hi"
    assert library.scored("unrelated words") == []


def test_agent_puts_matching_examples_in_the_system_prompt() -> None:
    user = User()
    profile = ModelProfile(model="m", context_tokens=8192, cost_tier="local", call_mode="native")
    agent = Agent(
        [FakeAdapter([], profile)],
        TOOLS,
        Executor(Gate(Policy(), user)),
        user,
        home=Path("/home/u"),
        examples=LIBRARY,
        clock=lambda: datetime(2026, 10, 29),
    )

    with_match = agent.core_request([Message("user", "How much RAM is free?")]).system
    without = agent.core_request([Message("user", "Tell me a joke")]).system

    assert "Similar situations and the right move:" in with_match
    assert "system.status()" in with_match
    assert "Similar situations" not in without
