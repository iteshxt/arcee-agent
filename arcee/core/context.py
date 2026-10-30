"""What the Core sees of the task so far.

Every call resends the conversation, and on a cloud Core with a per-minute token
limit that is what makes a long task wait: a Gmail run read three pages and its
fourth call carried all three. Two rules keep each call small:

- a plan step starts fresh: the Core sees the request, a note from each finished
  step and the current step's own turns, not the earlier steps' tool results;
- inside a step only the latest results are shown whole; older ones shrink to
  their one-line summary (page snapshots are the bulk, and a newer one replaces them).

The full history stays in the agent's state for the guards and the store.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from arcee.models.base import Message

# Tool results shown whole inside a step; older ones are shortened.
KEEP_WHOLE: Final = 2
# Results shorter than this stay as they are: shortening them saves nothing.
SHORTEN_ABOVE: Final = 400


@dataclass(frozen=True, slots=True)
class Handoff:
    """A finished plan step and the note it left for the steps after it."""

    step: int
    do: str
    note: str


def core_view(
    messages: Sequence[Message],
    *,
    prelude: int,
    start: int,
    handoffs: Sequence[Handoff] = (),
    asked: Mapping[str, str] | None = None,
) -> list[Message]:
    """The request and planning turns, the finished steps' notes, then this step's turns.

    `prelude` ends the turns before the first step (the request, clarify answers);
    `start` is where the current step's turns begin.
    """
    view = list(messages[:prelude])
    if handoffs or asked:
        lines = ["Done so far (earlier tool results are not shown again):"]
        lines += [f"Step {h.step}: {h.do}\nResult: {h.note}" for h in handoffs]
        lines += [f"You asked the user: {q}\nThey answered: {a}" for q, a in (asked or {}).items()]
        view.append(Message("user", "\n".join(lines)))
    return view + shorten_old_results(messages[max(start, prelude) :])


def shorten_old_results(messages: Sequence[Message], keep: int = KEEP_WHOLE) -> list[Message]:
    """Replace all but the last `keep` long tool results with their summary."""
    results = [i for i, m in enumerate(messages) if m.role == "tool"]
    old = set(results[:-keep] if keep else results)
    return [
        _shortened(m) if i in old and len(m.text) > SHORTEN_ABOVE else m
        for i, m in enumerate(messages)
    ]


def _shortened(message: Message) -> Message:
    try:
        status = str(json.loads(message.text).get("status", "ok"))
    except ValueError, AttributeError:
        status = "ok"
    stub: dict[str, Any] = {
        "status": status,
        "summary": message.summary or "an earlier result",
        "note": "Shortened to save space; later results show the current state.",
    }
    return replace(message, text=json.dumps(stub, ensure_ascii=False))
