"""A Groq rank with the local model behind it: a spent day moves the call to this computer."""

import pytest

from arcee.models.base import Message, ModelProfile, ToolRequest
from arcee.models.errors import ModelUnavailableError, QuotaExhaustedError
from arcee.models.fake import FakeAdapter
from arcee.models.fallback import FallbackAdapter

CLOUD = ModelProfile(
    model="openai/gpt-oss-20b", context_tokens=65_536, cost_tier="free", call_mode="native"
)
LOCAL = ModelProfile(
    model="lfm3-omni", context_tokens=32_768, cost_tier="local", call_mode="native"
)
ASK = ToolRequest(system="s", messages=(Message("user", "hi"),), tools=())


async def test_the_cloud_answers_while_it_can_then_this_computer() -> None:
    cloud = FakeAdapter(["from groq", QuotaExhaustedError("tokens for the day are used up")], CLOUD)
    local = FakeAdapter(["from lfm"], LOCAL)
    model = FallbackAdapter(cloud, local)

    first = await model.respond(ASK)
    assert (first.text, model.profile.model, model.fell_back) == ("from groq", CLOUD.model, "")

    second = await model.respond(ASK)

    assert second.text == "from lfm"
    assert model.profile.model == "lfm3-omni"
    assert model.fell_back == "tokens for the day are used up"
    # Personal data is still asked about as for the cloud: the next call may go there.
    assert model.profile.cost_tier == "free"
    assert model.label == "the cloud model"


async def test_back_to_the_cloud_once_it_answers_again() -> None:
    cloud = FakeAdapter([ModelUnavailableError("groq unreachable"), "groq again"], CLOUD)
    local = FakeAdapter(["from lfm", "lfm again"], LOCAL)
    now = [0.0]
    model = FallbackAdapter(cloud, local, clock=lambda: now[0])

    assert (await model.respond(ASK)).text == "from lfm"
    assert (await model.respond(ASK)).text == "lfm again"  # the cloud rests a minute
    assert len(cloud.requests) == 1
    now[0] = 61.0
    assert (await model.respond(ASK)).text == "groq again"
    assert model.profile.model == CLOUD.model
    assert model.fell_back == ""


async def test_both_down_is_the_local_models_error() -> None:
    cloud = FakeAdapter([QuotaExhaustedError("spent")], CLOUD)
    local = FakeAdapter([ModelUnavailableError("Ollama is not running")], LOCAL)
    model = FallbackAdapter(cloud, local)

    with pytest.raises(ModelUnavailableError, match="Ollama is not running"):
        await model.respond(ASK)
