"""Checks on a would-be final answer (spec section 6, "Code guards").

A plain-text reply ends a step, so it is checked before it is accepted:

- narration: an answer never names a tool; one that does is the model thinking
  aloud, and in native mode that would end the task;
- grounding: concrete facts in the answer (IP addresses, paths, file names) must
  appear somewhere in what the task has seen, the request or a tool result.
  A small model can state a wrong address with the right one in front of it.

Both the live loop and the eval runner use these, so the eval measures the
harness and not only the raw model.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from arcee.models.base import Message

NARRATION_NUDGE: Final = (
    "That was a note to yourself, not an answer. Either call one tool now, "
    "or reply to the user with the answer only."
)

_IPV4: Final = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PATH: Final = re.compile(r"(?:~|/home/\w+)(?:/[\w.\-]+)+")
# name.ext with a letter-led extension, minus the abbreviations that look like one.
_FILE: Final = re.compile(r"\b[\w\-]+\.[A-Za-z][A-Za-z0-9]{0,4}\b")
_NOT_FILES: Final = frozenset({"e.g", "i.e"})


def narrated_tool(reply: str, wire_names: Iterable[str]) -> str | None:
    """The tool a would-be answer names, if any."""
    for name in wire_names:
        if re.search(rf"\b{re.escape(name)}\b", reply):
            return name
    return None


def ungrounded(reply: str, evidence: str, *, tools_ran: bool) -> list[str]:
    """Facts in `reply` that `evidence` (the request and tool results) never shows.

    A path counts as shown when its last part is: listings report the folder and
    the names separately. Paths and file names are checked only once a tool has
    run, so chat about "README.md" is not flagged; IP addresses always are,
    because stating one without looking is a guess.
    """
    seen = _normalise(evidence)
    facts = _IPV4.findall(reply)
    if tools_ran:
        facts += _PATH.findall(reply)
        facts += [name for name in _FILE.findall(reply) if name.lower() not in _NOT_FILES]
    missing: list[str] = []
    for fact in dict.fromkeys(fact.rstrip(".,;:!?)") for fact in facts):
        key = fact.rstrip("/").rsplit("/", 1)[-1] if "/" in fact else fact
        if _normalise(key) not in seen:
            missing.append(fact)
    return missing


def grounding_nudge(facts: Iterable[str]) -> str:
    shown = ", ".join(repr(fact) for fact in facts)
    return (
        f"Your answer mentions {shown}, which no tool result shows. Answer only with facts "
        "from the tool results, or call a tool to check."
    )


def _normalise(text: str) -> str:
    # Home is written both ways; tool results are JSON with escaped slashes at times.
    text = re.sub(r"/home/\w+", "~", text.replace("\\/", "/"))
    return text.lower()


@dataclass(frozen=True, slots=True)
class Review:
    """Why a would-be answer was not accepted, and what to tell the model."""

    kind: Literal["narration", "grounding"]
    detail: str
    nudge: str


def review_reply(
    reply: str,
    messages: Sequence[Message],
    wire_names: Iterable[str],
    *,
    grounding: bool = True,
    narration: bool = True,
) -> Review | None:
    """Check a would-be final answer against the conversation so far; None when it passes.

    `narration` is off for a plan step's handoff note, which may name the tools
    the next steps will use.
    """
    if narration and (named := narrated_tool(reply, wire_names)) is not None:
        return Review("narration", f"reply was narration naming {named}", NARRATION_NUDGE)
    if not grounding:
        return None
    evidence = "\n".join(m.text for m in messages if m.role != "assistant")
    tools_ran = any(m.role == "tool" for m in messages)
    if missing := ungrounded(reply, evidence, tools_ran=tools_ran):
        return Review(
            "grounding", f"reply stated {', '.join(missing)} unseen", grounding_nudge(missing)
        )
    return None
