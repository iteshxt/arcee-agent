"""Rate budgets from the provider's own counts (Groq's x-ratelimit-* headers).

Groq answers every request with what is left of its two allowances, tokens per
minute and requests per day, and how long until each is full again. Both refill
continuously: on the free tier 8,000 tokens over a minute and 1,000 requests
over a day. So the budget keeps the last reported count per model, adds what
has refilled since, and holds a request back only when it would not fit, for
only as long as the refill takes. Counts are saved to disk, so the next run
starts from the truth instead of from zero, and `arcee-cli limits` can show them.

Groq also limits tokens per day (200,000 for gpt-oss), which no header reports.
The budget counts each reply's tokens against it, refilling at a day's rate (a
refusal for this limit asked for 372 s while about 860 tokens short), and takes
the true count from a 429's message when Groq refuses on it.
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
DAY_S: Final = 86_400.0
# Longer than this for the daily allowance and the request is refused, not held back.
MAX_DAY_WAIT_S: Final = 120.0
# Groq's 429 message: "... on tokens per day (TPD): Limit 200000, Used 199655, Requested 1234."
_REFUSAL: Final = re.compile(r"\((TPM|TPD|RPM|RPD)\): Limit (\d+), Used (\d+)")
_DURATION: Final = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_S: Final = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def parse_refusal(message: str) -> tuple[str, int, int] | None:
    """Which limit a 429 names, with its limit and the amount used; None if it names none."""
    if (match := _REFUSAL.search(message)) is None:
        return None
    return match[1], int(match[2]), int(match[3])


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


@dataclass
class DailyTokens:
    """One model's tokens-per-day allowance as counted here, and when."""

    limit: int
    left: float
    at: float

    def left_now(self, now: float) -> float:
        return min(float(self.limit), self.left + (now - self.at) * self.limit / DAY_S)

    def take(self, tokens: float, now: float) -> None:
        self.left, self.at = self.left_now(now) - tokens, now


def describe_wait(seconds: float) -> str:
    """A wait in words: "40 s", "6 min", "2.1 h"."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


class HeaderBudget:
    """Per-model allowances from response headers, saved to `path` after each update.

    `daily_tokens` gives the tokens-per-day limit of the models that have one.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        now: Callable[[], float] = time.time,
        daily_tokens: Mapping[str, int] | None = None,
    ) -> None:
        self._path = path
        self._now = now
        self._daily_limits = dict(daily_tokens or {})
        self._models: dict[str, Allowance] = {}
        self._daily: dict[str, DailyTokens] = {}
        self._load()
        self._output: dict[str, float] = {}

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Current tokens and requests left per model, refill included."""
        now = self._now()
        out: dict[str, dict[str, float]] = {
            model: {
                "tokens_left": round(a.tokens_now(now)),
                "tokens_limit": a.tokens_limit,
                "requests_left": int(a.requests_now(now)),
                "requests_limit": a.requests_limit,
            }
            for model, a in self._models.items()
        }
        for model, day in self._daily.items():
            out.setdefault(model, {}).update(
                day_tokens_left=max(0, round(day.left_now(now))), day_tokens_limit=day.limit
            )
        return out

    def wait_s(self, model: str, tokens: int) -> float:
        """How long to hold a request of `tokens` back; 0 when it fits now or nothing is known.

        Raises QuotaExhaustedError when the day's requests or tokens are used up.
        """
        now = self._now()
        day_wait = self._day_wait(model, tokens, now)
        allowance = self._models.get(model)
        if allowance is None:
            return day_wait
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
        return max(allowance.wait_for(tokens, now), day_wait)

    def _day_wait(self, model: str, tokens: int, now: float) -> float:
        day = self._day(model)
        if day is None:
            return 0.0
        short = tokens - day.left_now(now)
        if short <= 0:
            return 0.0
        wait = short * DAY_S / day.limit
        if wait > MAX_DAY_WAIT_S:
            raise QuotaExhaustedError(
                f"{model}: its {day.limit:,} tokens for the day are used up; enough for "
                f"this request frees up in about {describe_wait(wait)}"
            )
        return wait

    def _day(self, model: str) -> DailyTokens | None:
        if model not in self._daily and model in self._daily_limits:
            limit = self._daily_limits[model]
            self._daily[model] = DailyTokens(limit, float(limit), self._now())
        return self._daily.get(model)

    def use(self, model: str, tokens: int) -> None:
        """Count a reply's tokens (input and output) against the day's allowance."""
        if tokens > 0 and (day := self._day(model)) is not None:
            day.take(tokens, self._now())
            self._save()

    def refused(self, model: str, kind: str, limit: int, used: int) -> None:
        """Take the true count from a refusal ("tokens per day: Limit 200000, Used 199655")."""
        now = self._now()
        if kind == "TPD":
            self._daily_limits[model] = limit
            self._daily[model] = DailyTokens(limit, float(limit - used), now)
        elif (allowance := self._models.get(model)) is not None:
            if kind == "TPM":
                allowance.tokens_limit = limit
                allowance.tokens_left = float(limit - used)
            elif kind == "RPD":
                allowance.requests_limit = limit
                allowance.requests_left = float(limit - used)
            else:
                return
            allowance.at = now
        else:
            return
        self._save()

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

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            raw: dict[str, Any] = json.loads(self._path.read_text())
            # Files from before daily tracking hold only the per-minute section.
            minute: dict[str, Any] = raw.get("minute", raw)
            self._models = {model: Allowance(**values) for model, values in minute.items()}
            self._daily = {
                model: DailyTokens(**values) for model, values in raw.get("day", {}).items()
            }
        except ValueError, TypeError:
            self._models, self._daily = {}, {}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        state = {
            "minute": {m: asdict(a) for m, a in self._models.items()},
            "day": {m: asdict(d) for m, d in self._daily.items()},
        }
        temporary.write_text(json.dumps(state, indent=1))
        temporary.replace(self._path)
