import math
from typing import Any

import pytest

from arcee.models.base import Completion, Message, ModelProfile, ModelRequest, ToolRequest
from arcee.models.errors import QuotaExhaustedError
from arcee.models.fake import FakeAdapter
from arcee.models.rotation import RotatingAdapter

REQUEST = ToolRequest("s", [Message("user", "hi")], [])


class Timed(FakeAdapter):
    """A model whose rate budget would hold the next call back `wait` seconds."""

    def __init__(self, name: str, wait: float, script: list[Any] | None = None) -> None:
        profile = ModelProfile(
            model=name, context_tokens=8192, cost_tier="free", call_mode="native"
        )
        super().__init__(script or [Completion(f"from {name}")], profile)
        self.wait = wait

    def expected_wait(self, request: ToolRequest | ModelRequest[Any]) -> float:
        return self.wait


async def test_the_leader_answers_when_it_can() -> None:
    rotation = RotatingAdapter([Timed("20b", 0.5), Timed("qwen", 0), Timed("120b", 0)])

    reply = await rotation.respond(REQUEST)

    assert reply.text == "from 20b"
    assert rotation.profile.model == "20b"


async def test_a_busy_leader_hands_the_call_to_a_ready_model() -> None:
    rotation = RotatingAdapter([Timed("20b", 30), Timed("qwen", 12), Timed("120b", 0)])

    reply = await rotation.respond(REQUEST)

    assert reply.text == "from 120b"
    assert rotation.profile.model == "120b"  # events name the model that answered


async def test_when_all_would_wait_the_soonest_goes_first() -> None:
    rotation = RotatingAdapter([Timed("20b", 30), Timed("qwen", 12), Timed("120b", 40)])

    assert [m.profile.model for m in rotation.pick(REQUEST)] == ["qwen", "20b", "120b"]


async def test_a_model_spent_for_the_day_is_skipped() -> None:
    spent = Timed("20b", 0, [QuotaExhaustedError("20b: its tokens for the day are used up")])
    rotation = RotatingAdapter([spent, Timed("qwen", math.inf), Timed("120b", 0)])

    reply = await rotation.respond(REQUEST)

    assert reply.text == "from 120b"
    assert [m.profile.model for m in rotation.pick(REQUEST)] == ["20b", "120b"]


async def test_every_model_spent_is_one_clear_error() -> None:
    rotation = RotatingAdapter([Timed("20b", math.inf), Timed("120b", math.inf)])

    with pytest.raises(QuotaExhaustedError, match=r"every model's allowance.*20b, 120b"):
        await rotation.respond(REQUEST)
