"""Single-decision eval cases (spec section 16, decision 0003).

A case is a request, optionally with earlier tool calls and their results
already in the conversation, and a check on the model's next decision. One
decision per case keeps every result machine-checkable and every failure
attributable to one choice.

Cases live in TOML (`tests/eval/cases.toml`). They are held out: nothing in
them may be copied into prompts, usage cards or the example library.
"""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# What the model's next turn was, reduced to what checks look at.
type Kind = Literal["tool", "reply", "ask", "invalid"]


@dataclass(frozen=True, slots=True)
class PriorCall:
    """An earlier step: the call the model made and the result it got."""

    tool: str
    args: Mapping[str, Any]
    status: Literal["ok", "error", "refused", "timeout"] = "ok"
    data: Mapping[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PriorUser:
    """An extra user message, e.g. an answer to an earlier question."""

    text: str


@dataclass(frozen=True, slots=True)
class Expect:
    """Pass conditions; every one that is set must hold.

    `any_of` names tools by dotted name, plus `reply` for a final answer (plain
    text or `control.done`) and `ask` for a question to the user
    (`control.ask_user`, or a plain-text reply that asks something).
    `args_contain` / `args_lack` match case-insensitively against the string form
    of each argument; `reply_has` / `reply_lacks` against the reply text.
    """

    any_of: tuple[str, ...]
    args_contain: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict[str, Any])
    args_lack: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict[str, Any])
    reply_has: tuple[str, ...] = ()
    reply_lacks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    scenario: str
    request: str
    expect: Expect
    history: tuple[PriorCall | PriorUser, ...] = ()


@dataclass(frozen=True, slots=True)
class Decision:
    """The model's next turn: a validated tool call, a reply, a question, or garbage."""

    kind: Kind
    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict[str, Any])
    text: str = ""
    error: str = ""


def check(expect: Expect, decision: Decision) -> list[str]:
    """Why `decision` fails `expect`; empty when it passes."""
    if decision.kind == "invalid":
        return [f"invalid call: {decision.error}"]
    label = decision.tool if decision.kind == "tool" else decision.kind
    # A plain-text reply that asks something counts as asking: in native mode a
    # model often asks in text instead of calling control.ask_user.
    if label == "reply" and "ask" in expect.any_of and "?" in decision.text:
        label = "ask"
    if label not in expect.any_of:
        return [f"expected {' or '.join(expect.any_of)}, got {_describe(decision)}"]
    problems: list[str] = []
    # Argument checks describe the tool call; a reply or question has none to check.
    arg_checks = decision.kind == "tool"
    for name, wanted in expect.args_contain.items() if arg_checks else ():
        value = str(decision.args.get(name, "")).lower()
        problems += [f"{name} lacks {w!r}" for w in _many(wanted) if w.lower() not in value]
    for name, unwanted in expect.args_lack.items() if arg_checks else ():
        value = str(decision.args.get(name, "")).lower()
        problems += [f"{name} has {u!r}" for u in _many(unwanted) if u.lower() in value]
    text = decision.text.lower()
    problems += [f"reply lacks {w!r}" for w in expect.reply_has if w.lower() not in text]
    problems += [f"reply has {u!r}" for u in expect.reply_lacks if u.lower() in text]
    return problems


def _many(value: str | tuple[str, ...]) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else value


def _describe(decision: Decision) -> str:
    if decision.kind == "tool":
        return f"{decision.tool}({dict(decision.args)})"
    return f"{decision.kind}: {decision.text[:80]!r}"


# --- Loading --------------------------------------------------------------------


def load(path: Path) -> list[Case]:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    cases = [_case(entry) for entry in raw.get("case", [])]
    ids = [case.id for case in cases]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"duplicate case ids: {', '.join(dupes)}")
    return cases


def _case(entry: Mapping[str, Any]) -> Case:
    try:
        expect = entry["expect"]
        return Case(
            id=entry["id"],
            scenario=entry["scenario"],
            request=entry["request"],
            history=tuple(_prior(p) for p in entry.get("history", [])),
            expect=Expect(
                any_of=tuple(_list(expect["any_of"])),
                args_contain={k: _tuple(v) for k, v in expect.get("args_contain", {}).items()},
                args_lack={k: _tuple(v) for k, v in expect.get("args_lack", {}).items()},
                reply_has=tuple(_list(expect.get("reply_has", []))),
                reply_lacks=tuple(_list(expect.get("reply_lacks", []))),
            ),
        )
    except KeyError as exc:
        raise ValueError(f"case {entry.get('id', '?')}: missing {exc}") from exc


def _prior(entry: Mapping[str, Any]) -> PriorCall | PriorUser:
    if "user" in entry:
        return PriorUser(entry["user"])
    return PriorCall(
        tool=entry["call"],
        args=entry.get("args", {}),
        status=entry.get("status", "ok"),
        data=entry.get("data"),
        error=entry.get("error"),
    )


def _list(value: str | Sequence[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _tuple(value: str | Sequence[str]) -> str | tuple[str, ...]:
    return value if isinstance(value, str) else tuple(value)
