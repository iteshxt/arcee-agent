import io

import pytest

from arcee.cli import Terminal, _host, _usable, main
from arcee.core.agent import Escalated, Guarded, ModelTurn, ToolRan
from arcee.core.protocol import ExecutorResult
from arcee.models.base import ModelProfile, WireCall
from arcee.models.fake import FakeAdapter

PROFILE = ModelProfile(model="m", context_tokens=8192, cost_tier="local", call_mode="native")


class Health(FakeAdapter):
    def __init__(self, name: str, healthy: bool) -> None:
        super().__init__([], PROFILE.model_copy(update={"model": name}))
        self._healthy = healthy

    async def health(self) -> bool:
        return self._healthy


def terminal() -> tuple[Terminal, io.StringIO]:
    out = io.StringIO()
    return Terminal(verbose=True, out=out), out


async def test_missing_upper_tiers_are_skipped_with_a_note() -> None:
    term, out = terminal()
    ladder = [Health("a", True), Health("b", True), Health("c", False)]

    usable = await _usable(ladder, term)

    assert [m.profile.model for m in usable] == ["a", "b"]
    assert "c is not pulled" in out.getvalue()


async def test_missing_first_tier_means_nothing_runs() -> None:
    term, _ = terminal()

    assert await _usable([Health("a", False), Health("b", True)], term) == []


def test_prints_every_kind_of_event() -> None:
    term, out = terminal()
    result = ExecutorResult(n=1, tool="fs.list", status="ok", summary="Listed ~", duration_ms=4)

    term.on_event(
        ModelTurn(1, 0, "lfm3", "look first", WireCall("fs_list", {"path": "~"}), "", 1.2)
    )
    term.on_event(ToolRan(1, result))
    term.on_event(Guarded(2, "blocked a repeat of fs.list"))
    term.on_event(Escalated(3, 1, "lfm3", "invalid call"))

    assert out.getvalue().splitlines() == [
        "  thinking: look first",
        "→ fs_list path='~'  lfm3, 1.2s",
        "  ✓ Listed ~  4 ms",
        "  ⚑ guard: blocked a repeat of fs.list",
        "  ↑ escalating to tier 2, lfm3, thinking: invalid call",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("127.0.0.1:11434", "http://127.0.0.1:11434"), ("http://box:1", "http://box:1")],
)
def test_ollama_host_without_scheme(value: str, expected: str) -> None:
    assert _host(value) == expected


def test_run_needs_a_request() -> None:
    with pytest.raises(SystemExit):
        main(["run"])


async def test_answering_a_question_gives_control_back_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Presence:
        held = True

        async def acting(self) -> None:
            events.append("acting")

        async def release(self) -> None:
            events.append("release")

    term = Terminal(verbose=False, out=io.StringIO())
    term.presence = Presence()

    async def read(prompt: str) -> str:
        events.append("read")
        return "yes"

    monkeypatch.setattr(term, "_read", read)

    assert await term.ask("Proceed?") == "yes"
    assert events == ["release", "read", "acting"]
