"""Test doubles shared across test modules."""

from datetime import UTC, datetime, timedelta


class FakeClock:
    """A clock whose `sleep` advances time instantly and records each wait."""

    def __init__(self, start: datetime = datetime(2026, 10, 23, 12, tzinfo=UTC)) -> None:
        self._start = start
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self.elapsed)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
