"""Control tools (spec section 7.5): how the Core ends, pauses or hands back a task.

They are registered like any tool so the model sees them and the registry
validates them, but the orchestrator acts on them itself: it never calls `run`.
"""

from typing import Final

from pydantic import Field

from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage

DONE: Final = "control.done"
ASK_USER: Final = "control.ask_user"
REPLAN: Final = "control.replan"


class _Handled[A: ToolArgs](Tool[A]):
    observation_kind = "none"

    async def run(self, args: A, ctx: ToolContext) -> ToolResult:
        raise RuntimeError(f"{self.name} is handled by the orchestrator, not run")


class DoneArgs(ToolArgs):
    reply: str = Field(min_length=1, description="The answer for the user, using the facts found")


class Done(_Handled[DoneArgs]):
    name = DONE
    description = "Finish the task and give the user the answer."
    usage = Usage(
        when="The task is answered or done, using facts from tool results.",
        avoid="You have not checked anything yet, or the last action failed.",
        mistakes="Guessing facts you never observed.",
        example='control.done(reply="There are 3 PDFs in ~/Documents.")',
    )
    args_model = DoneArgs


class AskArgs(ToolArgs):
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list[str], max_length=6)


class AskUser(_Handled[AskArgs]):
    name = ASK_USER
    description = "Ask the user a question and wait for the answer."
    usage = Usage(
        when="You cannot continue without the user: the request is ambiguous, or "
        "three searches found nothing.",
        avoid="A tool can find the answer.",
        example='control.ask_user(question="I could not find it. What is the file called?")',
    )
    args_model = AskArgs


class ReplanArgs(ToolArgs):
    reason: str = Field(min_length=1)
    tried: list[str] = Field(default_factory=list[str])


class Replan(_Handled[ReplanArgs]):
    name = REPLAN
    description = "Give up on the current plan and explain why."
    usage = Usage(
        when="The plan cannot work as written, after real attempts.",
        avoid="One more different attempt could work.",
    )
    args_model = ReplanArgs


CONTROL_TOOLS: Final = (Done, AskUser, Replan)
