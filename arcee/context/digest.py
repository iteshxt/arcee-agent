"""After a task: its summary, and what it taught about the user (decision 0014).

One call to the Core's fast model (the spec's Context role) turns the finished task
(request, steps, reply) into a summary of at most 120 words and the durable facts it
learned, each with an importance. The facts already saved that relate to the task are
shown with their ids, so a changed fact updates the old one instead of repeating it.
Across 103 real tasks the Core never saved a note by itself: memory has to be made
here, not left to it.
"""

import logging
import re
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
# Facts below this importance are not kept: the prompt's "useful details" and "passing
# ones" were, in practice, the computer's state and the task's own results.
LASTING: Final = 0.6
# What a fact must not be about, whatever importance it was given. Kept by gpt-oss over
# a week (2026-11-06): "The user has an open Chrome window", "The user marked the
# outcome as done", "The user wants to mute audio", "The user has a default audio
# sink", "The user uses amixer to set Master volume", "The user's first email is from
# Google Photos", "The user has roughly 76 GB of free disk space".
_PASSING: Final = re.compile(
    r"\b(currently|right now|at the moment|today|open (chrome|window|tab)|an open|"
    r"(is|are|was) (open|playing|running|showing)|opened|free (disk )?space|disk space|"
    r"volume|audio (sink|source|device)|mixer|battery|brightness|"
    r"(first|latest|last|newest|recent) (email|message|event|file)|"
    r"marked|wants? to|wanted to|may want|might want|asked|requested|tried|"
    r"is (viewing|looking|reading|watching)|home directory|home folder|"
    r"uses (a |an |the )?(browser|terminal|command|shell|amixer|pactl|wpctl|gio|xdg-open)|"
    r"uses \S+ to (set|open|run|search|check|change|turn))\b",
    re.IGNORECASE,
)
_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def lasting(fact: str) -> bool:
    """Whether `fact` could still be true and worth knowing weeks from now: not the
    computer's state, not what this task did or wanted, not someone else's address."""
    if _PASSING.search(fact):
        return False
    # An address is the user's own account, never someone else's they happened to see.
    return not _EMAIL.search(fact) or "account" in fact.lower()


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
2. facts: lasting facts about the user as a person, that will still be true and useful \
weeks from now, and that THIS task's request, steps or reply state or plainly show: \
their name, work or studies, where they live, the people in their life, their own \
accounts, the apps and services they choose, standing preferences and routines. At \
most 3, each one short sentence beginning "The user". Only what this task says: \
nothing guessed, nothing from these instructions.
Never a fact about:
- the computer's state at the time: open windows or tabs, free disk space, volume, \
audio devices, what is playing or on screen;
- this task itself: what the user asked, wanted or marked, what was found, which \
email or file came first;
- how a task was done: commands, tools or programs used to do it;
- other people's email addresses or numbers, or anything in someone else's message;
- the world in general. Never passwords, codes, keys, card or account numbers.
No facts is the usual answer: almost every task teaches nothing lasting.
Each fact gets an importance from 0 to 1: 0.9 for who the user is and where they live, \
0.7 for standing preferences, people and accounts; anything less is not worth keeping. \
If a fact changes one of the facts already known (listed with ids), give that id in \
"replaces"; if it is already known as it is, leave it out.\
"""


# For models too small to tell a lasting fact from a task's result (LFM3-Omni, run
# over 101 past tasks, saved "YouTube is now open in Chrome" and "the biggest file in
# Downloads is ..." as facts at importance 0.9): the summary alone, which they do well.
SUMMARY_ONLY: Final = """\
You keep the memory of Arcee, an agent on the user's Ubuntu desktop. Given one finished \
task, write summary: what was asked and what happened, in at most 3 sentences (120 \
words), with the names, places, sites, files and numbers someone would search for later. \
Leave facts empty.\
"""


class Fact(BaseModel):
    text: str = Field(max_length=300)
    importance: float = Field(ge=0, le=1)
    replaces: str | None = Field(default=None, description="id of the known fact it updates")


class TaskDigest(BaseModel):
    summary: str = Field(max_length=1200)
    facts: list[Fact] = Field(default_factory=list[Fact], max_length=5)


async def digest_task(
    model: ModelAdapter, store: Store, task_id: str, *, facts: bool = True
) -> TaskDigest | None:
    """Summarise `task_id` and save what it taught; None when there was nothing to do.
    `facts`: also learn facts about the user (only on models able to tell them apart)."""
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
    related = (
        await store.search_memories(f"{task.request} {task.reply or ''}", RELATED) if facts else []
    )
    if related:
        lines.append("Facts already known:")
        lines += [f"- {row.id}: {row.text}" for row, _ in related]
    response = await model.complete(
        ModelRequest(
            system=SYSTEM if facts else SUMMARY_ONLY,
            messages=[Message("user", "\n".join(lines))],
            schema=TaskDigest,
            max_tokens=DIGEST_MAX_TOKENS,
        )
    )
    digest = response.parsed
    await store.save_summary(task_id, digest.summary)
    known = {row.id for row, _ in related}
    task_text = "\n".join(lines[: lines.index("Facts already known:")] if related else lines)
    kept = [
        f
        for f in digest.facts
        if facts
        and f.text.lower().startswith("the user")
        and f.importance >= LASTING
        and lasting(f.text)
        and grounded(f.text, task_text)
    ]
    if dropped := len(digest.facts) - len(kept):
        log.info("digest of %s: dropped %d facts (passing, or not in the task)", task_id, dropped)
    digest = digest.model_copy(update={"facts": kept})
    for fact in digest.facts:
        replaces = fact.replaces if fact.replaces in known else None  # never a made-up id
        await store.save_memory(
            fact.text, fact.importance, task_id=task_id, source="digest", replaces=replaces
        )
    return digest
