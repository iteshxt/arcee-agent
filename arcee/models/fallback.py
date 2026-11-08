"""A cloud model with the local one behind it.

Groq's free tier gives each model 200,000 tokens a day. When every Groq model at a
rank has spent its day, or Groq cannot be reached, the call goes to the local model
in that rank's place (Ollama, on this computer) instead of failing the request. Each
call tries the cloud first again: an exhausted Groq rotation fails at once, without a
request, until its allowance frees up, and then calls go back to it by themselves.

While the local model answers, the profile names it but keeps the cloud's cost tier
and label: the next call may go back to the cloud, so personal data is asked about
as if it went there.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from pydantic import BaseModel

from arcee.models.base import (
    Completion,
    ModelAdapter,
    ModelRequest,
    ModelResponse,
    RawRequest,
    ToolRequest,
)
from arcee.models.errors import ModelUnavailableError

log = logging.getLogger(__name__)

# After the cloud fails, calls go straight to this computer this long before the cloud
# is tried again: an outage or a bad key would otherwise cost a refused request a turn.
COOLDOWN_S: Final = 60.0


class FallbackAdapter(ModelAdapter):
    def __init__(
        self,
        primary: ModelAdapter,
        fallback: ModelAdapter,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(primary.profile)
        self.primary = primary
        self.fallback = fallback
        self.limiter = primary.limiter
        self.label = primary.label
        self.fell_back = ""  # why the last call went local; "" when the cloud answered
        self._clock = clock
        self._cloud_after = 0.0

    def expected_wait(self, request: ToolRequest | ModelRequest[Any]) -> float:
        return min(self.primary.expected_wait(request), self.fallback.expected_wait(request))

    async def respond(self, request: ToolRequest) -> Completion:
        async def call(model: ModelAdapter) -> Completion:
            return await model.respond(request)

        return await self._either(call)

    async def complete[M: BaseModel](self, request: ModelRequest[M]) -> ModelResponse[M]:
        async def call(model: ModelAdapter) -> ModelResponse[M]:
            return await model.complete(request)

        return await self._either(call)

    async def _either[T](self, call: Callable[[ModelAdapter], Awaitable[T]]) -> T:
        try:
            if self._clock() < self._cloud_after:
                raise ModelUnavailableError(self.fell_back)
            result = await call(self.primary)
        except ModelUnavailableError as exc:
            if not self.fell_back:
                log.warning("%s; using %s on this computer", exc, self.fallback.profile.model)
            self.fell_back = str(exc)
            self._cloud_after = max(self._cloud_after, self._clock() + COOLDOWN_S)
        else:
            self.profile, self.fell_back = self.primary.profile, ""
            self.last_wait_s = self.primary.last_wait_s
            return result
        # The local model's name, for events; the cloud's cost tier and label, so the
        # personal-data question never treats a call that may go back to the cloud as
        # local.
        self.profile = self.fallback.profile.model_copy(
            update={"cost_tier": self.primary.profile.cost_tier}
        )
        try:
            return await call(self.fallback)
        finally:
            self.last_wait_s = self.fallback.last_wait_s

    async def _send(self, request: RawRequest) -> Completion:  # pragma: no cover
        raise NotImplementedError("a fallback sends through its models")

    async def health(self) -> bool:
        return await self.primary.health() or await self.fallback.health()

    async def unload(self) -> None:
        await self.fallback.unload()

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.fallback.aclose()
