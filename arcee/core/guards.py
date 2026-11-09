"""Checks on a would-be final answer (spec section 6, "Code guards").

A plain-text reply ends a step, so it is checked before it is accepted:

- narration: an answer never names a tool; one that does is the model thinking
  aloud, and in native mode that would end the task;
- grounding: concrete facts in the answer (IP addresses, paths, file names) must
  appear somewhere in what the task has seen, the request or a tool result.
  A small model can state a wrong address with the right one in front of it;
- skipped tools: a request to work something out exactly, or to say something
  aloud, is not answered in text before its tool ran (2^40 from memory was off by
  7 million; "read this out loud" was written, not said).

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

GAVE_UP_NUDGE: Final = (
    "Do not answer yet: look it up first. For the weather, call web_weather now with no "
    "arguments (it finds where the user is) or with place set to a place they named. For "
    "the song or video playing now, call media_now_playing. For a window, act on it by its "
    "app's name, or call window_list. For other current facts, call web_search."
)
# Small models answer "what's the weather" with "I don't have access to real-time
# data" or by asking where the user is, before trying the tool they were given.
_GAVE_UP: Final = re.compile(
    r"\b(don'?t|do not|can'?t|cannot|unable to)\b[^.]{0,40}\b(access|real[- ]time|"
    r"current|browse|look (it )?up|check)\b|\b(tell|let) me (know )?(your|the) "
    r"(city|location)\b|\bwhere you are\b|\bwhich (song|track|video|music)\b|"
    r"\bwhat (song|track) you\b|\bwhich (\w+ )?(window|tab|app)\b|\b(its|the) id\b",
    re.IGNORECASE,
)

REASONING_NUDGE: Final = (
    "That was your reasoning about the request, not an answer to the user. Reply to "
    "the user directly with the answer itself."
)
# gpt-oss at times returns its analysis as the reply: it talks about "the user" and
# plans the answer ("User asks ... We can answer from knowledge. Provide brief answer").
_REASONING: Final = re.compile(
    r"^\W*(the )?user (asks|asked|wants|is asking|said|says|requests)\b"
    r"|\b(we|i) (can|should|need to|will) (answer|respond|reply|provide)\b"
    r"|\bprovide (a |the )?(brief |short |concise )?(answer|response|reply)\b",
    re.IGNORECASE,
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


# Requests whose answer must come from a tool, by the tools that give it.
_NEEDS: Final = (
    (
        re.compile(
            r"\b(calculate|compute|work out|exactly|to the power|power of|square root|"
            r"factorial|how many (seconds|minutes|hours|days) (are )?in)\b|"
            r"\d\s*(\^|\*\*|\u00d7)\s*\d",
            re.IGNORECASE,
        ),
        ("shell_script", "shell_run"),
        "Do not do the arithmetic in your head: small models get long numbers wrong. "
        "Work it out with shell_script (python) and answer with what it prints.",
    ),
    (
        re.compile(
            r"\b(out ?loud|aloud|read (it|this|that|them) to me|say (it|this|that) "
            r"(to me|in your voice))\b",
            re.IGNORECASE,
        ),
        ("voice_say",),
        "The user wants to hear it: call voice_say with the words, then reply briefly.",
    ),
)


_NUMBER: Final = re.compile(r"\d[\d,_ ]*\d")


def _unseen_numbers(reply: str, messages: Sequence[Message]) -> list[str]:
    """Long numbers (5+ digits) in a calculation's answer that no tool result printed:
    the model kept its own sum after the script gave the right one."""
    printed = re.sub(r"[,_ ]", "", "\n".join(m.text for m in messages if m.role == "tool"))
    out: list[str] = []
    for raw in _NUMBER.findall(reply):
        digits = re.sub(r"[,_ ]", "", raw)
        if len(digits) >= 5 and digits not in printed:
            out.append(raw)
    return out


@dataclass(frozen=True, slots=True)
class Review:
    """Why a would-be answer was not accepted, and what to tell the model."""

    kind: Literal["narration", "grounding", "skipped"]
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
    wire_names = list(wire_names)  # read twice
    if narration and (named := narrated_tool(reply, wire_names)) is not None:
        return Review("narration", f"reply was narration naming {named}", NARRATION_NUDGE)
    if narration and _REASONING.search(reply):
        return Review("narration", "reply was the model's own reasoning", REASONING_NUDGE)
    if not grounding:
        return None
    tools_ran = any(m.role == "tool" for m in messages)
    request = next((m.text for m in messages if m.role == "user"), "")
    ran = {m.tool_name for m in messages if m.role == "tool"}
    for pattern, tools, nudge in _NEEDS if narration else ():  # the final answer only
        offered = [t for t in tools if t in wire_names]
        if offered and not ran & set(tools) and pattern.search(request):
            return Review("skipped", f"answered without {offered[0]}", nudge)
    calculated = ran & {"shell_script", "shell_run"} and _NEEDS[0][0].search(request)
    if calculated and (wrong := _unseen_numbers(reply, messages)):
        return Review(
            "grounding",
            f"reply stated {', '.join(wrong)}, which no result printed",
            f"Your answer says {', '.join(wrong)}, but the tool printed something else. "
            "Answer with exactly what the tool printed.",
        )
    lookups = {"web_search", "web_weather", "media_now_playing", "window_list"}
    if not tools_ran and _GAVE_UP.search(reply) and lookups & {*wire_names}:
        return Review("grounding", "reply gave up without a lookup", GAVE_UP_NUDGE)
    evidence = "\n".join(m.text for m in messages if m.role != "assistant")
    if missing := ungrounded(reply, evidence, tools_ran=tools_ran):
        return Review(
            "grounding", f"reply stated {', '.join(missing)} unseen", grounding_nudge(missing)
        )
    return None
