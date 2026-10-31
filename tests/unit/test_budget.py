import json
from pathlib import Path

import httpx
import pytest

from arcee.models.base import Message, ToolRequest
from arcee.models.budget import HeaderBudget, parse_duration, parse_refusal
from arcee.models.errors import QuotaExhaustedError
from arcee.models.openai_compat import OpenAICompatAdapter
from arcee.models.presets import cloud_profile
from tests.fakes import FakeClock

GROQ = {
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "1000",
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-remaining-requests": "500",
    "x-ratelimit-reset-tokens": "52.5s",
}


class Now:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("600ms", 0.6), ("1m26.4s", 86.4), ("10m4.8s", 604.8), ("2h3m", 7380.0), ("", 0.0)],
)
def test_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == pytest.approx(seconds)


def test_requests_that_fit_go_at_once_and_refill_counts() -> None:
    now = Now()
    budget = HeaderBudget(None, now=now)
    budget.observe("m", GROQ)

    assert budget.wait_s("m", 200) == 0  # 1,000 left: fits with 800 headroom
    assert budget.wait_s("m", 600) == pytest.approx(3.0)  # 400 short at 133/s
    now.t += 3
    assert budget.wait_s("m", 600) == pytest.approx(0.0)


def test_spending_counts_before_the_reply() -> None:
    budget = HeaderBudget(None, now=Now())
    budget.observe("m", GROQ)

    budget.spend("m", 1000)

    assert budget.snapshot()["m"]["tokens_left"] == 0
    assert budget.snapshot()["m"]["requests_left"] == 499
    assert budget.wait_s("m", 600) == pytest.approx(10.5)  # 1,400 short, headroom included


def test_unknown_models_and_other_providers_never_wait() -> None:
    budget = HeaderBudget(None, now=Now())
    budget.observe("m", {"content-type": "application/json"})

    assert budget.wait_s("m", 5000) == 0
    assert budget.snapshot() == {}


def test_a_spent_day_stops_with_a_clear_message() -> None:
    budget = HeaderBudget(None, now=Now())
    budget.observe("m", {**GROQ, "x-ratelimit-remaining-requests": "0"})

    with pytest.raises(QuotaExhaustedError, match="today's 1000 requests are used up"):
        budget.wait_s("m", 100)


def test_counts_carry_across_runs(tmp_path: Path) -> None:
    now = Now()
    first = HeaderBudget(tmp_path / "rate-limits.json", now=now)
    first.observe("m", GROQ)
    now.t += 30  # half a minute later, in a new run

    second = HeaderBudget(tmp_path / "rate-limits.json", now=now)

    assert second.snapshot()["m"]["tokens_left"] == 5000  # 1,000 + 30 s x 133.3


async def test_adapter_waits_only_for_the_shortfall_and_records_headers() -> None:
    now = Now()
    budget = HeaderBudget(None, now=now)
    budget.observe("openai/gpt-oss-20b", {**GROQ, "x-ratelimit-remaining-tokens": "0"})
    reply = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 10}}
    headers = {**GROQ, "x-ratelimit-remaining-tokens": "7000"}

    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply, headers=headers)

    clock = FakeClock()
    client = httpx.AsyncClient(base_url="https://g.test/v1", transport=httpx.MockTransport(server))
    adapter = OpenAICompatAdapter(
        cloud_profile("openai/gpt-oss-20b"), "k", client=client, clock=clock, budget=budget
    )

    await adapter.respond(ToolRequest("s", [Message("user", "hi")], []))

    assert len(clock.sleeps) == 1
    assert 0 < clock.sleeps[0] < 12  # seconds of refill, headroom included
    assert adapter.last_wait_s == clock.sleeps[0]
    assert budget.snapshot()["openai/gpt-oss-20b"]["tokens_left"] == 7000


def test_output_size_is_learned_per_model() -> None:
    budget = HeaderBudget(None, now=Now())

    assert budget.expected_output("m") == 400
    budget.note_output("m", 2000)  # a long reasoning reply
    budget.note_output("m", 2000)

    assert budget.expected_output("m") == 2000
    assert budget.expected_output("other") == 400


TPD_REFUSAL = (
    "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service "
    "tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199655, Requested 1234. "
    "Please try again in 6m12s."
)


def test_parse_refusal() -> None:
    assert parse_refusal(TPD_REFUSAL) == ("TPD", 200_000, 199_655)
    assert parse_refusal("rate limited") is None


def test_replies_count_against_the_day_and_it_refills_slowly() -> None:
    now = Now()
    budget = HeaderBudget(None, now=now, daily_tokens={"m": 200_000})

    budget.use("m", 199_900)

    assert budget.snapshot()["m"]["day_tokens_left"] == 100
    assert budget.wait_s("m", 200) == pytest.approx(43.2)  # 100 short at 2.3 tokens/s
    now.t += 44
    assert budget.wait_s("m", 200) == 0


def test_a_spent_day_of_tokens_is_refused_with_a_clear_message() -> None:
    budget = HeaderBudget(None, now=Now(), daily_tokens={"m": 200_000})
    budget.use("m", 200_000)

    with pytest.raises(QuotaExhaustedError, match=r"200,000 tokens for the day.*about 22 min"):
        budget.wait_s("m", 3000)


def test_a_refusal_sets_the_true_daily_count(tmp_path: Path) -> None:
    now = Now()
    budget = HeaderBudget(tmp_path / "rate-limits.json", now=now)

    budget.refused("m", "TPD", 200_000, 199_655)

    again = HeaderBudget(tmp_path / "rate-limits.json", now=now)
    assert again.snapshot()["m"] == {"day_tokens_left": 345, "day_tokens_limit": 200_000}


def test_files_from_before_daily_tracking_still_load(tmp_path: Path) -> None:
    path = tmp_path / "rate-limits.json"
    old = {"m": {**dict.fromkeys(("tokens_limit", "requests_limit"), 8000), "at": 1_000_000.0}}
    old["m"].update(tokens_left=8000.0, requests_left=8000.0)
    path.write_text(json.dumps(old))

    assert HeaderBudget(path, now=Now()).snapshot()["m"]["tokens_limit"] == 8000


async def test_a_daily_refusal_is_a_quota_error_not_a_wait() -> None:
    budget = HeaderBudget(None, now=Now())

    def server(request: httpx.Request) -> httpx.Response:
        body = {"error": {"message": TPD_REFUSAL, "code": "rate_limit_exceeded"}}
        return httpx.Response(429, json=body, headers={**GROQ, "retry-after": "372"})

    clock = FakeClock()
    client = httpx.AsyncClient(base_url="https://g.test/v1", transport=httpx.MockTransport(server))
    adapter = OpenAICompatAdapter(
        cloud_profile("openai/gpt-oss-120b"), "k", client=client, clock=clock, budget=budget
    )

    with pytest.raises(
        QuotaExhaustedError, match=r"tokens for the day .*199,655 of 200,000.*6 min"
    ):
        await adapter.respond(ToolRequest("s", [Message("user", "hi")], []))
    assert clock.sleeps == []
    assert budget.snapshot()["openai/gpt-oss-120b"]["day_tokens_left"] == 345
