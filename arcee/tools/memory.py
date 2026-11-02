"""The Core's own reach into memory (spec section 11): look something up, forget a fact.

Recall already puts what matters into every request (decision 0014); these are for
when the user asks about it ("what do you know about me?", "forget my old address").
Saving stays `control.note`.
"""

from typing import Any, ClassVar, Protocol

from pydantic import Field

from arcee.core.policy import Policy, Tier
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage


class Memory(Protocol):
    """The store, as these tools see it."""

    async def recall_items(
        self, query: str, exclude_task: str | None
    ) -> list[tuple[str, str, str]]: ...  # (id, text, date)
    async def forget_memory(self, memory_id: str) -> bool: ...


class RecallArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=300, description="What to look up, in words")


class MemoryRecall(Tool[RecallArgs]):
    name = "memory.recall"
    description = "Look up what Arcee remembers: facts about the user and past tasks."
    usage = Usage(
        when='The user asks what you know or remember ("what is my home station?", '
        '"what did we do about my visa?"), and the remembered facts shown do not answer it.',
        avoid="Facts about the world: those are knowledge or web.search.",
        example='memory.recall(query="home train station")',
    )
    args_model = RecallArgs
    read_only: ClassVar[bool] = True

    def __init__(self, memory: Memory, seen: dict[str, str]) -> None:
        self._memory = memory
        self._seen = seen

    async def run(self, args: RecallArgs, ctx: ToolContext) -> ToolResult:
        items = await self._memory.recall_items(args.query, ctx.task_id)
        if not items:
            return ToolResult.ok("Nothing remembered about that", data={"memory": []})
        self._seen.update({item_id: text for item_id, text, _ in items})
        lines = [f"{item_id}: {text} ({date})" for item_id, text, date in items]
        return ToolResult.ok(f"Remembered {len(items)} things", data={"memory": lines})


class ForgetArgs(ToolArgs):
    id: str = Field(pattern=r"^m_[0-9a-f]+$", description="The m_... id of the fact")


class MemoryForget(Tool[ForgetArgs]):
    name = "memory.forget"
    description = "Forget one remembered fact about the user, by its m_... id."
    usage = Usage(
        when='The user asks to forget something ("forget my old address"). Find its id '
        "with memory.recall first.",
        example='memory.forget(id="m_3fa1c2d0")',
    )
    args_model = ForgetArgs

    def __init__(self, memory: Memory, seen: dict[str, str]) -> None:
        self._memory = memory
        self._seen = seen  # the words of facts memory.recall showed, for the question

    def tier_for(self, args: ForgetArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM  # forgetting cannot be undone

    def sentence(self, args: ForgetArgs) -> str:
        return f"Forget “{self._seen.get(args.id, args.id)}”?"

    async def run(self, args: ForgetArgs, ctx: ToolContext) -> ToolResult:
        if not await self._memory.forget_memory(args.id):
            return ToolResult.fail(f"no remembered fact has the id {args.id}")
        text = self._seen.pop(args.id, args.id)
        data: dict[str, Any] = {"forgot": text}
        return ToolResult.ok(f"Forgot: {text}", data=data)


def memory_tools(memory: Memory) -> list[Tool[Any]]:
    seen: dict[str, str] = {}
    return [MemoryRecall(memory, seen), MemoryForget(memory, seen)]
