import json

import pytest
from pydantic import BaseModel

from arcee.core.protocol import CoreAction, PlannerOutput
from arcee.models.base import (
    BACKOFF_S,
    MAX_RETRIES,
    Message,
    ModelProfile,
    ModelRequest,
    TokenUsage,
    provider_schema,
)
from arcee.models.errors import (
    ModelOutputError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitedError,
    ServerBusyError,
)
from arcee.models.fake import FAKE_PROFILE, FakeAdapter
from tests.fakes import FakeClock

GOOD = '{"action": {"tool": "fs.list", "args": {"path": "~"}}}'
BAD_TOOL = '{"action": {"tool": "FS"}}'


def request() -> ModelRequest[CoreAction]:
    return ModelRequest(system="sys", messages=[Message("user", "go")], schema=CoreAction)


class TestComplete:
    async def test_first_try_success(self) -> None:
        adapter = FakeAdapter([GOOD])

        response = await adapter.complete(request())

        assert response.parsed.action.tool == "fs.list"
        assert response.attempts == 1
        assert response.raw == GOOD

    async def test_retries_with_validation_feedback(self) -> None:
        adapter = FakeAdapter([BAD_TOOL, GOOD])

        response = await adapter.complete(request())

        assert response.attempts == 2
        retry = adapter.requests[1].messages
        assert retry[-2] == Message("assistant", BAD_TOOL)
        assert retry[-1].role == "user"
        assert retry[-1].text.startswith("VALIDATION ERROR: action.tool:")

    async def test_raises_after_retry_budget(self) -> None:
        adapter = FakeAdapter([BAD_TOOL] * (MAX_RETRIES + 1))

        with pytest.raises(ModelOutputError) as caught:
            await adapter.complete(request())

        assert caught.value.attempts == MAX_RETRIES + 1
        assert caught.value.raw == BAD_TOOL
        assert len(adapter.requests) == MAX_RETRIES + 1

    async def test_does_not_mutate_caller_messages(self) -> None:
        req = request()
        adapter = FakeAdapter([BAD_TOOL, GOOD])

        await adapter.complete(req)

        assert list(req.messages) == [Message("user", "go")]

    async def test_sums_usage_across_attempts(self) -> None:
        adapter = FakeAdapter([BAD_TOOL, GOOD])

        response = await adapter.complete(request())

        assert response.usage.output_tokens == len(BAD_TOOL) // 4 + len(GOOD) // 4

    async def test_sends_schema_only_when_profile_has_json_mode(self) -> None:
        with_schema = FakeAdapter([GOOD])
        without = FakeAdapter([GOOD], FAKE_PROFILE.model_copy(update={"json_mode": "none"}))

        await with_schema.complete(request())
        await without.complete(request())

        assert with_schema.requests[0].schema == provider_schema(CoreAction)
        assert without.requests[0].schema is None

    async def test_transport_errors_propagate_without_retry(self) -> None:
        adapter = FakeAdapter([])

        with pytest.raises(ModelUnavailableError):
            await adapter.complete(request())


class TestRateLimiting:
    async def test_retries_429_with_exponential_backoff(self) -> None:
        clock = FakeClock()
        adapter = FakeAdapter([RateLimitedError(), RateLimitedError(), GOOD], clock=clock)

        response = await adapter.complete(request())

        assert response.attempts == 1  # backoff retries are not model attempts
        assert clock.sleeps == [1.0, 2.0]

    async def test_retries_server_errors_on_the_same_schedule(self) -> None:
        clock = FakeClock()
        adapter = FakeAdapter([ServerBusyError("HTTP 503"), GOOD], clock=clock)

        await adapter.complete(request())

        assert clock.sleeps == [1.0]

    async def test_honours_longer_retry_after(self) -> None:
        clock = FakeClock()
        adapter = FakeAdapter([RateLimitedError(retry_after_s=7), GOOD], clock=clock)

        await adapter.complete(request())

        assert clock.sleeps == [7]

    async def test_gives_up_after_backoff_schedule(self) -> None:
        clock = FakeClock()
        adapter = FakeAdapter([RateLimitedError()] * (len(BACKOFF_S) + 1), clock=clock)

        with pytest.raises(ModelUnavailableError, match="rate limited"):
            await adapter.complete(request())
        assert clock.sleeps == list(BACKOFF_S)

    async def test_retry_after_beyond_limit_is_unavailable_at_once(self) -> None:
        clock = FakeClock()
        adapter = FakeAdapter([RateLimitedError(retry_after_s=600), GOOD], clock=clock)

        with pytest.raises(ModelUnavailableError):
            await adapter.complete(request())
        assert clock.sleeps == []

    async def test_each_round_trip_uses_the_daily_budget(self) -> None:
        profile = FAKE_PROFILE.model_copy(update={"rpd": 2})
        adapter = FakeAdapter([BAD_TOOL, GOOD, GOOD], profile, clock=FakeClock())

        await adapter.complete(request())  # two round-trips: invalid reply, then retry

        with pytest.raises(QuotaExhaustedError):
            await adapter.complete(request())


class TestProviderSchema:
    @pytest.mark.parametrize("model", [CoreAction, PlannerOutput])
    def test_has_no_refs_defs_or_titles(self, model: type[BaseModel]) -> None:
        text = json.dumps(provider_schema(model))

        assert "$ref" not in text
        assert "$defs" not in text
        assert '"title"' not in text

    def test_inlines_definitions_with_sibling_keywords(self) -> None:
        status = provider_schema(CoreAction)["properties"]["step_status"]

        assert status["enum"] == ["in_progress", "step_done", "task_done", "stuck"]
        assert status["default"] == "in_progress"
        assert "description" in status

    def test_keeps_properties_named_like_keywords(self) -> None:
        class Notify(BaseModel):
            title: str
            enum: int

        assert set(provider_schema(Notify)["properties"]) == {"title", "enum"}

    def test_rejects_recursive_models(self) -> None:
        class Node(BaseModel):
            children: list[Node]

        with pytest.raises(ValueError, match="recursive"):
            provider_schema(Node)


def test_token_usage_adds() -> None:
    assert TokenUsage(1, 2) + TokenUsage(3, 4) == TokenUsage(4, 6)


def test_profile_rejects_unknown_flags() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        ModelProfile.model_validate(
            {"model": "m", "context_tokens": 8192, "cost_tier": "free", "streaming": True}
        )


async def test_async_context_closes_adapter() -> None:
    closed: list[bool] = []

    class Closing(FakeAdapter):
        async def aclose(self) -> None:
            closed.append(True)

    async with Closing([]):
        pass

    assert closed == [True]
