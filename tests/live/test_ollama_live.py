"""Round-trips against the local Ollama with the default Core model.

Run with `uv run pytest -m live`; skipped when Ollama or the model is missing.
"""

import pytest

from arcee.models.base import Message, ModelProfile, ToolRequest
from arcee.models.ollama import ALL_LAYERS, OllamaAdapter

pytestmark = pytest.mark.live

CORE = ModelProfile(
    model="lfm3", context_tokens=8192, cost_tier="local", call_mode="native", thinking=False
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fs_list",
            "description": "List the entries of a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]


@pytest.fixture
async def core() -> OllamaAdapter:
    model = OllamaAdapter(CORE, options={"num_gpu": ALL_LAYERS, "num_ctx": 8192})
    if not await model.health():
        await model.aclose()
        pytest.skip("Ollama is not running or lfm3 is not pulled")
    return model


async def test_native_turn_calls_a_tool(core: OllamaAdapter) -> None:
    async with core:
        completion = await core.respond(
            ToolRequest(
                system="You are a desktop agent. Use the tools.",
                messages=[Message("user", "What is in ~/Downloads?")],
                tools=TOOLS,
            )
        )

    assert [call.name for call in completion.tool_calls] == ["fs_list"]
    assert "Downloads" in str(completion.tool_calls[0].args.get("path"))
