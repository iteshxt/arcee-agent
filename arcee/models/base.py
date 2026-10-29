"""The interface every model provider implements (spec section 5).

Providers only implement one raw round-trip, `_send`. The shared `complete` loop
on top of it owns the protocol: schema passthrough, parsing, validation and the
retry turn, plus rate limiting and backoff on 429 and 5xx. That keeps every provider
behaving identically, and keeps the first-try success rate
(`ModelResponse.attempts == 1`) comparable across them.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import tzinfo
from functools import cache
from types import TracebackType
from typing import Any, ClassVar, Final, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field

from arcee.models.errors import (
    ModelOutputError,
    ModelUnavailableError,
    RateLimitedError,
    RetryableError,
)
from arcee.models.jsonfix import ReplyError, parse_reply, retry_prompt
from arcee.models.limiter import SYSTEM_CLOCK, Clock, RateLimiter

MAX_RETRIES: Final = 2
# Waits between attempts after HTTP 429 or 5xx; one more failure gives up.
BACKOFF_S: Final = (1.0, 2.0, 4.0, 8.0)
# A provider asking for a longer wait than this is treated as unavailable.
MAX_RETRY_AFTER_S: Final = 60.0
# Total time spent waiting on rate limits the provider timed, before giving up.
MAX_RATE_LIMIT_WAIT_S: Final = 120.0


class ModelProfile(BaseModel):
    """Capability flags for one configured model (spec section 4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(description="Provider model id, e.g. 'gemini-2.5-flash'.")
    vision: bool = False
    grounding: Literal["none", "marks", "coords"] = "none"
    json_mode: Literal["none", "schema", "grammar"] = "none"
    call_mode: Literal["native", "schema"] = Field(
        default="schema",
        description="How actions travel: provider tool calls, or one schema-checked JSON object.",
    )
    context_tokens: int = Field(ge=2048)
    rpm: int | None = Field(default=None, ge=1)
    rpd: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=100, description="Tokens per minute to stay under.")
    tool_detail: Literal["full", "compact"] = Field(
        default="full",
        description="full: tools carry usage cards (small models need them); compact: one line.",
    )
    cost_tier: Literal["free", "paid", "local"]
    thinking: bool | None = Field(
        default=None, description="Reasoning before answering; None keeps the provider default."
    )
    max_image_px: int = Field(default=1280, ge=256)


@dataclass(frozen=True, slots=True)
class WireCall:
    """A tool call as the provider sees it: wire name and raw arguments, unvalidated."""

    name: str
    args: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation turn.

    Native call mode adds two shapes: an assistant turn carrying `tool_calls`, and a
    `tool` turn carrying a call's result, named by `tool_name`.
    """

    role: Literal["user", "assistant", "tool"]
    text: str
    tool_calls: tuple[WireCall, ...] = ()
    tool_name: str = ""


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True, slots=True)
class ModelRequest[M: BaseModel]:
    system: str
    messages: Sequence[Message]
    schema: type[M]
    max_tokens: int = 1024
    temperature: float = 0.1


@dataclass(frozen=True, slots=True)
class ModelResponse[M: BaseModel]:
    parsed: M
    raw: str
    usage: TokenUsage
    attempts: int


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """One native tool-calling turn: the model may call one of `tools` or reply in text."""

    system: str
    messages: Sequence[Message]
    tools: Sequence[Mapping[str, Any]]
    max_tokens: int = 1024
    temperature: float = 0.1


@dataclass(frozen=True, slots=True)
class RawRequest:
    """One provider round-trip.

    `schema` is None when the profile has no JSON mode; `tools` is non-empty only
    for native tool-calling turns.
    """

    system: str
    messages: Sequence[Message]
    schema: Mapping[str, Any] | None
    max_tokens: int
    temperature: float
    tools: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool_calls: tuple[WireCall, ...] = ()
    thinking: str = ""


class ModelAdapter(ABC):
    # Where the provider's daily quota resets; None means the local timezone.
    quota_tz: ClassVar[tzinfo | None] = None

    def __init__(self, profile: ModelProfile, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.profile = profile
        self._clock = clock
        # Seconds the last call spent waiting on rate budgets and backoff, for the UI.
        self.last_wait_s = 0.0
        self.limiter = RateLimiter(
            profile.rpm, profile.rpd, tpm=profile.tpm, quota_tz=self.quota_tz, clock=clock
        )

    @abstractmethod
    async def _send(self, request: RawRequest) -> Completion:
        """Run one round-trip.

        Raise `RateLimitedError` on HTTP 429, `ServerBusyError` on 5xx or a dropped
        connection, and `ModelUnavailableError` on failures that repeating cannot fix.
        """

    @abstractmethod
    async def health(self) -> bool:
        """Cheap reachability check for the Models page and fallback decisions."""

    async def unload(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Free the model's memory now; the next request loads it again. Best effort."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release connections. Adapters without any keep the default."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def complete[M: BaseModel](self, request: ModelRequest[M]) -> ModelResponse[M]:
        """Return one validated `request.schema` object, retrying with feedback on bad output."""
        schema = None if self.profile.json_mode == "none" else provider_schema(request.schema)
        messages = list(request.messages)
        usage = TokenUsage()
        attempts = 0
        while True:
            attempts += 1
            reply = await self._send_with_backoff(
                RawRequest(
                    system=request.system,
                    messages=tuple(messages),
                    schema=schema,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                )
            )
            usage += reply.usage
            try:
                parsed = parse_reply(reply.text, request.schema)
            except ReplyError as error:
                if attempts > MAX_RETRIES:
                    raise ModelOutputError(error, reply.text, attempts, usage) from error
                messages += (Message("assistant", reply.text), Message("user", retry_prompt(error)))
                continue
            return ModelResponse(parsed, reply.text, usage, attempts)

    async def respond(self, request: ToolRequest) -> Completion:
        """Run one native tool-calling turn and return the raw reply.

        The calls are not validated here: the caller owns the tool registry, and in
        native mode a bad call is answered with a `tool` error message rather than
        a separate retry turn.
        """
        if self.profile.call_mode != "native":
            raise ValueError(f"{self.profile.model} is configured for schema call mode")
        return await self._send_with_backoff(
            RawRequest(
                system=request.system,
                messages=tuple(request.messages),
                schema=None,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                tools=tuple(request.tools),
            )
        )

    async def _send_with_backoff(self, request: RawRequest) -> Completion:
        backoff = iter(BACKOFF_S)
        waited = 0.0
        estimate = estimate_tokens(request)
        started = self._clock.monotonic()
        self.last_wait_s = 0.0
        while True:
            entry = await self.limiter.acquire(estimate)
            self.last_wait_s = self._clock.monotonic() - started
            try:
                completion = await self._send(request)
                used = completion.usage.input_tokens + completion.usage.output_tokens
                self.limiter.settle(entry, used)
                return completion
            except RetryableError as exc:
                delay = next(backoff, None)
                wait = max(delay or 0.0, exc.retry_after_s or 0.0)
                # A provider that says how long to wait (per-minute token limits do) is
                # waited for up to MAX_RATE_LIMIT_WAIT_S in total, past the usual schedule.
                told = isinstance(exc, RateLimitedError) and exc.retry_after_s is not None
                within = waited + wait <= MAX_RATE_LIMIT_WAIT_S
                gave_up = not within if told else delay is None
                if wait > MAX_RETRY_AFTER_S or gave_up:
                    raise ModelUnavailableError(f"{self.profile.model}: {exc}") from exc
                waited += wait
                await self._clock.sleep(wait)


# Measured on gpt-oss: about 5 characters per token for these prompts; 4.5 errs high.
CHARS_PER_TOKEN: Final = 4.5
# Replies are usually short; a full max_tokens reservation would starve the budget.
EXPECTED_OUTPUT_TOKENS: Final = 400


def estimate_input_tokens(request: RawRequest) -> int:
    """A cautious size for what `request` sends."""
    chars = len(request.system) + sum(len(m.text) + 20 for m in request.messages)
    chars += sum(len(json.dumps(dict(c.args))) for m in request.messages for c in m.tool_calls)
    chars += len(json.dumps(list(request.tools))) if request.tools else 0
    chars += len(json.dumps(dict(request.schema))) if request.schema else 0
    return int(chars / CHARS_PER_TOKEN)


def estimate_tokens(request: RawRequest) -> int:
    """Input plus a typical reply, for budgets that know nothing better."""
    return estimate_input_tokens(request) + min(request.max_tokens, EXPECTED_OUTPUT_TOKENS)


# Keywords whose values are data rather than subschemas; copied without rewriting.
_DATA_KEYWORDS: Final = frozenset({"const", "default", "enum", "examples"})
# Keywords whose values map names to subschemas; the names are not keywords.
_SCHEMA_MAPS: Final = frozenset({"properties", "patternProperties"})


@cache
def provider_schema(model: type[BaseModel]) -> Mapping[str, Any]:
    """`model`'s JSON Schema with `$ref`s inlined and titles dropped.

    Gemini's `responseSchema` and several OpenAI-compatible servers reject `$defs`,
    and titles only cost tokens. Cached per model class; treat the result as
    read-only.
    """
    schema = model.model_json_schema()
    # The class docstring is for readers of the code; sent as the schema's own
    # description it invites models to echo the schema instead of filling it in.
    schema.pop("description", None)
    return _inline(schema, schema.pop("$defs", {}), ())


def _inline(node: Any, defs: dict[str, Any], resolving: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(item, defs, resolving) for item in cast(list[Any], node)]
    if not isinstance(node, dict):
        return node

    schema = cast(dict[str, Any], node)
    if (ref := schema.get("$ref")) is not None:
        name = str(ref).rsplit("/", 1)[-1]
        if name in resolving:
            raise ValueError(f"recursive schema through {name!r} cannot be inlined")
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        return _inline(defs[name] | siblings, defs, (*resolving, name))

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key in _DATA_KEYWORDS:
            out[key] = value
        elif key in _SCHEMA_MAPS:
            out[key] = {name: _inline(sub, defs, resolving) for name, sub in value.items()}
        else:
            out[key] = _inline(value, defs, resolving)
    return out
