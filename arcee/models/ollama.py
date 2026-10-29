"""Ollama's local `/api/chat` (spec section 5): the default provider.

Native call mode passes tool definitions in `tools` and lets Ollama's per-model
parser turn the model's own tool-call format (LFM3's XML, Qwen's JSON) into
structured calls. Schema mode passes the JSON schema as `format`, which Ollama
enforces with a grammar.

Two settings matter on a 4 GB GPU shared with the desktop: `num_gpu` (Ollama
otherwise splits a model that fits across CPU and GPU and runs 2-3x slower) and
`think`, which the escalation ladder turns on only when the cheap tier fails.
"""

import contextlib
import json
import re
from collections.abc import Mapping
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
from arcee.models.errors import MalformedReplyError, ModelUnavailableError, ServerBusyError
from arcee.models.limiter import SYSTEM_CLOCK, Clock

DEFAULT_HOST: Final = "http://127.0.0.1:11434"
# Loading a model from disk can take half a minute; a long answer on CPU, longer.
TIMEOUT: Final = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
# Every layer on the GPU. Only for models that fit: an 8B model on 4 GB must leave
# the split to Ollama.
ALL_LAYERS: Final = 999
# Ollama answers 500 when its per-model parser rejects the model's own output
# (seen: LFM3 writing a truncated <tool_call> XML block).
_PARSE_FAILURE: Final = re.compile(r"(?i)\b(?:xml|json) syntax error|failed to parse|error parsing")


class OllamaAdapter(ModelAdapter):
    def __init__(
        self,
        profile: ModelProfile,
        *,
        host: str = DEFAULT_HOST,
        options: Mapping[str, Any] | None = None,
        keep_alive: str = "30m",
        client: httpx.AsyncClient | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """`options` are Ollama model options (`num_gpu`, `num_ctx`, sampling).

        Sampling defaults come from the model's Modelfile, which for LFM matches
        Liquid's recommendation, so presets set only what differs.
        """
        super().__init__(profile, clock=clock)
        self._options = dict(options or {})
        self._keep_alive = keep_alive
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=host, timeout=TIMEOUT)

    async def _send(self, request: RawRequest) -> Completion:
        body: dict[str, Any] = {
            "model": self.profile.model,
            "stream": False,
            "keep_alive": self._keep_alive,
            "messages": [
                {"role": "system", "content": request.system},
                *map(_message, request.messages),
            ],
            "options": {
                **self._options,
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }
        if self.profile.thinking is not None:
            body["think"] = self.profile.thinking
        if request.tools:
            body["tools"] = list(request.tools)
        elif request.schema is not None:
            body["format"] = request.schema
        return _completion(await self._post("/api/chat", body))

    async def health(self) -> bool:
        """True when Ollama answers and has this profile's model pulled."""
        try:
            response = await self._client.get("/api/tags")
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        models = cast(list[dict[str, Any]], response.json().get("models", []))
        return any(_same_model(m.get("name", ""), self.profile.model) for m in models)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=body)
        except httpx.ConnectError as exc:
            raise ModelUnavailableError(
                f"Ollama is not reachable at {self._client.base_url}; is the service running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ModelUnavailableError(f"{self.profile.model}: timed out") from exc
        except httpx.TransportError as exc:
            raise ServerBusyError(f"connection dropped: {exc}") from exc
        if response.status_code == 200:
            return cast(dict[str, Any], response.json())
        error = _error_text(response)
        if response.status_code == 404:
            raise ModelUnavailableError(f"{error}; pull it with `ollama pull {self.profile.model}`")
        if response.status_code >= 500 and _PARSE_FAILURE.search(error):
            raise MalformedReplyError(f"{self.profile.model} wrote an unparseable reply: {error}")
        if response.status_code >= 500:
            raise ServerBusyError(f"HTTP {response.status_code}: {error}")
        raise ModelUnavailableError(f"HTTP {response.status_code}: {error}")


def _message(message: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": message.role, "content": message.text}
    if message.tool_calls:
        out["tool_calls"] = [
            {"function": {"name": call.name, "arguments": dict(call.args)}}
            for call in message.tool_calls
        ]
    if message.tool_name:
        out["tool_name"] = message.tool_name
    return out


def _completion(data: dict[str, Any]) -> Completion:
    message = cast(dict[str, Any], data.get("message") or {})
    calls = cast(list[dict[str, Any]], message.get("tool_calls") or [])
    return Completion(
        text=str(message.get("content") or ""),
        usage=TokenUsage(int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0))),
        tool_calls=tuple(_wire_call(call) for call in calls),
        thinking=str(message.get("thinking") or ""),
    )


def _wire_call(call: dict[str, Any]) -> WireCall:
    function = cast(dict[str, Any], call.get("function") or {})
    args: Any = function.get("arguments") or {}
    # Some parsers hand back arguments as a JSON string; the registry needs a mapping.
    if isinstance(args, str):
        with contextlib.suppress(ValueError):
            args = json.loads(args)
    if not isinstance(args, dict):
        args = {"_unparsed": str(args)}
    return WireCall(str(function.get("name", "")), cast(dict[str, Any], args))


def _error_text(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error") or response.text)
    except ValueError:
        return response.text[:200] or response.reason_phrase


def _same_model(listed: str, wanted: str) -> bool:
    """Ollama lists `lfm3:latest` for a model pulled as `lfm3`."""
    return listed == wanted or (":" not in wanted and listed == f"{wanted}:latest")
