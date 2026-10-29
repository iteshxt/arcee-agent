"""Control tools (spec section 7.5): how the Core ends, pauses or hands back a task.

They are registered like any tool so the model sees them and the registry
validates them, but the orchestrator acts on them itself: it never calls `run`.
"""

import asyncio
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


class WaitArgs(ToolArgs):
    seconds: float = Field(gt=0, le=30)


class Wait(Tool[WaitArgs]):
    name = "control.wait"
    description = "Pause for up to 30 seconds, e.g. while an app or page loads."
    usage = Usage(
        when="Something you started needs a moment before you can look at it.",
        avoid="Nothing is loading; waiting does not make a tool result change.",
        example="control.wait(seconds=3)",
    )
    args_model = WaitArgs
    observation_kind = "screenshot_auto"
    timeout_s = 35

    async def run(self, args: WaitArgs, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(args.seconds)
        return ToolResult.ok(f"Waited {args.seconds:g}s")


class SpeakArgs(ToolArgs):
    text: str = Field(min_length=1, max_length=500)


class Speak(Tool[SpeakArgs]):
    name = "control.speak"
    description = "Tell the user something now without ending the task, e.g. progress."
    usage = Usage(
        when="A long task has news the user should hear before it finishes.",
        avoid="Giving the final answer: reply in plain text instead.",
    )
    args_model = SpeakArgs
    observation_kind = "none"

    async def run(self, args: SpeakArgs, ctx: ToolContext) -> ToolResult:
        # Shown by the CLI through the tool's summary; spoken once voice exists (phase 5).
        return ToolResult.ok(f"Said: {args.text}")


class NoteArgs(ToolArgs):
    text: str = Field(min_length=1, max_length=500, description="One durable fact")
    importance: float = Field(default=0.5, ge=0, le=1)


class Note(Tool[NoteArgs]):
    name = "control.note"
    description = "Save a lasting fact about the user or their setup to memory."
    usage = Usage(
        when="The user states a preference or fact worth remembering next time "
        '("my work folder is ~/code", "I use Firefox").',
        avoid="Facts only about this task, or anything secret such as passwords.",
        example='control.note(text="The user\'s work folder is ~/code", importance=0.8)',
    )
    args_model = NoteArgs
    observation_kind = "none"

    async def run(self, args: NoteArgs, ctx: ToolContext) -> ToolResult:
        if ctx.notes is None:
            return ToolResult.fail("memory is not available in this session")
        note_id = await ctx.notes.save_note(ctx.task_id, args.text, args.importance)
        return ToolResult.ok(f"Remembered: {args.text}", data={"id": note_id})


CONTROL_TOOLS: Final = (Done, AskUser, Replan)
UTILITY_CONTROL_TOOLS: Final = (Wait, Speak, Note)
