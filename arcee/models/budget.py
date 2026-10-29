"""Rate budgets from the provider's own counts (Groq's x-ratelimit-* headers).

Groq answers every request with what is left of its two allowances, tokens per
minute and requests per day, and how long until each is full again. Both refill
continuously: on the free tier 8,000 tokens over a minute and 1,000 requests
over a day. So the budget keeps the last reported count per model, adds what
has refilled since, and holds a request back only when it would not fit, for
only as long as the refill takes. Counts are saved to disk, so the next run
starts from the truth instead of from zero, and `arcee-cli limits` can show them.
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from arcee.models.errors import QuotaExhaustedError

TOKEN_WINDOW_S: Final = 60.0
# Share of the token limit kept unused, so a reply longer than expected (gpt-oss reasons
# before answering, and Groq counts that as output) cannot push a minute over the limit.
HEADROOM: Final = 0.10
# Output tokens assumed before a model's real replies have been seen.
DEFAULT_OUTPUT: Final = 400
# Weight of the newest reply in the running average of output size.
OUTPUT_SMOOTHING: Final = 0.3
REQUEST_WINDOW_S: Final = 86_400.0
_DURATION: Final = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S: Final = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def parse_duration(text: str) -> float:
    """Groq's reset durations: "600ms", "1m26.4s", "10m4.8s", "2h3m"."""
    return sum(float(n) * _UNIT_S[unit] for n, unit in _DURATION.findall(text))


@dataclass
class Allowance:
    """One model's two allowances as last reported, and when."""

    tokens_limit: int
    tokens_left: float
    requests_limit: int
    requests_left: float
    at: float  # wall-clock seconds, so counts carry across runs

    def tokens_now(self, now: float) -> float:
        refill = (now - self.at) * self.tokens_limit / TOKEN_WINDOW_S
        return min(float(self.tokens_limit), self.tokens_left + refill)

    def requests_now(self, now: float) -> float:
        refill = (now - self.at) * self.requests_limit / REQUEST_WINDOW_S
        return min(float(self.requests_limit), self.requests_left + refill)

    def wait_for(self, tokens: int, now: float) -> float:
        """Seconds until `tokens` fit with the headroom left unused."""
        reserve = HEADROOM * self.tokens_limit
        need = min(float(tokens) + reserve, float(self.tokens_limit)) - self.tokens_now(now)
        return max(0.0, need * TOKEN_WINDOW_S / self.tokens_limit)


class HeaderBudget:
    """Per-model allowances from response headers, saved to `path` after each update."""

    def __init__(self, path: Path | None, *, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._now = now
        self._models: dict[str, Allowance] = self._load()
        self._output: dict[str, float] = {}

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Current tokens and requests left per model, refill included."""
        now = self._now()
        return {
            model: {
                "tokens_left": round(a.tokens_now(now)),
                "tokens_limit": a.tokens_limit,
                "requests_left": int(a.requests_now(now)),
                "requests_limit": a.requests_limit,
            }
            for model, a in self._models.items()
        }

    def wait_s(self, model: str, tokens: int) -> float:
        """How long to hold a request of `tokens` back; 0 when it fits now or nothing is known.

        Raises QuotaExhaustedError when the day's requests are used up.
        """
        allowance = self._models.get(model)
        if allowance is None:
            return 0.0
        now = self._now()
        if allowance.requests_now(now) < 1:
            hours = (
                (1 - allowance.requests_now(now))
                * REQUEST_WINDOW_S
                / allowance.requests_limit
                / 3600
            )
            raise QuotaExhaustedError(
                f"{model}: today's {allowance.requests_limit} requests are used up; "
                f"the next frees up in about {hours:.1f} h"
            )
        return allowance.wait_for(tokens, now)

    def expected_output(self, model: str) -> int:
        """Typical output tokens for `model`, learned from its replies."""
        return round(self._output.get(model, DEFAULT_OUTPUT))

    def note_output(self, model: str, tokens: int) -> None:
        """Fold a reply's output tokens (reasoning included) into the running average."""
        if tokens > 0:
            previous = self._output.get(model, float(tokens))
            self._output[model] = (1 - OUTPUT_SMOOTHING) * previous + OUTPUT_SMOOTHING * tokens

    def spend(self, model: str, tokens: int) -> None:
        """Count a request as sent before its reply, so back-to-back calls see it."""
        allowance = self._models.get(model)
        if allowance is not None:
            now = self._now()
            allowance.tokens_left = allowance.tokens_now(now) - tokens
            allowance.requests_left = allowance.requests_now(now) - 1
            allowance.at = now

    def observe(self, model: str, headers: Mapping[str, str]) -> None:
        """Take the provider's counts from a reply's headers, then save."""
        try:
            allowance = Allowance(
                tokens_limit=int(headers["x-ratelimit-limit-tokens"]),
                tokens_left=float(headers["x-ratelimit-remaining-tokens"]),
                requests_limit=int(headers["x-ratelimit-limit-requests"]),
                requests_left=float(headers["x-ratelimit-remaining-requests"]),
                at=self._now(),
            )
        except KeyError, ValueError:
            return  # a provider without these headers keeps the limiter's own budget
        self._models[model] = allowance
        self._save()

    def _load(self) -> dict[str, Allowance]:
        if self._path is None or not self._path.is_file():
            return {}
        try:
            raw: dict[str, Any] = json.loads(self._path.read_text())
            return {model: Allowance(**values) for model, values in raw.items()}
        except ValueError, TypeError:
            return {}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps({m: asdict(a) for m, a in self._models.items()}, indent=1))
        temporary.replace(self._path)
