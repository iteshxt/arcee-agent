from pathlib import Path

import httpx
import pytest

from arcee.models.base import Message, ToolRequest
from arcee.models.budget import HeaderBudget, parse_duration
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
