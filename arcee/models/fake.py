"""A scripted adapter for tests and dry runs: no network, deterministic replies."""

from collections import deque
from collections.abc import Iterable

from arcee.models.base import (
    Completion,
    ModelAdapter,
    ModelProfile,
    ModelUnavailableError,
    RawRequest,
    TokenUsage,
)

FAKE_PROFILE = ModelProfile(
    model="fake", json_mode="schema", context_tokens=32_768, cost_tier="local"
)


class FakeAdapter(ModelAdapter):
    """Returns `replies` in order and records every request it receives."""

    def __init__(self, replies: Iterable[str], profile: ModelProfile = FAKE_PROFILE) -> None:
        super().__init__(profile)
        self._replies = deque(replies)
        self.requests: list[RawRequest] = []

    async def _send(self, request: RawRequest) -> Completion:
        self.requests.append(request)
        if not self._replies:
            raise ModelUnavailableError("fake adapter has no scripted replies left")
        text = self._replies.popleft()
        prompt_chars = len(request.system) + sum(len(m.text) for m in request.messages)
        return Completion(text, TokenUsage(prompt_chars // 4, len(text) // 4))

    async def health(self) -> bool:
        return True
