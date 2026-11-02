"""What Arcee remembers that matters now (spec section 8, decision 0014).

For a request (or a plan step) recall gathers the facts most worth knowing (high
importance) always, then the facts and past-task summaries whose words match, ranks
them by `0.6 * match + 0.25 * recency + 0.15 * importance`, and packs the best into
a small block for the Core's and the Planner's system prompts.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from arcee.context.store import MemoryRow, Store, SummaryRow

# Facts this important come along whatever the request: who the user is, where they live.
ALWAYS: Final = 0.8
ALWAYS_LIMIT: Final = 6
CANDIDATES: Final = 20
# The block's size, in characters (about 400 tokens): the Groq Core has 8,000 tokens a
# minute for everything, so memory gets a small, fixed share.
BUDGET_CHARS: Final = 1500
HALF_LIFE_DAYS: Final = 14.0
WEIGHTS: Final = (0.6, 0.25, 0.15)  # match, recency, importance


@dataclass(frozen=True, slots=True)
class Recalled:
    """One thing remembered: a fact (`m_...`) or a past task's summary (`t_...`)."""

    id: str
    text: str
    score: float
    when: float


def recency(ts: float, now: float) -> float:
    """1 now, 0.5 after HALF_LIFE_DAYS, halving again every half-life."""
    days = max(0.0, (now - ts) / 86400)
    return math.pow(0.5, days / HALF_LIFE_DAYS)


def rank(
    facts: list[tuple[MemoryRow, float]],
    summaries: list[tuple[SummaryRow, float]],
    always: list[MemoryRow],
    now: float,
) -> list[Recalled]:
    """Everything found, scored and best first; the always-facts lead."""
    best = max([score for _, score in facts] + [score for _, score in summaries] + [1e-9])
    w_match, w_recent, w_important = WEIGHTS
    out: dict[str, Recalled] = {}
    for row in always:
        out[row.id] = Recalled(row.id, row.text, 2.0 + row.importance, row.updated)
    for row, score in facts:
        if row.id in out:
            continue
        value = w_match * score / best + w_recent * recency(row.updated, now)
        out[row.id] = Recalled(row.id, row.text, value + w_important * row.importance, row.updated)
    for row, score in summaries:
        # A past task has no importance of its own: the middle of the range.
        value = w_match * score / best + w_recent * recency(row.created, now) + w_important * 0.5
        text = f"{row.request.strip()} → {row.text.strip()}"
        out[row.task_id] = Recalled(row.task_id, text, value, row.created)
    return sorted(out.values(), key=lambda r: r.score, reverse=True)


def pack(items: list[Recalled], budget: int = BUDGET_CHARS) -> list[Recalled]:
    """As many as fit in `budget` characters, in rank order."""
    kept: list[Recalled] = []
    used = 0
    for item in items:
        cost = len(item.text) + 24  # the line's date and bullet
        if used + cost > budget:
            continue
        kept.append(item)
        used += cost
    return kept


def render(items: list[Recalled]) -> str:
    """The block for a system prompt; "" when there is nothing to say."""
    if not items:
        return ""
    lines = [
        "What you remember (from earlier tasks; use it when it helps, and trust what "
        "the user says now over it):"
    ]
    for item in items:
        date = datetime.fromtimestamp(item.when).strftime("%Y-%m-%d")
        lines.append(f"- {item.text} ({date})")
    return "\n".join(lines)


async def recall(
    store: Store,
    query: str,
    *,
    exclude_task: str | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """The memory block for `query` (a request or a plan step)."""
    always = await store.top_memories(ALWAYS, ALWAYS_LIMIT)
    facts = await store.search_memories(query, CANDIDATES)
    summaries = await store.search_summaries(query, CANDIDATES, exclude=exclude_task)
    return render(pack(rank(facts, summaries, always, now())))
