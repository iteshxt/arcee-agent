import json
from typing import Any

import httpx
import pytest

from arcee.core.protocol import CoreAction
from arcee.models.base import Message, ModelProfile, ModelRequest, ToolRequest, WireCall
from arcee.models.errors import ModelUnavailableError, QuotaExhaustedError
from arcee.models.openai_compat import OpenAICompatAdapter
from arcee.models.presets import cloud_profile, core_ladder
from tests.fakes import FakeClock

NATIVE = cloud_profile("google/gemma-4-31b-it")
TOOLS = [{"type": "function", "function": {"name": "fs_list", "parameters": {}}}]


def answer(
    content: str = "", calls: list[dict[str, Any]] | None = None, **extra: Any
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content, **extra}
    if calls is not None:
        message["tool_calls"] = calls
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 9},
        },
    )


class Server:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, i: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def adapter(server: Server, profile: ModelProfile = NATIVE) -> OpenAICompatAdapter:
    client = httpx.AsyncClient(
        base_url="https://or.test/api/v1", transport=httpx.MockTransport(server)
    )
    return OpenAICompatAdapter(profile, "key", client=client, clock=FakeClock())


async def test_native_turn_pairs_call_ids_and_parses_calls() -> None:
    server = Server(
        answer(
            calls=[
                {
                    "id": "x",
                    "type": "function",
                    "function": {"name": "fs_list", "arguments": '{"path": "~"}'},
                }
            ]
        )
    )
    history = (
        Message("user", "list"),
        Message("assistant", "", tool_calls=(WireCall("fs_list", {"path": "~"}),)),
        Message("tool", '{"count": 1}', tool_name="fs_list"),
    )

    completion = await adapter(server).respond(ToolRequest("sys", history, TOOLS))

    body = server.body()
    assert body["model"] == "google/gemma-4-31b-it"
    assert body["tools"] == TOOLS
    assert body["messages"][2]["tool_calls"][0] == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "fs_list", "arguments": '{"path": "~"}'},
    }
    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"count": 1}',
    }
    assert completion.tool_calls == (WireCall("fs_list", {"path": "~"}),)
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (50, 9)


async def test_schema_mode_sends_a_json_schema() -> None:
    server = Server(answer('{"action": {"tool": "fs.list", "args": {"path": "~"}}}'))
    profile = cloud_profile("m", schema=True)

    response = await adapter(server, profile).complete(
        ModelRequest(system="s", messages=[Message("user", "go")], schema=CoreAction)
    )

    assert server.body()["response_format"]["type"] == "json_schema"
    assert response.parsed.action.tool == "fs.list"


async def test_reasoning_follows_the_thinking_flag() -> None:
    server = Server(answer("hi"), answer("hi"))

    await adapter(server, NATIVE.model_copy(update={"thinking": False})).respond(
        ToolRequest("s", [Message("user", "hi")], [])
    )
    await adapter(server).respond(ToolRequest("s", [Message("user", "hi")], []))

    assert server.body(0)["reasoning"] == {"enabled": False, "exclude": True}
    assert "reasoning" not in server.body(1)


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (
            httpx.Response(401, json={"error": {"code": 401, "message": "No auth"}}),
            ModelUnavailableError,
            "No auth",
        ),
        (
            httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}}),
            QuotaExhaustedError,
            "credits",
        ),
    ],
)
async def test_client_errors(response: httpx.Response, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        await adapter(Server(response)).respond(ToolRequest("s", [Message("user", "hi")], []))


async def test_rate_limits_and_upstream_errors_are_retried() -> None:
    server = Server(
        httpx.Response(
            429, headers={"retry-after": "2"}, json={"error": {"code": 429, "message": "slow"}}
        ),
        httpx.Response(200, json={"error": {"code": 502, "message": "upstream died"}}),
        answer("ok"),
    )

    completion = await adapter(server).respond(ToolRequest("s", [Message("user", "hi")], []))

    assert completion.text == "ok"
    assert len(server.requests) == 3


def test_core_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert len(core_ladder("local")) == 3
    with pytest.raises(ModelUnavailableError, match="OPENROUTER_API_KEY"):
        core_ladder("openrouter:google/gemma-4-31b-it")
    with pytest.raises(ValueError, match="unknown core"):
        core_ladder("acme:model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    [model] = core_ladder("openrouter:google/gemma-4-31b-it")
    assert (model.profile.model, model.profile.rpm) == ("google/gemma-4-31b-it", 20)
