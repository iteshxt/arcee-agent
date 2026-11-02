"""After a task: a summary, and the facts it taught (decision 0014)."""

import json
from pathlib import Path
from typing import Any

from arcee.context.digest import digest_task
from arcee.context.store import Store
from arcee.core.executor import ActionRecord
from arcee.core.policy import Tier
from arcee.core.protocol import ExecutorResult
from arcee.models.base import ModelProfile
from arcee.models.fake import FakeAdapter

CONTEXT = ModelProfile(
    model="context", context_tokens=8192, cost_tier="local", call_mode="schema", json_mode="schema"
)


def reply(summary: str, *facts: dict[str, Any]) -> str:
    return json.dumps({"summary": summary, "facts": list(facts)})


async def finished_task(store: Store, request: str, reply_text: str, *tools: str) -> str:
    task_id = await store.start_task(request)
    for n, tool in enumerate(tools, 1):
        result = ExecutorResult(
            n=n, tool=tool, status="ok", summary=f"{tool} worked", data=None, duration_ms=5
        )
        await store.record_action(
            ActionRecord(
                ts=1000.0 + n,
                task_id=task_id,
                args={},
                tier=Tier.AUTO,
                approved_by="policy",
                result=result,
            )
        )
    await store.finish_task(
        task_id, status="done", reply=reply_text, steps=len(tools), model_calls=2
    )
    return task_id


async def test_a_task_leaves_a_summary_and_the_facts_it_taught(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = await finished_task(
        store, "book my usual train home from Abu Road", "Booked ABR to Delhi.", "browser.navigate"
    )
    model = FakeAdapter(
        [
            reply(
                "Booked a train from Abu Road (ABR) to Delhi on IRCTC.",
                {"text": "The user's home station is Abu Road (ABR)", "importance": 0.9},
            )
        ],
        CONTEXT,
    )

    digest = await digest_task(model, store, task)

    assert digest is not None
    found = await store.search_summaries("train Delhi", 5)
    assert [row.text for row, _ in found] == [
        "Booked a train from Abu Road (ABR) to Delhi on IRCTC."
    ]
    (fact,) = await store.all_memories()
    assert (fact.text, fact.importance, fact.task_id, fact.source) == (
        "The user's home station is Abu Road (ABR)",
        0.9,
        task,
        "digest",
    )
    sent = model.requests[0].messages[-1].text
    assert "Request: book my usual train home from Abu Road" in sent
    assert "- browser.navigate: browser.navigate worked" in sent
    store.close()


async def test_a_changed_fact_updates_the_one_already_known(tmp_path: Path) -> None:
    store = Store(tmp_path)
    old = await store.save_memory("The user lives in Jaipur", 0.9)
    task = await finished_task(store, "I moved: I live in Pune now", "Noted.")
    model = FakeAdapter(
        [
            reply(
                "The user said they moved to Pune.",
                {"text": "The user lives in Pune", "importance": 0.9, "replaces": old},
            )
        ],
        CONTEXT,
    )

    await digest_task(model, store, task)

    assert [(r.id, r.text) for r in await store.all_memories()] == [(old, "The user lives in Pune")]
    assert f"- {old}: The user lives in Jaipur" in model.requests[0].messages[-1].text
    store.close()


async def test_a_made_up_id_cannot_overwrite_a_fact(tmp_path: Path) -> None:
    store = Store(tmp_path)
    kept = await store.save_memory("The user's bank is HDFC", 0.7)
    task = await finished_task(store, "what is my favourite colour? it is blue", "Blue, noted.")
    model = FakeAdapter(
        [
            reply(
                "Favourite colour is blue.",
                {
                    "text": "The user's favourite colour is blue",
                    "importance": 0.4,
                    "replaces": kept,
                },
            )
        ],
        CONTEXT,
    )

    await digest_task(model, store, task)

    texts = sorted(r.text for r in await store.all_memories())
    assert texts == ["The user's bank is HDFC", "The user's favourite colour is blue"]
    store.close()


async def test_a_bare_request_that_did_nothing_is_not_digested(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task = await finished_task(store, "hi", "Hello!")
    model = FakeAdapter([], CONTEXT)

    assert await digest_task(model, store, task) is None
    assert model.requests == []
    store.close()
