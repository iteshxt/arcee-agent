"""OpenAI-compatible chat completions (spec section 5, `openai_compat`): OpenRouter and the like.

Native call mode sends `tools` and gets `tool_calls` back with JSON-string
arguments; earlier calls are replayed with ids paired to their `tool` results.
Schema mode sends `response_format: json_schema`. `reasoning` follows the
profile's thinking flag (OpenRouter's unified field; other servers ignore it).
"""

import json
import os
from collections.abc import Sequence
from typing import Any, Final, cast

import httpx

from arcee.models.base import (
    Completion,
    Message,
    ModelAdapter,
    ModelProfile,
    RawRequest,
    TokenUsage,
    WireCall,
)
from arcee.models.errors import (
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitedError,
    ServerBusyError,
)
from arcee.models.limiter import SYSTEM_CLOCK, Clock

OPENROUTER_URL: Final = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV: Final = "OPENROUTER_API_KEY"
# Providers that speak this API: name -> (base URL, environment variable with the key).
PROVIDERS: Final = {
    "openrouter": (OPENROUTER_URL, OPENROUTER_KEY_ENV),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
}
TIMEOUT: Final = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


class OpenAICompatAdapter(ModelAdapter):
    def __init__(
        self,
        profile: ModelProfile,
        api_key: str,
        *,
        base_url: str = OPENROUTER_URL,
        client: httpx.AsyncClient | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        super().__init__(profile, clock=clock)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "Arcee"},
        )

    @classmethod
    def openrouter(cls, profile: ModelProfile) -> OpenAICompatAdapter:
        return cls.provider("openrouter", profile)

    @classmethod
    def provider(cls, name: str, profile: ModelProfile) -> OpenAICompatAdapter:
        """An adapter for a known provider, with its key from the environment (or .env)."""
        if name not in PROVIDERS:
            raise ValueError(f"unknown provider {name!r}; known: {', '.join(PROVIDERS)}")
        url, env = PROVIDERS[name]
        key = os.environ.get(env)
        if not key:
            raise ModelUnavailableError(f"set {env} (in .env) to use {name}")
        return cls(profile, key, base_url=url)

    async def _send(self, request: RawRequest) -> Completion:
        body: dict[str, Any] = {
            "model": self.profile.model,
            "messages": [
                {"role": "system", "content": request.system},
                *_messages(request.messages),
            ],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if self.profile.thinking is not None:
            if "groq.com" in str(self._client.base_url):
                # Groq's gpt-oss models take an effort level instead of OpenRouter's field.
                body["reasoning_effort"] = "medium" if self.profile.thinking else "low"
            else:
                body["reasoning"] = {
                    "enabled": self.profile.thinking,
                    "exclude": not self.profile.thinking,
                }
        if request.tools:
            body["tools"] = list(request.tools)
        elif request.schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "strict": False, "schema": request.schema},
            }
        return _completion(await self._post("/chat/completions", body))

    async def health(self) -> bool:
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def limits(self) -> dict[str, Any]:
        """The key's own report of its credits and rate limits (OpenRouter's /key)."""
        response = await self._client.get("/key")
        response.raise_for_status()
        return cast(dict[str, Any], response.json().get("data", {}))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise ModelUnavailableError(f"{self.profile.model}: timed out") from exc
        except httpx.TransportError as exc:
            raise ServerBusyError(f"connection failed: {exc}") from exc
        data = _json(response)
        # OpenRouter can answer 200 with an error object when the upstream fails.
        error = cast(dict[str, Any], data.get("error") or {})
        status = int(error.get("code") or response.status_code) if error else response.status_code
        if status == 200 and data.get("choices"):
            return data
        message = str(error.get("message") or response.text[:200] or f"HTTP {status}")
        if status == 429:
            raise RateLimitedError(_retry_after(response))
        if status in (402, 403):
            raise QuotaExhaustedError(f"{self.profile.model}: {message}")
        if status >= 500 or status == 200:
            raise ServerBusyError(f"HTTP {status}: {message}")
        raise ModelUnavailableError(f"HTTP {status}: {message}")


def _messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Map turns, pairing each tool result with the id of the call it answers."""
    out: list[dict[str, Any]] = []
    waiting: list[str] = []
    counter = 0
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            calls: list[dict[str, Any]] = []
            for call in message.tool_calls:
                counter += 1
                call_id = f"call_{counter}"
                waiting.append(call_id)
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(dict(call.args))},
                    }
                )
            out.append({"role": "assistant", "content": message.text or None, "tool_calls": calls})
        elif message.role == "tool":
            call_id = waiting.pop(0) if waiting else f"call_{counter}"
            out.append({"role": "tool", "tool_call_id": call_id, "content": message.text})
        else:
            out.append({"role": message.role, "content": message.text})
    return out


def _completion(data: dict[str, Any]) -> Completion:
    choice = cast(dict[str, Any], data["choices"][0])
    message = cast(dict[str, Any], choice.get("message") or {})
    calls = cast(list[dict[str, Any]], message.get("tool_calls") or [])
    usage = cast(dict[str, Any], data.get("usage") or {})
    return Completion(
        text=str(message.get("content") or ""),
        usage=TokenUsage(
            int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        ),
        tool_calls=tuple(_wire_call(call) for call in calls),
        thinking=str(message.get("reasoning") or ""),
    )


def _wire_call(call: dict[str, Any]) -> WireCall:
    function = cast(dict[str, Any], call.get("function") or {})
    raw: Any = function.get("arguments") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        args = {"_unparsed": str(raw)}
    if not isinstance(args, dict):
        args = {"_unparsed": str(args)}
    return WireCall(str(function.get("name", "")), cast(dict[str, Any], args))


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None
