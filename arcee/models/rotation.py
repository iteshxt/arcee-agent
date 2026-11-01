"""Models that take turns at one rank of the ladder (decision 0012).

Groq allows each model its own 8,000 tokens a minute and 200,000 a day. Most
requests are easy, and gpt-oss-20b handles them; but a busy minute on it would
make the user wait while qwen3.8-27b and gpt-oss-120b sit idle. So a rank holds
several models in order of preference, and each call goes to the first that can
take it now. When all would wait, the one that frees up soonest takes it; a model
whose day is spent is skipped.
"""

import math
from collections.abc import Awaitable, Callable, Sequence
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
from arcee.models.errors import QuotaExhaustedError

# A wait shorter than this on the preferred model is not worth leaving it for.
TOLERABLE_WAIT_S: Final = 2.0


class RotatingAdapter(ModelAdapter):
    def __init__(self, members: Sequence[ModelAdapter]) -> None:
        if not members:
            raise ValueError("a rotation needs at least one model")
        super().__init__(members[0].profile)
        self.members = list(members)
        self.limiter = members[0].limiter

    def pick(self, request: ToolRequest | ModelRequest[Any]) -> list[ModelAdapter]:
        """The members to try, in order: the first that need not wait, then by wait."""
        waits = [(member.expected_wait(request), i) for i, member in enumerate(self.members)]
        ready = [i for wait, i in waits if wait <= TOLERABLE_WAIT_S]
        rest = [i for _, i in sorted(waits) if i not in ready and waits[i][0] < math.inf]
        return [self.members[i] for i in ready + rest]

    def expected_wait(self, request: ToolRequest | ModelRequest[Any]) -> float:
        return min(member.expected_wait(request) for member in self.members)

    async def respond(self, request: ToolRequest) -> Completion:
        async def call(member: ModelAdapter) -> Completion:
            return await member.respond(request)

        return await self._each(request, call)

    async def complete[M: BaseModel](self, request: ModelRequest[M]) -> ModelResponse[M]:
        async def call(member: ModelAdapter) -> ModelResponse[M]:
            return await member.complete(request)

        return await self._each(request, call)

    async def _each[T](
        self,
        request: ToolRequest | ModelRequest[Any],
        call: Callable[[ModelAdapter], Awaitable[T]],
    ) -> T:
        tried = self.pick(request)
        if not tried:
            raise QuotaExhaustedError(
                "every model's allowance for the day is used up: "
                + ", ".join(m.profile.model for m in self.members)
            )
        error: QuotaExhaustedError | None = None
        for member in tried:
            # Events and budgets name the model that actually answered.
            self.profile = member.profile
            try:
                result: T = await call(member)
            except QuotaExhaustedError as exc:
                error = exc  # spent while we were asking: the next one takes it
                continue
            finally:
                self.last_wait_s = member.last_wait_s
            return result
        assert error is not None
        raise error

    async def _send(self, request: RawRequest) -> Completion:  # pragma: no cover
        raise NotImplementedError("a rotation sends through its members")

    async def health(self) -> bool:
        for member in self.members:
            if await member.health():
                return True
        return False

    async def aclose(self) -> None:
        for member in self.members:
            await member.aclose()
