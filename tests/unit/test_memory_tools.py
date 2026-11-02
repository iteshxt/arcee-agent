"""The Core's reach into memory: memory.recall and memory.forget."""

from pathlib import Path

from arcee.context.store import Store
from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import MEMORY, NOTE, filters_for
from arcee.models.base import Message
from arcee.tools.base import ToolContext
from arcee.tools.memory import ForgetArgs, MemoryForget, MemoryRecall, RecallArgs, memory_tools


def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(task_id="t_now", home=tmp_path)


async def test_recall_lists_facts_with_their_ids(tmp_path: Path) -> None:
    store = Store(tmp_path)
    memory_id = await store.save_memory("The user's home station is Abu Road (ABR)", 0.9)
    recall, _ = memory_tools(store)
    assert isinstance(recall, MemoryRecall)

    result = await recall.run(RecallArgs(query="home station"), ctx(tmp_path))

    assert result.summary == "Remembered 1 things"
    assert result.data is not None
    (line,) = result.data["memory"]
    assert line.startswith(f"{memory_id}: The user's home station is Abu Road (ABR) (")
    store.close()


async def test_forgetting_asks_with_the_facts_own_words(tmp_path: Path) -> None:
    store = Store(tmp_path)
    memory_id = await store.save_memory("The user lives in Jaipur", 0.9)
    recall, forget = memory_tools(store)
    assert isinstance(recall, MemoryRecall)
    assert isinstance(forget, MemoryForget)
    await recall.run(RecallArgs(query="where does the user live"), ctx(tmp_path))
    args = ForgetArgs(id=memory_id)

    assert forget.tier_for(args, Policy()) is Tier.CONFIRM
    assert forget.sentence(args) == "Forget “The user lives in Jaipur”?"
    done = await forget.run(args, ctx(tmp_path))

    assert done.summary == "Forgot: The user lives in Jaipur"
    assert await store.all_memories() == []
    again = await forget.run(args, ctx(tmp_path))
    assert again.error == f"no remembered fact has the id {memory_id}"
    store.close()


async def test_nothing_remembered_says_so(tmp_path: Path) -> None:
    store = Store(tmp_path)
    recall, _ = memory_tools(store)
    assert isinstance(recall, MemoryRecall)

    result = await recall.run(RecallArgs(query="passport number"), ctx(tmp_path))

    assert result.summary == "Nothing remembered about that"
    store.close()


def test_asking_about_memory_offers_the_memory_tools() -> None:
    def offered(request: str) -> tuple[str, ...]:
        return filters_for([Message("user", request)])

    assert set(MEMORY) <= set(offered("What do you know about me?"))
    assert set(MEMORY) <= set(offered("Forget my old address"))
    assert set(MEMORY) <= set(offered("do you remember my home station?"))
    saving = offered("Remember that my work folder is ~/code")
    assert set(NOTE) <= set(saving)
    assert "memory.*" not in saving  # saving needs only the note
    assert "memory.*" not in offered("open the files app")
