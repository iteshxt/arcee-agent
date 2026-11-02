"""After a task: its summary, and what it taught about the user (decision 0014).

One call to the Core's fast model (the spec's Context role) turns the finished task
(request, steps, reply) into a summary of at most 120 words and the durable facts it
learned, each with an importance. The facts already saved that relate to the task are
shown with their ids, so a changed fact updates the old one instead of repeating it.
Across 103 real tasks the Core never saved a note by itself: memory has to be made
here, not left to it.
"""

import logging
from typing import Final

from pydantic import BaseModel, Field

from arcee.context.store import Store
from arcee.models.base import Message, ModelAdapter, ModelRequest

log = logging.getLogger(__name__)

STEP_LINES: Final = 12
RELATED: Final = 8
DIGEST_MAX_TOKENS: Final = 1024
# Nothing to learn from a request this bare that did nothing ("stop", "hi").
TRIVIAL_WORDS: Final = 3
# Share of a fact's content words that must appear in the task itself. LFM3-Omni,
# digesting a task that was a typo ("afasdf"), saved the prompt's own example fact
# (a home station) as the user's: a fact has to come from the task.
GROUNDED: Final = 0.6
# Words every fact may use that the task need not contain.
_FREE: Final = frozenset({"user", "users", "user's", "uses", "use", "prefers", "likes", "usual"})


def grounded(fact: str, task_text: str) -> bool:
    """Whether most of `fact`'s content words appear in the task's own words."""
    from arcee.context.examples import tokens

    words = [w for w in tokens(fact) if w not in _FREE]
    if not words:
        return False
    seen = set(tokens(task_text))
    return sum(w in seen for w in words) / len(words) >= GROUNDED


SYSTEM: Final = """\
You keep the memory of Arcee, an agent on the user's Ubuntu desktop. Given one finished \
task, write:
1. summary: what was asked and what happened, in at most 3 sentences (120 words), with \
the names, places, sites, files and numbers someone would search for later.
2. facts: durable facts about the user that THIS task's request, steps or reply state or \
plainly show, and that will help with future requests: their name, people, places, \
accounts and services they use, devices, folders, routines, preferences. At most 5, \
each one short sentence beginning "The user". Only what this task says: nothing \
guessed, nothing from these instructions. Not the task's own results ("found 3 \
files"), not facts about the world. Never passwords, codes, keys, card or account \
numbers. No facts is the usual answer: most tasks teach nothing lasting.
Each fact gets an importance from 0 to 1: 0.9 for who the user is and where they live, \
0.7 for standing preferences and accounts, 0.4 for useful details, 0.2 for passing ones. \
If a fact changes one of the facts already known (listed with ids), give that id in \
"replaces"; if it is already known as it is, leave it out.\
"""


class Fact(BaseModel):
    text: str = Field(max_length=300)
    importance: float = Field(ge=0, le=1)
    replaces: str | None = Field(default=None, description="id of the known fact it updates")


class TaskDigest(BaseModel):
    summary: str = Field(max_length=1200)
    facts: list[Fact] = Field(default_factory=list[Fact], max_length=5)


async def digest_task(model: ModelAdapter, store: Store, task_id: str) -> TaskDigest | None:
    """Summarise `task_id` and save what it taught; None when there was nothing to do."""
    found = await store.task(task_id)
    if found is None:
        return None
    task, steps = found
    if not steps and len(task.request.split()) < TRIVIAL_WORDS:
        return None
    lines = [f"Request: {task.request}", f"Outcome: {task.status}"]
    lines += [f"- {step.tool}: {step.summary}" for step in steps[-STEP_LINES:]]
    if len(steps) > STEP_LINES:
        lines.insert(2, f"({len(steps) - STEP_LINES} earlier steps not shown)")
    if task.reply:
        lines.append(f"Reply to the user: {task.reply[:1500]}")
    related = await store.search_memories(f"{task.request} {task.reply or ''}", RELATED)
    if related:
        lines.append("Facts already known:")
        lines += [f"- {row.id}: {row.text}" for row, _ in related]
    response = await model.complete(
        ModelRequest(
            system=SYSTEM,
            messages=[Message("user", "\n".join(lines))],
            schema=TaskDigest,
            max_tokens=DIGEST_MAX_TOKENS,
        )
    )
    digest = response.parsed
    await store.save_summary(task_id, digest.summary)
    known = {row.id for row, _ in related}
    task_text = "\n".join(lines[: lines.index("Facts already known:")] if related else lines)
    kept = [f for f in digest.facts if grounded(f.text, task_text)]
    if dropped := len(digest.facts) - len(kept):
        log.info("digest of %s: dropped %d facts not found in the task", task_id, dropped)
    digest = digest.model_copy(update={"facts": kept})
    for fact in digest.facts:
        replaces = fact.replaces if fact.replaces in known else None  # never a made-up id
        await store.save_memory(
            fact.text, fact.importance, task_id=task_id, source="digest", replaces=replaces
        )
    return digest
