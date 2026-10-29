"""A scripted adapter for tests and dry runs: no network, deterministic replies."""

from collections import deque
from collections.abc import Iterable

from arcee.models.base import Completion, ModelAdapter, ModelProfile, RawRequest, TokenUsage
from arcee.models.errors import ModelUnavailableError
from arcee.models.limiter import SYSTEM_CLOCK, Clock

FAKE_PROFILE = ModelProfile(
    model="fake", json_mode="schema", context_tokens=32_768, cost_tier="local"
)


class FakeAdapter(ModelAdapter):
    """Plays `script` in order and records every request it receives.

    A string is returned as the model's reply and a `Completion` as it is (how tests
    script native tool calls); an exception is raised from the round-trip, which is
    how tests simulate 429s and outages.
    """

    def __init__(
        self,
        script: Iterable[str | Completion | Exception],
        profile: ModelProfile = FAKE_PROFILE,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        super().__init__(profile, clock=clock)
        self._script = deque(script)
        self.requests: list[RawRequest] = []
        self.unloads = 0

    async def _send(self, request: RawRequest) -> Completion:
        self.requests.append(request)
        if not self._script:
            raise ModelUnavailableError("fake adapter has no scripted replies left")
        step = self._script.popleft()
        if isinstance(step, Exception):
            raise step
        if isinstance(step, Completion):
            return step
        prompt_chars = len(request.system) + sum(len(m.text) for m in request.messages)
        return Completion(step, TokenUsage(prompt_chars // 4, len(step) // 4))

    async def unload(self) -> None:
        self.unloads += 1

    async def health(self) -> bool:
        return True
