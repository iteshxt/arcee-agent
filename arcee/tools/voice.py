"""`voice.say`: read something out loud, asked for in words (decision 0019).

"Read it out loud", "say that aloud", "read me the file": typed or spoken, the text is
said by arceed's voice, as the answers to spoken requests are. It needs arceed (which
holds the voice) and the voice itself (in the .deb, or downloaded from Settings → Voice).
"""

from collections.abc import Callable
from typing import Final

from pydantic import Field

from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage

# Said aloud, at most this much of the text (a reply is cut at 600; asked for, more).
LONGEST_ASKED: Final = 4000

# Starts saying `text` and returns None, or why it cannot ("the voice is not here").
Say = Callable[[str], str | None]


class SayArgs(ToolArgs):
    text: str = Field(
        min_length=1,
        max_length=20_000,
        description="Exactly what to say: the earlier answer, the file's text, the words "
        "the user gave. Plain sentences; Markdown is read as words would be.",
    )


class VoiceSay(Tool[SayArgs]):
    name = "voice.say"
    description = "Say text out loud with Arcee's voice, on this computer's speakers."
    usage = Usage(
        when='"Read it out loud", "say that aloud", "read me the summary", "tell me by '
        'voice": pass the text itself (from the conversation, or read with fs.read first).',
        avoid="Answering normally: the reply is shown, and said too when the request was "
        "spoken. Call this only when the user asks to hear something. Progress notes: "
        "control.speak (shown, not said).",
        example='voice.say(text="It is 18 degrees and cloudy in Delhi.")',
    )
    args_model = SayArgs

    def __init__(self, say: Callable[[], Say | None]) -> None:
        self._say = say  # arceed's, once it is up; None in arcee-cli

    async def run(self, args: SayArgs, ctx: ToolContext) -> ToolResult:
        from arcee.voice_reply import speakable

        say = self._say()
        if say is None:
            return ToolResult.fail("saying things aloud needs arceed (the Arcee service)")
        text = speakable(args.text, longest=LONGEST_ASKED)
        if not text:
            return ToolResult.fail("nothing in that text can be said aloud")
        if (why := say(text)) is not None:
            return ToolResult.fail(why)
        seconds = round(len(text.split()) / 2.6)  # about 155 words a minute
        return ToolResult.ok(
            f"Saying it aloud (about {seconds} s)",
            data={"saying": text[:200], "about_seconds": seconds},
        )
