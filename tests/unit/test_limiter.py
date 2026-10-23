import asyncio
from zoneinfo import ZoneInfo

import pytest

from arcee.models.errors import QuotaExhaustedError
from arcee.models.limiter import WINDOW_S, RateLimiter
from tests.fakes import FakeClock

PACIFIC = ZoneInfo("America/Los_Angeles")


class _StuckClock(FakeClock):
    """A clock whose sleep never returns, to hold a request in the queue."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await asyncio.Event().wait()


class TestRequestsPerMinute:
    async def test_admits_up_to_rpm_without_waiting(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(rpm=3, rpd=None, clock=clock)

        for _ in range(3):
            await limiter.acquire()

        assert clock.sleeps == []

    async def test_waits_until_oldest_request_leaves_the_window(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(rpm=2, rpd=None, clock=clock)
        await limiter.acquire()
        clock.advance(10)
        await limiter.acquire()

        await limiter.acquire()

        assert clock.sleeps == [WINDOW_S - 10]

    async def test_never_exceeds_rpm_in_any_rolling_minute(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(rpm=5, rpd=None, clock=clock)
        admitted: list[float] = []

        for i in range(23):
            clock.advance(i % 3)  # irregular arrivals, including bursts
            await limiter.acquire()
            admitted.append(clock.monotonic())

        for t in admitted:
            assert sum(1 for s in admitted if t <= s < t + WINDOW_S) <= 5

    async def test_no_limit_without_rpm(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(rpm=None, rpd=None, clock=clock)

        for _ in range(1000):
            await limiter.acquire()

        assert clock.sleeps == []

    async def test_concurrent_callers_are_admitted_in_order(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter(rpm=1, rpd=None, clock=clock)
        order: list[int] = []

        async def call(i: int) -> None:
            await limiter.acquire()
            order.append(i)

        await asyncio.gather(*(call(i) for i in range(4)))

        assert order == [0, 1, 2, 3]
        assert clock.elapsed == 3 * WINDOW_S


class TestRequestsPerDay:
    async def test_raises_once_daily_budget_is_spent(self) -> None:
        limiter = RateLimiter(rpm=None, rpd=2, clock=FakeClock())
        await limiter.acquire()
        await limiter.acquire()

        with pytest.raises(QuotaExhaustedError):
            await limiter.acquire()
        assert limiter.remaining_today == 0

    async def test_budget_resets_at_the_quota_timezone_midnight(self) -> None:
        # 2026-10-23 12:00 UTC is 05:00 Pacific; Pacific midnight is 19 hours later.
        clock = FakeClock()
        limiter = RateLimiter(rpm=None, rpd=1, quota_tz=PACIFIC, clock=clock)
        await limiter.acquire()

        clock.advance(18 * 3600)
        with pytest.raises(QuotaExhaustedError):
            await limiter.acquire()

        clock.advance(3600)
        assert limiter.remaining_today == 1
        await limiter.acquire()

    async def test_request_cancelled_while_queued_costs_no_quota(self) -> None:
        clock = _StuckClock()
        limiter = RateLimiter(rpm=1, rpd=5, clock=clock)
        await limiter.acquire()

        waiter = asyncio.create_task(limiter.acquire())
        await clock.sleeping.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert limiter.remaining_today == 4
