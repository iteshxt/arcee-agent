"""Recall: what Arcee remembers that matters for a request (decision 0014)."""

from pathlib import Path

from arcee.context.recall import BUDGET_CHARS, Recalled, pack, rank, recall, recency
from arcee.context.store import MemoryRow, Store, SummaryRow

DAY = 86400.0
NOW = 1_800_000_000.0


def fact(text: str, importance: float, age_days: float, memory_id: str = "m_1") -> MemoryRow:
    ts = NOW - age_days * DAY
    return MemoryRow(memory_id, None, text, importance, ts, ts, "digest")


def test_recency_halves_every_two_weeks() -> None:
    assert recency(NOW, NOW) == 1.0
    assert abs(recency(NOW - 14 * DAY, NOW) - 0.5) < 1e-9
    assert recency(NOW + DAY, NOW) == 1.0  # a clock that moved back is not the future


def test_a_closer_match_wins_and_newer_breaks_a_tie() -> None:
    close = fact("Home station is Abu Road", 0.5, 30, "m_close")
    loose = fact("Took a train once", 0.5, 30, "m_loose")
    new = fact("Took a train last week", 0.5, 7, "m_new")

    ranked = rank([(close, 4.0), (loose, 1.0), (new, 1.0)], [], [], NOW)

    assert [r.id for r in ranked] == ["m_close", "m_new", "m_loose"]


def test_important_facts_always_lead() -> None:
    name = fact("The user's name is Itesh", 0.95, 60, "m_name")
    match = fact("Prefers aisle seats", 0.4, 1, "m_seat")

    ranked = rank([(match, 5.0)], [], [name], NOW)

    assert [r.id for r in ranked] == ["m_name", "m_seat"]


def test_a_past_task_reads_as_request_and_outcome() -> None:
    summary = SummaryRow("t_9", "find trains to Delhi", "Found 3 trains on IRCTC.", NOW - DAY)

    (item,) = rank([], [(summary, 2.0)], [], NOW)

    assert (item.id, item.text) == ("t_9", "find trains to Delhi → Found 3 trains on IRCTC.")


def test_the_block_stays_inside_its_budget() -> None:
    items = [Recalled(f"m_{i}", "x" * 300, 1.0 - i / 100, NOW) for i in range(20)]

    kept = pack(items)

    assert sum(len(r.text) + 24 for r in kept) <= BUDGET_CHARS
    assert [r.id for r in kept] == ["m_0", "m_1", "m_2", "m_3"]


async def test_a_week_old_fact_comes_back_for_a_request_that_needs_it(tmp_path: Path) -> None:
    # The phase 5 gate, at the storage level: told last week, used today unprompted.
    store = Store(tmp_path)
    old_task = await store.start_task("book my usual train home")
    await store.save_memory("The user's home station is Abu Road (ABR)", 0.7, task_id=old_task)
    await store.save_summary(old_task, "Booked Abu Road to Delhi on IRCTC for Friday.")
    await store.save_memory("The user likes dark mode", 0.3)
    week_later = lambda: NOW + 8 * DAY  # noqa: E731

    block = await recall(store, "find trains from my home station tomorrow", now=week_later)

    assert block.startswith("What you remember")
    assert "home station is Abu Road (ABR)" in block
    assert "book my usual train home → Booked Abu Road to Delhi" in block
    assert "dark mode" not in block  # nothing to do with trains
    store.close()


async def test_nothing_remembered_means_no_block(tmp_path: Path) -> None:
    store = Store(tmp_path)

    assert await recall(store, "hello there") == ""
    store.close()
