"""The worked-example library (spec section 8, "Context assembly").

Per step, the examples closest to the current situation are put in the Core's
prompt. Retrieval is lexical (BM25 over the situations) for now: fast,
deterministic and model-free; the embedder role (phase 5) can replace it
behind `search`. Nothing is returned below a score threshold, because a small
model copies a near-miss example's answer.
"""

import math
import re
import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Final, Self

DEFAULT_K: Final = 2
# BM25 score an example needs to be shown; tuned on the library's own situations.
MIN_SCORE: Final = 2.0
# And at least this fraction of the best match's score, so a weak second is dropped.
RELATIVE_CUTOFF: Final = 0.8
_K1: Final = 1.2
_B: Final = 0.75
_WORD: Final = re.compile(r"[a-z0-9]+(?:[._][a-z0-9]+)*")
_STOP: Final = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
        "in", "is", "it", "its", "me", "my", "of", "on", "or", "the", "their", "this", "to",
        "was", "what", "with", "you", "your",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Example:
    id: str
    situation: str
    action: str
    why: str
    # Words a request with this intent uses; retrieval matches on these, not on the
    # folders and files a situation happens to mention.
    tags: str


def tokens(text: str) -> list[str]:
    """Lowercase content words, crudely singular so 'photos' meets 'photo'."""
    words = (w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1)
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


class ExampleLibrary:
    def __init__(self, examples: Sequence[Example]) -> None:
        self.examples = list(examples)
        self._docs = [Counter(tokens(e.tags)) for e in self.examples]
        lengths = [sum(doc.values()) for doc in self._docs]
        self._avg = sum(lengths) / len(lengths) if lengths else 1.0
        self._lengths = lengths
        df: Counter[str] = Counter()
        for doc in self._docs:
            df.update(doc.keys())
        n = len(self._docs)
        self._idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    @classmethod
    def load(cls, text: str | None = None) -> Self:
        """The packaged library, or `text` in the same TOML shape."""
        raw = text if text is not None else _packaged()
        entries = tomllib.loads(raw).get("example", [])
        return cls(
            [Example(e["id"], e["situation"], e["action"], e["why"], e["tags"]) for e in entries]
        )

    def scored(self, query: str) -> list[tuple[float, Example]]:
        terms = set(tokens(query))
        results: list[tuple[float, Example]] = []
        for doc, length, example in zip(self._docs, self._lengths, self.examples, strict=True):
            score = 0.0
            for term in terms & doc.keys():
                tf = doc[term]
                norm = tf + _K1 * (1 - _B + _B * length / self._avg)
                score += self._idf[term] * tf * (_K1 + 1) / norm
            if score > 0:
                results.append((score, example))
        return sorted(results, key=lambda pair: -pair[0])

    def search(self, query: str, k: int = DEFAULT_K) -> list[Example]:
        scored = self.scored(query)
        if not scored or scored[0][0] < MIN_SCORE:
            return []
        floor = max(MIN_SCORE, scored[0][0] * RELATIVE_CUTOFF)
        return [example for score, example in scored[:k] if score >= floor]


def render(examples: Sequence[Example]) -> str:
    """The prompt block; empty when there is nothing close enough to show."""
    if not examples:
        return ""
    lines = ["Similar situations and the right move:"]
    lines += [f"- {e.situation} -> {e.action} ({e.why})" for e in examples]
    return "\n".join(lines)


def _packaged() -> str:
    return resources.files("arcee.context").joinpath("examples.toml").read_text("utf-8")
