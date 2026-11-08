"""Days and weeks: the times people say, the summaries arceed writes, memory.history."""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from arcee.context.periods import (
    WEEKS_BACK,
    catch_up,
    midnight,
    next_run,
    span,
    summarise_day,
    summarise_week,
)
from arcee.context.store import SCHEMA_VERSION, Store
from arcee.core.toolsets import MEMORY, filters_for
from arcee.models.base import Message, ModelProfile
from arcee.models.fake import FakeAdapter
from arcee.tools.base import ToolContext
from arcee.tools.memory import HistoryArgs, MemoryHistory, memory_tools

CONTEXT = ModelProfile(
    model="context", context_tokens=8192, cost_tier="local", call_mode="schema", json_mode="schema"
)
# A Sunday: last week is Monday 26 October to Sunday 1 November.
TODAY = date(2026, 11, 8)


def summary(text: str) -> str:
    return json.dumps({"summary": text})


async def asked(
    store: Store, request: str, when: datetime, *, said: str | None = None, status: str = "done"
) -> str:
    task_id = await store.start_task(request)
    await store.finish_task(task_id, status=status, reply="ok", steps=1, model_calls=1)
    with sqlite3.connect(store.root / "history.db") as db:
        db.execute("UPDATE tasks SET started = ? WHERE id = ?", (when.timestamp(), task_id))
    if said is not None:
        await store.save_summary(task_id, said)
    return task_id


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


# --- the times people say ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "first", "last"),
    [
        ("today", "2026-11-08", "2026-11-08"),
        ("Yesterday?", "2026-11-07", "2026-11-07"),
        ("the day before yesterday", "2026-11-06", "2026-11-06"),
        ("this week", "2026-11-02", "2026-11-08"),
        ("last week", "2026-10-26", "2026-11-01"),
        ("2 weeks ago", "2026-10-19", "2026-10-25"),
        ("three days ago", "2026-11-05", "2026-11-05"),
        ("the last 7 days", "2026-11-02", "2026-11-08"),
        ("past two weeks", "2026-10-26", "2026-11-08"),
        ("on Monday", "2026-11-02", "2026-11-02"),
        ("last sunday", "2026-11-01", "2026-11-01"),  # never today
        ("2026-11-03", "2026-11-03", "2026-11-03"),
        ("7 November", "2026-11-07", "2026-11-07"),
        ("Nov 7th", "2026-11-07", "2026-11-07"),
        ("25 december", "2025-12-25", "2025-12-25"),  # the last one that has happened
        ("this month", "2026-11-01", "2026-11-08"),
        ("last month", "2026-10-01", "2026-10-31"),
    ],
)
def test_the_times_people_say_become_dates(words: str, first: str, last: str) -> None:
    found = span(words, TODAY)
    assert found is not None
    assert (found.first.isoformat(), found.last.isoformat()) == (first, last)


@pytest.mark.parametrize("words", ["soon", "next week", "the 31st of February", "999 days ago"])
def test_a_time_it_does_not_know_is_none(words: str) -> None:
    assert span(words, TODAY) is None


def test_the_nightly_run_is_just_after_midnight() -> None:
    assert next_run(datetime(2026, 11, 8, 23, 0)) == 65 * 60
    assert next_run(datetime(2026, 11, 8, 0, 4, 30)) == 24 * 3600 + 30


# --- summaries --------------------------------------------------------------------------


async def test_a_quiet_day_is_its_requests_without_a_model(tmp_path: Path) -> None:
    store = Store(tmp_path)
    day = TODAY - timedelta(days=1)
    await asked(store, "weather in Delhi", at(day, 9), said="It was 18 °C and cloudy in Delhi.")
    model = FakeAdapter([], CONTEXT)

    row = await summarise_day(model, store, day)

    assert row is not None
    assert (row.id, row.tasks, row.text) == (
        "day:2026-11-07",
        1,
        "It was 18 °C and cloudy in Delhi.",
    )
    assert model.requests == []
    store.close()


async def test_a_busy_day_is_written_by_the_model_from_its_tasks(tmp_path: Path) -> None:
    store = Store(tmp_path)
    day = TODAY - timedelta(days=1)
    await asked(store, "weather in Delhi", at(day, 9), said="18 °C in Delhi.")
    await asked(store, "book a train to Delhi", at(day, 9, 30), status="failed")
    await asked(store, "email Rahul the tickets", at(day, 18, 5))
    model = FakeAdapter([summary("You planned a trip to Delhi.")], CONTEXT)

    row = await summarise_day(model, store, day)

    assert row is not None
    assert row.text == "You planned a trip to Delhi."
    sent = model.requests[0].messages[-1].text
    assert "Saturday 07 November 2026: 3 requests" in sent
    assert "- 09:00 weather in Delhi → 18 °C in Delhi." in sent
    assert "- 09:30 book a train to Delhi (failed)" in sent
    assert "- 18:05 email Rahul the tickets" in sent
    store.close()


async def test_a_week_is_written_from_its_days(tmp_path: Path) -> None:
    store = Store(tmp_path)
    monday = date(2026, 10, 26)
    await asked(store, "weather in Delhi", at(monday, 9), said="18 °C in Delhi.")
    await asked(store, "play some music", at(monday + timedelta(days=4), 20), said="Played lofi.")
    await summarise_day(FakeAdapter([], CONTEXT), store, monday)
    model = FakeAdapter([summary("A quiet week: weather and music.")], CONTEXT)

    row = await summarise_week(model, store, monday)

    assert row is not None
    assert (row.id, row.tasks, row.text) == (
        "week:2026-10-26",
        2,
        "A quiet week: weather and music.",
    )
    sent = model.requests[0].messages[-1].text
    assert "Monday 26 October (1 requests): 18 °C in Delhi." in sent
    assert "Friday 30 October (1 requests): Played lofi." in sent  # no day summary: its tasks
    store.close()


async def test_catching_up_writes_each_missing_day_and_week_once(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await asked(store, "weather", at(date(2026, 10, 27), 9), said="Sunny.")
    await asked(store, "today's thing", at(TODAY, 9), said="Not over yet.")
    await asked(store, "too old", at(TODAY - timedelta(weeks=WEEKS_BACK + 2), 9), said="Old.")
    model = FakeAdapter([], CONTEXT)

    written = await catch_up(model, store, TODAY)

    assert written == 2  # 27 October, and its week; not today, not long ago
    days = await store.periods_between("day", 0, midnight(TODAY + timedelta(days=1)))
    weeks = await store.periods_between("week", 0, midnight(TODAY))
    assert [d.id for d in days] == ["day:2026-10-27"]
    assert [(w.id, w.text) for w in weeks] == [("week:2026-10-26", "Sunny.")]
    assert await catch_up(model, store, TODAY) == 0
    store.close()


async def test_forgetting_a_task_drops_the_day_that_told_of_it(tmp_path: Path) -> None:
    store = Store(tmp_path)
    day = TODAY - timedelta(days=1)
    secret = await asked(store, "my bank thing", at(day, 9), said="Checked the bank.")
    await asked(store, "weather", at(day - timedelta(days=1), 9), said="Sunny.")
    model = FakeAdapter([], CONTEXT)
    await catch_up(model, store, TODAY)

    await store.delete_tasks([secret])

    days = await store.periods_between("day", 0, midnight(TODAY))
    assert [d.id for d in days] == ["day:2026-11-06"]
    await store.delete_tasks()
    assert await store.periods_between("day", 0, midnight(TODAY)) == []
    store.close()


def test_the_store_is_at_version_5(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.close()
    with sqlite3.connect(tmp_path / "history.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        assert db.execute("SELECT COUNT(*) FROM periods").fetchone()[0] == 0


# --- memory.history ---------------------------------------------------------------------


def history(store: Store) -> MemoryHistory:
    return MemoryHistory(store, today=lambda: TODAY)


def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(task_id="t_now", home=tmp_path)


async def test_yesterday_lists_what_was_asked_and_the_days_summary(tmp_path: Path) -> None:
    store = Store(tmp_path)
    day = TODAY - timedelta(days=1)
    await asked(store, "weather in Delhi", at(day, 9), said="18 °C in Delhi.")
    await asked(store, "today", at(TODAY, 8))
    await summarise_day(FakeAdapter([], CONTEXT), store, day)

    result = await history(store).run(HistoryArgs(when="yesterday"), ctx(tmp_path))

    assert result.summary == "1 requests yesterday"
    assert result.data == {
        "when": "yesterday",
        "from": "2026-11-07",
        "to": "2026-11-07",
        "requests": 1,
        "summary": "18 °C in Delhi.",
        "asked": ["09:00 weather in Delhi → 18 °C in Delhi."],
    }
    store.close()


async def test_a_week_is_told_day_by_day(tmp_path: Path) -> None:
    store = Store(tmp_path)
    monday = date(2026, 10, 26)
    await asked(store, "weather", at(monday, 9), said="Sunny.")
    await asked(store, "music", at(monday + timedelta(days=2), 20))
    await catch_up(FakeAdapter([summary("Weather and music.")], CONTEXT), store, TODAY)

    result = await history(store).run(HistoryArgs(when="last week"), ctx(tmp_path))

    assert result.data is not None
    assert result.data["weeks"] == ["week of 2026-10-26: Weather and music."]
    assert result.data["days"] == [
        "Mon 2026-10-26 (1 requests): Sunny.",
        "Wed 2026-10-28 (1 requests): You asked: music.",
    ]
    store.close()


async def test_history_leaves_out_this_request_and_says_when_nothing_was_asked(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    tool = history(store)

    empty = await tool.run(HistoryArgs(when="Monday"), ctx(tmp_path))
    unknown = await tool.run(HistoryArgs(when="next week"), ctx(tmp_path))

    assert empty.summary == "Nothing was asked Monday 2026-11-02"
    assert unknown.error is not None
    assert "is not a time memory.history knows" in unknown.error
    store.close()


def test_memory_tools_include_history(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert [tool.name for tool in memory_tools(store)] == [
        "memory.recall",
        "memory.forget",
        "memory.history",
    ]
    store.close()


@pytest.mark.parametrize(
    "request_",
    [
        "What did I ask you yesterday?",
        "what did we do last week",
        "What was I working on on Monday?",
        "have I asked you about trains before?",
    ],
)
def test_asking_what_was_done_offers_the_memory_tools(request_: str) -> None:
    assert set(MEMORY) <= set(filters_for([Message("user", request_)]))
