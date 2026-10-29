"""Google's Gemini API: the Gemini models and hosted Gemma 4 (default model: see decision 0002).

Plain REST over httpx rather than the google-genai SDK: one endpoint is all the
harness needs, and owning the request keeps error handling and retries in the
base adapter where every provider shares them.

Observed behaviour this adapter accounts for (Gemma 4, October 2026):
- Replies may carry an empty `thought: true` part before the answer; it is skipped.
- Transient HTTP 500s are common and succeed on retry, so 5xx is retryable.
- Latency for identical requests ranges from 2 s to over 60 s, hence the long
  read timeout. A timeout is not retried: another minute of waiting is worse
  than falling back.
"""

import os
from collections.abc import Mapping
from typing import Any, Final, Literal, Self
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from arcee.models.base import Completion, ModelAdapter, ModelProfile, RawRequest, TokenUsage
from arcee.models.errors import (
    ModelUnavailableError,
    RateLimitedError,
    ServerBusyError,
)
from arcee.models.limiter import SYSTEM_CLOCK, Clock

API_ROOT: Final = "https://generativelanguage.googleapis.com/v1beta"
API_KEY_ENV: Final = "GEMINI_API_KEY"
TIMEOUT: Final = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

_RETRYABLE_STATUS: Final = frozenset({500, 502, 503, 504})
_ROLES: Final[Mapping[str, str]] = {"user": "user", "assistant": "model"}


class GeminiAdapter(ModelAdapter):
    # Free-tier daily quotas reset at midnight Pacific time.
    quota_tz = ZoneInfo("America/Los_Angeles")

    def __init__(
        self,
        profile: ModelProfile,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        super().__init__(profile, clock=clock)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=API_ROOT, timeout=TIMEOUT)
        # The key travels in a header, never the URL, so it stays out of request logs.
        self._headers = {"x-goog-api-key": api_key}
        self._model_path = f"/models/{profile.model}"

    @classmethod
    def from_env(cls, profile: ModelProfile, *, clock: Clock = SYSTEM_CLOCK) -> Self:
        api_key = os.environ.get(API_KEY_ENV, "")
        if not api_key:
            raise ModelUnavailableError(f"{API_KEY_ENV} is not set")
        return cls(profile, api_key, clock=clock)

    async def _send(self, request: RawRequest) -> Completion:
        response = await self._request("POST", ":generateContent", self._body(request))
        try:
            reply = _GenerateResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise ModelUnavailableError(f"{self.profile.model}: unexpected response shape") from exc
        return reply.completion()

    async def health(self) -> bool:
        try:
            await self._request("GET", "")
        except ModelUnavailableError, ServerBusyError, RateLimitedError:
            return False
        return True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _body(self, request: RawRequest) -> dict[str, Any]:
        config: dict[str, Any] = {
            "maxOutputTokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseJsonSchema"] = request.schema
        if self.profile.thinking is not None:
            # Gemma 4 and Gemini 3 take a level; Gemma only knows on ("high") and off ("minimal").
            config["thinkingConfig"] = {
                "thinkingLevel": "high" if self.profile.thinking else "minimal"
            }
        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [
                {"role": _ROLES[message.role], "parts": [{"text": message.text}]}
                for message in request.messages
            ],
            "generationConfig": config,
        }

    async def _request(
        self, method: Literal["GET", "POST"], suffix: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        model = self.profile.model
        try:
            response = await self._client.request(
                method, self._model_path + suffix, json=body, headers=self._headers
            )
        except httpx.TimeoutException as exc:
            raise ModelUnavailableError(f"{model}: timed out") from exc
        except httpx.TransportError as exc:
            raise ServerBusyError(f"{model}: {type(exc).__name__}") from exc

        if response.is_success:
            return response
        error = _ErrorBody.parse(response)
        if response.status_code == 429:
            raise RateLimitedError(error.retry_after_s(response))
        message = f"{model}: HTTP {response.status_code}: {error.message}"
        if response.status_code in _RETRYABLE_STATUS:
            raise ServerBusyError(message)
        raise ModelUnavailableError(message)


# --- Response payloads (only the fields the harness reads) -----------------------


class _Payload(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="ignore", frozen=True)


class _Part(_Payload):
    text: str = ""
    thought: bool = False


class _Content(_Payload):
    parts: tuple[_Part, ...] = ()


class _Candidate(_Payload):
    content: _Content = _Content()
    finish_reason: str | None = None


class _Usage(_Payload):
    prompt_token_count: int = 0
    candidates_token_count: int = 0
    thoughts_token_count: int = 0


class _PromptFeedback(_Payload):
    block_reason: str | None = None


class _GenerateResponse(_Payload):
    candidates: tuple[_Candidate, ...] = ()
    usage_metadata: _Usage = _Usage()
    prompt_feedback: _PromptFeedback = _PromptFeedback()

    def completion(self) -> Completion:
        if not self.candidates:
            reason = self.prompt_feedback.block_reason or "no candidates"
            raise ModelUnavailableError(f"reply withheld: {reason}")
        parts = self.candidates[0].content.parts
        usage = self.usage_metadata
        return Completion(
            "".join(part.text for part in parts if not part.thought),
            TokenUsage(
                usage.prompt_token_count,
                usage.candidates_token_count + usage.thoughts_token_count,
            ),
        )


class _ErrorDetail(_Payload):
    type: str = Field(default="", alias="@type")
    retry_delay: str | None = None


class _Error(_Payload):
    message: str = ""
    details: tuple[_ErrorDetail, ...] = ()


class _ErrorBody(_Payload):
    error: _Error = _Error()

    @property
    def message(self) -> str:
        return self.error.message or "no error message"

    @classmethod
    def parse(cls, response: httpx.Response) -> _ErrorBody:
        try:
            return cls.model_validate_json(response.content)
        except ValidationError:
            return cls(error=_Error(message=response.text[:200]))

    def retry_after_s(self, response: httpx.Response) -> float | None:
        """Seconds to wait, from the Retry-After header or a google.rpc.RetryInfo detail."""
        delays = [response.headers.get("retry-after")] + [
            detail.retry_delay
            for detail in self.error.details
            if detail.type.endswith("google.rpc.RetryInfo")
        ]
        for delay in filter(None, delays):
            try:
                return float(delay.removesuffix("s"))
            except ValueError:
                continue
        return None
