"""Round-trips against the real Gemini API. Run with: uv run --env-file .env pytest -m live"""

import os

import pytest

from arcee.core.protocol import CoreAction, PlannerOutput
from arcee.models.base import Message, ModelProfile, ModelRequest
from arcee.models.gemini import API_KEY_ENV, GeminiAdapter

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get(API_KEY_ENV), reason=f"{API_KEY_ENV} not set"),
]

FLASH_LITE = ModelProfile(
    model="gemini-3.5-flash-lite",
    json_mode="schema",
    context_tokens=1_048_576,
    rpm=15,
    cost_tier="free",
    thinking=False,
)
GEMMA = FLASH_LITE.model_copy(update={"model": "gemma-4-26b-a4b-it", "context_tokens": 262_144})


@pytest.fixture(params=[FLASH_LITE, GEMMA], ids=lambda p: p.model)
def profile(request: pytest.FixtureRequest) -> ModelProfile:
    return request.param


SYSTEM = (
    "You are the core of a desktop agent. Reply with exactly one JSON object.\n"
    "Tools: fs.list(path), fs.read(path, max_bytes?), control.done(reply)."
)


async def test_core_action_round_trip(profile: ModelProfile) -> None:
    async with GeminiAdapter.from_env(profile) as model:
        response = await model.complete(
            ModelRequest(
                system=SYSTEM,
                messages=[Message("user", "Step 1: list the files in ~/Downloads.")],
                schema=CoreAction,
            )
        )

    assert response.parsed.action.tool == "fs.list"
    assert response.parsed.action.args.get("path") == "~/Downloads"


async def test_planner_round_trip_with_thinking(profile: ModelProfile) -> None:
    thinking = profile.model_copy(update={"thinking": True})
    async with GeminiAdapter.from_env(thinking) as model:
        response = await model.complete(
            ModelRequest(
                system="You plan tasks for a desktop agent. Reply with exactly one JSON object.",
                messages=[
                    Message("user", "Find the three largest files in ~/Downloads and tell me.")
                ],
                schema=PlannerOutput,
                max_tokens=4096,
            )
        )

    assert response.parsed.steps
    assert response.parsed.clarify is None
