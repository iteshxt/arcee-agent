import json
from pathlib import Path

import pytest

from arcee.eval.cases import Case, Decision, Expect, PriorCall, PriorUser, check, load
from arcee.eval.runner import conversation, decide, run_cases, summary
from arcee.models.base import Completion, ModelProfile, WireCall
from arcee.models.fake import FakeAdapter
from arcee.tools.builtin import builtin_registry

REPO_CASES = Path(__file__).parents[1] / "eval" / "cases.toml"
PROFILE = ModelProfile(model="m", context_tokens=8192, cost_tier="local", call_mode="native")
TOOLS = builtin_registry(shell_limits=False)


class TestCheck:
    def test_tool_and_argument_substrings(self) -> None:
        expect = Expect(("shell.run",), args_contain={"cmd": ("du", "videos")})

        assert check(expect, Decision("tool", "shell.run", {"cmd": "du -sh ~/Videos"})) == []
        assert check(expect, Decision("tool", "shell.run", {"cmd": "du -sh ~"})) == [
            "cmd lacks 'videos'"
        ]

    def test_wrong_kind(self) -> None:
        problems = check(Expect(("reply",)), Decision("tool", "fs.list", {"path": "~"}))

        assert problems == ["expected reply, got fs.list({'path': '~'})"]

    def test_argument_absence(self) -> None:
        expect = Expect(("fs.trash",), args_lack={"paths": "notes"})

        assert check(expect, Decision("tool", "fs.trash", {"paths": ["~/notes.txt"]})) == [
            "paths has 'notes'"
        ]

    def test_reply_text(self) -> None:
        expect = Expect(("reply",), reply_has=("44",), reply_lacks=("curl",))

        assert check(expect, Decision("reply", text="44G free")) == []
        assert check(expect, Decision("reply", text="run curl")) == [
            "reply lacks '44'",
            "reply has 'curl'",
        ]

    def test_text_question_counts_as_asking(self) -> None:
        assert check(Expect(("ask",)), Decision("reply", text="Which report do you mean?")) == []
        assert check(Expect(("ask",)), Decision("reply", text="Done.")) != []

    def test_argument_checks_skip_replies(self) -> None:
        expect = Expect(("fs.trash", "reply"), args_contain={"paths": "x"})

        assert check(expect, Decision("reply", text="ok")) == []

    def test_invalid_always_fails(self) -> None:
        assert check(Expect(("reply",)), Decision("invalid", error="empty reply")) == [
            "invalid call: empty reply"
        ]


class TestLoad:
    def test_repo_cases_load_and_name_real_tools(self) -> None:
        cases = load(REPO_CASES)
        names = {tool.name for tool in TOOLS} | {"reply", "ask"}

        assert len(cases) >= 50
        for case in cases:
            assert set(case.expect.any_of) <= names, case.id
            for prior in case.history:
                if isinstance(prior, PriorCall):
                    assert prior.tool in names, case.id

    def test_rejects_duplicates_and_missing_fields(self, tmp_path: Path) -> None:
        dupes = tmp_path / "d.toml"
        dupes.write_text(
            '[[case]]\nid="a"\nscenario="s"\nrequest="r"\nexpect={any_of="reply"}\n' * 2
        )
        missing = tmp_path / "m.toml"
        missing.write_text('[[case]]\nid="a"\nscenario="s"\nexpect={any_of="reply"}\n')

        with pytest.raises(ValueError, match="duplicate case ids: a"):
            load(dupes)
        with pytest.raises(ValueError, match="case a: missing 'request'"):
            load(missing)


CASE = Case(
    id="c",
    scenario="use-result",
    request="How many PDFs?",
    expect=Expect(("reply",), reply_has=("2",)),
    history=(
        PriorCall("fs.list", {"path": "~"}, data={"count": 2}),
        PriorUser("only in ~"),
    ),
)


def test_conversation_replays_history_as_tool_turns() -> None:
    messages = conversation(CASE)

    assert [m.role for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[1].tool_calls == (WireCall("fs_list", {"path": "~"}),)
    assert json.loads(messages[2].text) == {"status": "ok", "data": {"count": 2}}
    assert messages[2].tool_name == "fs_list"


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        (Completion("There are 2."), Decision("reply", text="There are 2.")),
        (Completion("  "), Decision("invalid", error="empty reply")),
        (
            Completion("", tool_calls=(WireCall("control_done", {"reply": "2"}),)),
            Decision("reply", "control.done", {"reply": "2"}, text="2"),
        ),
        (
            Completion("", tool_calls=(WireCall("control_ask_user", {"question": "Which?"}),)),
            Decision("ask", "control.ask_user", {"question": "Which?", "options": []}, "Which?"),
        ),
    ],
)
def test_decide(completion: Completion, expected: Decision) -> None:
    assert decide(completion, TOOLS) == expected


def test_decide_reports_invalid_calls() -> None:
    decision = decide(Completion("", tool_calls=(WireCall("fs_nope", {}),)), TOOLS)

    assert (decision.kind, decision.tool) == ("invalid", "fs_nope")
    assert decision.error.startswith("unknown tool 'fs_nope'")


async def test_run_cases_scores_and_repeats() -> None:
    model = FakeAdapter([Completion("There are 2."), Completion("No idea.")], PROFILE)

    results = await run_cases(model, TOOLS, [CASE], repeat=2)

    assert [r.passed for r in results] == [True, False]
    assert "/home/user" in model.requests[0].system
    assert "Thursday 2026-10-29 10:00" in model.requests[0].system
    report = summary(results)
    assert "use-result" in report
    assert "overall" in report
    assert "valid first reply: 2/2 (100%)" in report
