import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from arcee.core.protocol import CoreAction
from arcee.models.base import BACKOFF_S, Message, ModelProfile, ModelRequest, provider_schema
from arcee.models.errors import ModelUnavailableError
from arcee.models.gemini import API_KEY_ENV, API_ROOT, GeminiAdapter
from tests.fakes import FakeClock

KEY = "test-key-not-real"
PROFILE = ModelProfile(
    model="gemma-4-26b-a4b-it",
    json_mode="schema",
    context_tokens=262_144,
    rpm=30,
    cost_tier="free",
    thinking=False,
)
ACTION = '{"action": {"tool": "fs.list", "args": {"path": "~/Downloads"}}}'

type Responder = Callable[[httpx.Request], httpx.Response]


def ok(text: str = ACTION, **usage: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "", "thought": True}, {"text": text}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 47, "candidatesTokenCount": 51, **usage},
        },
    )


def error(status: int, message: str = "boom", **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message, **extra}})


class Server:
    """Replays scripted responses and records the requests that reached it."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses: Iterator[httpx.Response | Exception] = iter(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def adapter(
    server: Server, profile: ModelProfile = PROFILE, clock: FakeClock | None = None
) -> GeminiAdapter:
    client = httpx.AsyncClient(base_url=API_ROOT, transport=httpx.MockTransport(server))
    return GeminiAdapter(profile, KEY, client=client, clock=clock or FakeClock())


def request(*messages: Message) -> ModelRequest[CoreAction]:
    return ModelRequest(
        system="You are Arcee.",
        messages=messages or (Message("user", "List my downloads"),),
        schema=CoreAction,
    )


class TestRequestShape:
    async def test_posts_to_model_endpoint_with_key_in_header(self) -> None:
        server = Server(ok())

        await adapter(server).complete(request())

        sent = server.requests[0]
        assert sent.url.path == "/v1beta/models/gemma-4-26b-a4b-it:generateContent"
        assert sent.headers["x-goog-api-key"] == KEY
        assert KEY not in str(sent.url)

    async def test_maps_system_and_roles(self) -> None:
        server = Server(ok())

        await adapter(server).complete(
            request(Message("user", "a"), Message("assistant", "b"), Message("user", "c"))
        )

        body = server.body()
        assert body["systemInstruction"] == {"parts": [{"text": "You are Arcee."}]}
        assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
        assert body["contents"][1]["parts"] == [{"text": "b"}]

    async def test_sends_flattened_schema_in_json_mode(self) -> None:
        server = Server(ok())

        await adapter(server).complete(request())

        config = server.body()["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseJsonSchema"] == provider_schema(CoreAction)

    async def test_omits_schema_without_json_mode(self) -> None:
        server = Server(ok())

        await adapter(server, PROFILE.model_copy(update={"json_mode": "none"})).complete(request())

        assert "responseJsonSchema" not in server.body()["generationConfig"]

    @pytest.mark.parametrize(
        ("thinking", "expected"),
        [(True, {"thinkingLevel": "high"}), (False, {"thinkingLevel": "minimal"}), (None, None)],
    )
    async def test_maps_thinking_flag_to_level(
        self, thinking: bool | None, expected: dict[str, str] | None
    ) -> None:
        server = Server(ok())

        await adapter(server, PROFILE.model_copy(update={"thinking": thinking})).complete(request())

        assert server.body()["generationConfig"].get("thinkingConfig") == expected


class TestReplies:
    async def test_skips_thought_parts_and_counts_thinking_tokens(self) -> None:
        response = await adapter(Server(ok(thoughtsTokenCount=200))).complete(request())

        assert response.raw == ACTION
        assert response.usage.input_tokens == 47
        assert response.usage.output_tokens == 51 + 200

    async def test_blocked_prompt_is_unavailable(self) -> None:
        blocked = httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

        with pytest.raises(ModelUnavailableError, match="SAFETY"):
            await adapter(Server(blocked)).complete(request())


class TestErrors:
    async def test_retries_server_errors(self) -> None:
        server = Server(error(500), error(503), ok())

        response = await adapter(server).complete(request())

        assert response.parsed.action.tool == "fs.list"
        assert len(server.requests) == 3

    async def test_retries_dropped_connections(self) -> None:
        server = Server(httpx.ConnectError("reset"), ok())

        await adapter(server).complete(request())

        assert len(server.requests) == 2

    async def test_rate_limit_uses_retry_info_delay(self) -> None:
        retry_info = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
        clock = FakeClock()

        await adapter(Server(error(429, details=[retry_info]), ok()), clock=clock).complete(
            request()
        )

        assert clock.sleeps == [7.0]

    async def test_rate_limit_prefers_retry_after_header(self) -> None:
        limited = httpx.Response(429, headers={"retry-after": "3"}, json={"error": {}})
        clock = FakeClock()

        await adapter(Server(limited, ok()), clock=clock).complete(request())

        assert clock.sleeps == [3.0]

    async def test_gives_up_after_backoff_schedule(self) -> None:
        server = Server(*[error(500)] * (len(BACKOFF_S) + 1))

        with pytest.raises(ModelUnavailableError, match="HTTP 500: boom"):
            await adapter(server).complete(request())

    @pytest.mark.parametrize("status", [400, 403, 404])
    async def test_client_errors_fail_without_retry(self, status: int) -> None:
        server = Server(error(status, "API key not valid"))

        with pytest.raises(ModelUnavailableError, match=f"HTTP {status}: API key not valid"):
            await adapter(server).complete(request())
        assert len(server.requests) == 1

    async def test_timeout_fails_without_retry(self) -> None:
        server = Server(httpx.ReadTimeout("slow"))

        with pytest.raises(ModelUnavailableError, match="timed out"):
            await adapter(server).complete(request())
        assert len(server.requests) == 1

    async def test_non_json_error_body_is_reported(self) -> None:
        server = Server(httpx.Response(502, text="<html>Bad Gateway</html>"), ok())

        await adapter(server).complete(request())

        assert len(server.requests) == 2


class TestLifecycle:
    async def test_health_reflects_model_endpoint(self) -> None:
        assert await adapter(Server(httpx.Response(200, json={"name": "m"}))).health()
        assert not await adapter(Server(error(403))).health()

    def test_from_env_requires_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(API_KEY_ENV, raising=False)

        with pytest.raises(ModelUnavailableError, match=API_KEY_ENV):
            GeminiAdapter.from_env(PROFILE)

    async def test_closes_only_its_own_client(self) -> None:
        shared = httpx.AsyncClient(transport=httpx.MockTransport(Server()))

        async with GeminiAdapter(PROFILE, KEY, client=shared):
            pass

        assert not shared.is_closed
        await shared.aclose()
