import json
from typing import Any

import httpx
import pytest

from arcee.core.protocol import CoreAction
from arcee.models.base import (
    Message,
    ModelProfile,
    ModelRequest,
    ToolRequest,
    WireCall,
    provider_schema,
)
from arcee.models.errors import MalformedReplyError, ModelUnavailableError
from arcee.models.ollama import DEFAULT_HOST, OllamaAdapter
from tests.fakes import FakeClock

NATIVE = ModelProfile(
    model="lfm3",
    context_tokens=8192,
    cost_tier="local",
    call_mode="native",
    thinking=False,
)
SCHEMA = NATIVE.model_copy(update={"call_mode": "schema", "json_mode": "schema"})
TOOLS = [{"type": "function", "function": {"name": "fs_list", "parameters": {}}}]
ACTION = '{"action": {"tool": "fs.list", "args": {"path": "~"}}}'


def reply(
    content: str = "", *, tool_calls: list[dict[str, Any]] | None = None, **extra: Any
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content, **extra}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200, json={"message": message, "prompt_eval_count": 120, "eval_count": 30, "done": True}
    )


class Server:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def adapter(server: Server, profile: ModelProfile = NATIVE, **kwargs: Any) -> OllamaAdapter:
    client = httpx.AsyncClient(base_url=DEFAULT_HOST, transport=httpx.MockTransport(server))
    return OllamaAdapter(profile, client=client, clock=FakeClock(), **kwargs)


def tool_request(*messages: Message) -> ToolRequest:
    return ToolRequest(
        system="You are Arcee.",
        messages=messages or (Message("user", "list my home"),),
        tools=TOOLS,
    )


class TestNative:
    async def test_sends_tools_think_and_options(self) -> None:
        server = Server(reply("hi"))

        await adapter(server, options={"num_gpu": 999, "num_ctx": 8192}).respond(tool_request())

        body = server.body()
        assert server.requests[0].url.path == "/api/chat"
        assert body["model"] == "lfm3"
        assert body["stream"] is False
        assert body["think"] is False
        assert body["tools"] == TOOLS
        assert "format" not in body
        assert body["options"] == {
            "num_gpu": 999,
            "num_ctx": 8192,
            "temperature": 0.1,
            "num_predict": 1024,
        }
        assert body["messages"][0] == {"role": "system", "content": "You are Arcee."}

    async def test_replays_tool_turns(self) -> None:
        server = Server(reply("done"))
        call = WireCall("fs_list", {"path": "~"})

        await adapter(server).respond(
            tool_request(
                Message("user", "list my home"),
                Message("assistant", "", tool_calls=(call,)),
                Message("tool", '{"count": 2}', tool_name="fs_list"),
            )
        )

        assert server.body()["messages"][1:] == [
            {"role": "user", "content": "list my home"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "fs_list", "arguments": {"path": "~"}}}],
            },
            {"role": "tool", "content": '{"count": 2}', "tool_name": "fs_list"},
        ]

    async def test_returns_tool_calls_thinking_and_usage(self) -> None:
        calls = [{"function": {"name": "fs_list", "arguments": {"path": "~"}}}]
        server = Server(reply(tool_calls=calls, thinking="look first"))

        completion = await adapter(server).respond(tool_request())

        assert completion.tool_calls == (WireCall("fs_list", {"path": "~"}),)
        assert completion.thinking == "look first"
        assert (completion.usage.input_tokens, completion.usage.output_tokens) == (120, 30)

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [('{"path": "~"}', {"path": "~"}), ("not json", {"_unparsed": "not json"}), (None, {})],
    )
    async def test_normalises_argument_shapes(
        self, arguments: Any, expected: dict[str, Any]
    ) -> None:
        server = Server(
            reply(tool_calls=[{"function": {"name": "fs_list", "arguments": arguments}}])
        )

        completion = await adapter(server).respond(tool_request())

        assert completion.tool_calls[0].args == expected

    async def test_omits_think_when_profile_keeps_default(self) -> None:
        server = Server(reply("hi"))

        await adapter(server, NATIVE.model_copy(update={"thinking": None})).respond(tool_request())

        assert "think" not in server.body()

    async def test_refuses_schema_profiles(self) -> None:
        with pytest.raises(ValueError, match="schema call mode"):
            await adapter(Server(), SCHEMA).respond(tool_request())


class TestSchemaMode:
    async def test_sends_format_and_parses_reply(self) -> None:
        server = Server(reply(ACTION))

        response = await adapter(server, SCHEMA).complete(
            ModelRequest(system="s", messages=[Message("user", "go")], schema=CoreAction)
        )

        assert server.body()["format"] == provider_schema(CoreAction)
        assert "tools" not in server.body()
        assert response.parsed.action.tool == "fs.list"


class TestErrors:
    async def test_not_running_is_unavailable(self) -> None:
        server = Server(httpx.ConnectError("refused"))

        with pytest.raises(ModelUnavailableError, match="is the service running"):
            await adapter(server).respond(tool_request())

    async def test_missing_model_suggests_pull(self) -> None:
        server = Server(httpx.Response(404, json={"error": "model 'lfm3' not found"}))

        with pytest.raises(ModelUnavailableError, match="ollama pull lfm3"):
            await adapter(server).respond(tool_request())

    async def test_server_errors_are_retried(self) -> None:
        server = Server(httpx.Response(500, json={"error": "busy"}), reply("ok"))

        completion = await adapter(server).respond(tool_request())

        assert completion.text == "ok"
        assert len(server.requests) == 2

    async def test_parser_failures_are_malformed_replies_not_retried(self) -> None:
        error = {"error": "XML syntax error on line 5: unexpected EOF"}
        server = Server(httpx.Response(500, json=error))

        with pytest.raises(MalformedReplyError, match="unparseable reply: XML syntax error"):
            await adapter(server).respond(tool_request())
        assert len(server.requests) == 1

    async def test_timeout_is_unavailable(self) -> None:
        with pytest.raises(ModelUnavailableError, match="timed out"):
            await adapter(Server(httpx.ReadTimeout("slow"))).respond(tool_request())


class TestHealth:
    @pytest.mark.parametrize(
        ("listed", "healthy"),
        [("lfm3:latest", True), ("lfm3", True), ("qwen3.5:2b", False)],
    )
    async def test_requires_the_model_to_be_pulled(self, listed: str, healthy: bool) -> None:
        server = Server(httpx.Response(200, json={"models": [{"name": listed}]}))

        assert await adapter(server).health() is healthy

    async def test_unreachable_is_unhealthy(self) -> None:
        assert not await adapter(Server(httpx.ConnectError("refused"))).health()

    async def test_unload_sets_keep_alive_to_zero(self) -> None:
        server = Server(httpx.Response(200, json={"done": True}))

        await adapter(server).unload()

        assert server.requests[0].url.path == "/api/generate"
        assert server.body() == {"model": "lfm3", "keep_alive": 0}

    async def test_unload_failures_are_ignored(self) -> None:
        await adapter(Server(httpx.ConnectError("refused"))).unload()

    async def test_closes_only_its_own_client(self) -> None:
        shared = httpx.AsyncClient(transport=httpx.MockTransport(Server()))

        async with OllamaAdapter(NATIVE, client=shared):
            pass

        assert not shared.is_closed
        await shared.aclose()


async def test_a_cut_off_turn_is_asked_for_again_with_more_room() -> None:
    cut = httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": "The three newest emails are"},
            "done": True,
            "done_reason": "length",
        },
    )
    server = Server(cut, reply("The three newest emails are A, B and C."))

    completion = await adapter(server).respond(tool_request())

    assert completion.text.endswith("A, B and C.")
    assert [server.body(i)["options"]["num_predict"] for i in range(2)] == [1024, 2048]
