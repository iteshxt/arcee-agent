"""Keep each model under its provider's request limits (spec section 5).

Providers enforce requests-per-minute over a rolling minute, so the limiter keeps
the start times of recent requests and waits until the oldest one leaves the
window. (A token bucket sized to `rpm` would let nearly twice that many requests
through in one rolling minute after an idle period, which providers reject.)
Requests-per-day is a counter that resets at the provider's quota day boundary.
"""

import asyncio
import time
from collections import deque
from datetime import UTC, date, datetime, tzinfo
from typing import Final, Protocol

from arcee.models.errors import QuotaExhaustedError

WINDOW_S: Final = 60.0


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


SYSTEM_CLOCK: Final = SystemClock()


class RateLimiter:
    """Admits requests in arrival order without exceeding `rpm` or `rpd`.

    `quota_tz` is where the provider's day starts (Gemini resets at midnight
    Pacific); None means the local timezone.
    """

    def __init__(
        self,
        rpm: int | None,
        rpd: int | None,
        *,
        quota_tz: tzinfo | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._rpm = rpm
        self._rpd = rpd
        self._quota_tz = quota_tz
        self._clock = clock
        # asyncio.Lock wakes waiters in FIFO order, so queued requests stay in order.
        self._lock = asyncio.Lock()
        self._recent: deque[float] = deque()
        self._day: date | None = None
        self._used_today = 0

    @property
    def remaining_today(self) -> int | None:
        if self._rpd is None:
            return None
        used = self._used_today if self._day == self._quota_day() else 0
        return self._rpd - used

    async def acquire(self) -> None:
        """Wait for a free slot. Raise `QuotaExhaustedError` if the day's budget is spent."""
        async with self._lock:
            self._check_daily()
            if self._rpm is not None:
                await self._wait_for_minute_slot(self._rpm)
            # Counted only once admitted, so a request cancelled while queued costs nothing.
            self._used_today += 1

    def _check_daily(self) -> None:
        today = self._quota_day()
        if today != self._day:
            self._day, self._used_today = today, 0
        if self._rpd is not None and self._used_today >= self._rpd:
            raise QuotaExhaustedError(f"daily limit of {self._rpd} requests reached")

    async def _wait_for_minute_slot(self, rpm: int) -> None:
        while True:
            now = self._clock.monotonic()
            while self._recent and now - self._recent[0] >= WINDOW_S:
                self._recent.popleft()
            if len(self._recent) < rpm:
                self._recent.append(now)
                return
            await self._clock.sleep(self._recent[0] + WINDOW_S - now)

    def _quota_day(self) -> date:
        return self._clock.now().astimezone(self._quota_tz).date()
