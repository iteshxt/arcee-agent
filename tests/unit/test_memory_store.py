"""Memory in the store (decision 0014): facts and task summaries, found by their words."""

import sqlite3
from pathlib import Path

from arcee.context.store import Store, match_query


async def test_a_fact_is_found_by_the_words_of_a_later_request(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_memory("The user's home station is Abu Road (ABR)", 0.9, task_id="t_1")
    await store.save_memory("The user prefers Firefox for work", 0.5)

    found = await store.search_memories("book a train from my station to Delhi", 5)

    assert [row.text for row, _ in found] == ["The user's home station is Abu Road (ABR)"]
    assert found[0][1] > 0  # higher is better
    store.close()


async def test_stemming_matches_other_forms_of_a_word(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_memory("The user travels to Pune every month", 0.6)

    assert await store.search_memories("travelling plans", 5)
    store.close()


async def test_the_same_fact_again_is_not_a_second_fact(tmp_path: Path) -> None:
    store = Store(tmp_path)
    first = await store.save_memory("Work folder is ~/code", 0.4)
    again = await store.save_memory("work folder is ~/code", 0.8)

    rows = await store.all_memories()
    assert again == first
    assert [(r.text, r.importance) for r in rows] == [("Work folder is ~/code", 0.8)]
    store.close()


async def test_a_newer_fact_replaces_the_old_one(tmp_path: Path) -> None:
    store = Store(tmp_path)
    old = await store.save_memory("The user lives in Jaipur", 0.8)
    await store.save_memory("The user lives in Pune", 0.7, replaces=old)

    rows = await store.all_memories()
    assert [(r.id, r.text, r.importance) for r in rows] == [(old, "The user lives in Pune", 0.8)]
    assert await store.search_memories("Pune", 5)
    assert not await store.search_memories("Jaipur", 5)  # the index followed the update
    store.close()


async def test_forgetting_removes_a_fact_from_search(tmp_path: Path) -> None:
    store = Store(tmp_path)
    memory_id = await store.save_memory("Bank is HDFC", 0.6)

    assert await store.forget_memory(memory_id)
    assert not await store.search_memories("bank", 5)
    assert not await store.forget_memory(memory_id)
    store.close()


async def test_summaries_are_found_without_the_current_task(tmp_path: Path) -> None:
    store = Store(tmp_path)
    old = await store.start_task("find trains to Delhi")
    now = await store.start_task("trains again")
    await store.save_summary(old, "Searched IRCTC for trains from Abu Road to Delhi.")
    await store.save_summary(now, "Looking for trains to Delhi.")

    found = await store.search_summaries("trains to Delhi", 5, exclude=now)

    assert [(row.task_id, row.request) for row, _ in found] == [(old, "find trains to Delhi")]
    store.close()


async def test_forgetting_a_task_drops_what_it_taught(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = await store.start_task("set up my email")
    await store.save_memory("Email is on Fastmail", 0.7, task_id=task)
    await store.save_memory("Phone is a Pixel", 0.5, task_id="t_other")
    await store.save_summary(task, "Set up Fastmail in Thunderbird.")

    assert await store.forget_task(task) == 1
    assert [r.text for r in await store.all_memories()] == ["Phone is a Pixel"]
    assert not await store.search_summaries("Fastmail", 5)
    assert (await store.task(task)) is not None  # History keeps the task itself
    store.close()


async def test_an_old_history_db_gains_memory_and_keeps_its_notes(tmp_path: Path) -> None:
    # A history.db from before memory: version 1, a memories table without source.
    db = sqlite3.connect(tmp_path / "history.db")
    db.executescript(
        """
        CREATE TABLE memories (id TEXT PRIMARY KEY, task_id TEXT, text TEXT NOT NULL,
                               importance REAL NOT NULL, created REAL NOT NULL);
        INSERT INTO memories VALUES ('m_old', NULL, 'Daughter is called Asha', 0.9, 1.0);
        PRAGMA user_version=1;
        """
    )
    db.close()

    store = Store(tmp_path)

    rows = await store.all_memories()
    assert [(r.id, r.source, r.updated) for r in rows] == [("m_old", "note", 1.0)]
    assert await store.search_memories("daughter", 5)  # indexed on the way in
    store.close()


def test_a_request_becomes_a_query_of_its_content_words() -> None:
    assert match_query("What is my home station?") == '"home" OR "station"'
    assert match_query("the a of") == ""


async def test_tasks_without_a_summary_are_the_ones_left_to_learn(tmp_path: Path) -> None:
    store = Store(tmp_path)
    done = await store.start_task("old request")
    await store.finish_task(done, status="done", reply="ok", steps=0, model_calls=1)
    learned = await store.start_task("learned already")
    await store.finish_task(learned, status="done", reply="ok", steps=0, model_calls=1)
    await store.save_summary(learned, "Already digested.")
    await store.start_task("still running")

    assert await store.unsummarised(10) == [done]
    store.close()
