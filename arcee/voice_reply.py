# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""Spoken replies (decision 0019): the answer to a spoken request, said aloud.

Made on this computer by Supertonic through sherpa-onnx, which reads plain text (no
espeak-ng, which kept Piper's voices out). The model (92 MB) is downloaded when the
user turns "Answer aloud" on. It is loaded on the first reply, about a second, and let
go after IDLE_S unused: it holds about 250 MB.

The reply is said a sentence at a time, each made while the one before plays, through
`pw-play`: the first is heard about half a second after the answer. What reads well on
screen (Markdown, links, lists) is turned into what sounds right first (`speakable`).
"""

import array
import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Final, Protocol

from arcee.voice_local import RELEASES, VoiceModel, model_path

log = logging.getLogger(__name__)

SPEECH: Final = VoiceModel(
    "speech",
    "says Arcee's answers aloud",
    f"{RELEASES}/tts-models/sherpa-onnx-supertonic-tts-int8-2026-03-06.tar.bz2",
    (
        "duration_predictor.int8.onnx",
        "text_encoder.int8.onnx",
        "vector_estimator.int8.onnx",
        "vocoder.int8.onnx",
        "tts.json",
        "unicode_indexer.bin",
        "voice.bin",
    ),
    92,
)
VOICES: Final = 10  # Supertonic's own, as speaker ids 0-9
DEFAULT_VOICE: Final = 0
THREADS: Final = 2
IDLE_S: Final = 300.0  # the model is let go after this long unused
# A reply longer than this is cut at a sentence, and the rest left on screen.
LONGEST: Final = 600
REST_ON_SCREEN: Final = "The rest is on screen."

_URL: Final = re.compile(r"https?://\S+|www\.\S+")
_MD_LINK: Final = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_CODE_BLOCK: Final = re.compile(r"```.*?```", re.DOTALL)
_SENTENCE: Final = re.compile(r"(?<=[.!?…])\s+(?=[\"'“(]?[A-Z0-9])")
_CLOCK: Final = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?m\b)", re.IGNORECASE)
# Read wrongly as written: "18 °C" was said "18 to see" (2026-11-08).
_UNITS: Final = (
    (re.compile(r"\s?°\s?C\b"), " degrees Celsius"),
    (re.compile(r"\s?°\s?F\b"), " degrees Fahrenheit"),
    (re.compile(r"\s?°"), " degrees"),
    (re.compile(r"(?<=\d)\s?%"), " percent"),
    (re.compile(r"(?<=\d)\s?km/h\b"), " kilometres an hour"),
    (re.compile(r"\s&\s"), " and "),
    (re.compile(r"\s(->|→)\s"), " to "),
)


def _clock(match: re.Match[str]) -> str:
    """ "13:00" as said: "1 PM"; "10:30" as "10 30 AM" ("13:00" was said "1300")."""
    hour, minute = int(match[1]), int(match[2])
    half = "AM" if hour < 12 else "PM"
    hour = hour % 12 or 12
    if minute == 0:
        return f"{hour} {half}"
    # "10:30 AM" was said "30 AM": the model drops what is before a colon.
    return f"{hour} {minute:02d} {half}" if minute >= 10 else f"{hour} oh {minute} {half}"


def said(text: str) -> str:
    """Clock times, units and symbols in words, as they are said."""
    text = _CLOCK.sub(_clock, text)
    for pattern, words in _UNITS:
        text = pattern.sub(words, text)
    return text


def speakable(reply: str) -> str:
    """`reply` as it should sound: no Markdown, code or addresses, list items as
    sentences, and at most LONGEST characters, ended at a sentence."""
    text = _CODE_BLOCK.sub(" ", reply)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("the link on screen", text)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        line = re.sub(r"^#{1,6}\s*", "", line)  # headings
        line = re.sub(r"^([-*•]|\d+[.)])\s+", "", line)  # list markers
        line = re.sub(r"[*`~|]+", "", line)  # emphasis, code, tables
        line = re.sub(r"_+", " ", line).strip()  # __bold__, and snake_case names
        if not line:
            continue
        if line[-1] not in ".!?:;,…":
            line += "."  # a list item or heading ends as a sentence would
        lines.append(line)
    text = re.sub(r"\s+", " ", said(" ".join(lines))).strip()
    if len(text) <= LONGEST:
        return text
    kept = ""
    for sentence in sentences(text):
        if len(kept) + len(sentence) > LONGEST:
            break
        kept = f"{kept} {sentence}".strip()
    if not kept:  # one very long sentence: cut it at a word
        kept = text[:LONGEST].rsplit(" ", 1)[0] + "…"
    return f"{kept} {REST_ON_SCREEN}"


def sentences(text: str) -> list[str]:
    """`text` in sentences, short ones joined to the next (each is made in one go)."""
    out: list[str] = []
    for piece in _SENTENCE.split(text.strip()):
        if out and len(out[-1]) < 25:
            out[-1] = f"{out[-1]} {piece}"
        elif piece:
            out.append(piece)
    return out


class Synth(Protocol):
    sample_rate: int

    def generate(self, text: str, sid: int = 0, speed: float = 1.0) -> Any: ...


def make_synth() -> Synth:
    import sherpa_onnx

    folder = model_path(SPEECH)
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            supertonic=sherpa_onnx.OfflineTtsSupertonicModelConfig(
                duration_predictor=str(folder / "duration_predictor.int8.onnx"),
                text_encoder=str(folder / "text_encoder.int8.onnx"),
                vector_estimator=str(folder / "vector_estimator.int8.onnx"),
                vocoder=str(folder / "vocoder.int8.onnx"),
                tts_json=str(folder / "tts.json"),
                unicode_indexer=str(folder / "unicode_indexer.bin"),
                voice_style=str(folder / "voice.bin"),
            ),
            num_threads=THREADS,
        )
    )
    return sherpa_onnx.OfflineTts(config)


def pcm(samples: Any) -> bytes:
    """Float samples to 16-bit little-endian PCM."""
    return array.array("h", (int(max(-1.0, min(1.0, v)) * 32767) for v in samples)).tobytes()


def play_command(rate: int) -> tuple[str, ...]:
    return (
        "pw-play",
        "--raw",
        "--rate",
        str(rate),
        "--channels",
        "1",
        "--format",
        "s16",
        "--media-role",
        "Communication",
        "-",
    )


class Speaker:
    """Says replies aloud, one at a time: a new one, or `stop`, ends the one playing."""

    def __init__(
        self,
        make: Callable[[], Synth] = make_synth,
        *,
        command: Callable[[int], tuple[str, ...]] = play_command,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._make = make
        self._command = command
        self._clock = clock
        self._synth: Synth | None = None
        self._used = 0.0
        self._task: asyncio.Task[None] | None = None
        self._player: asyncio.subprocess.Process | None = None
        self._unload: asyncio.TimerHandle | None = None
        self._loading = asyncio.Lock()
        self._warming: asyncio.Task[Synth] | None = None

    @property
    def speaking(self) -> bool:
        return self._task is not None and not self._task.done()

    def say(self, text: str, voice: int = DEFAULT_VOICE) -> asyncio.Task[None]:
        """Start saying `text` (already `speakable`); the task ends when it has been said."""
        self.stop()
        self._task = asyncio.ensure_future(self._say(text, voice))
        return self._task

    def warm(self) -> None:
        """Load the model now, while the request is still thought about: the answer is
        then heard about half a second after it comes, not a second and a half."""
        if self._synth is None and (self._warming is None or self._warming.done()):
            self._warming = asyncio.ensure_future(self._loaded())
            self._warming.add_done_callback(lambda _t: self._schedule_unload())

    def stop(self) -> bool:
        """Stop talking at once; whether it was."""
        was = self.speaking
        if self._player is not None and self._player.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._player.kill()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        return was

    async def _say(self, text: str, voice: int) -> None:
        parts = sentences(text)
        if not parts:
            return
        started = self._clock()
        synth = await self._loaded()
        player = await asyncio.create_subprocess_exec(
            *self._command(synth.sample_rate),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._player = player
        assert player.stdin is not None
        try:
            for n, part in enumerate(parts):
                audio = await asyncio.to_thread(synth.generate, part, voice % VOICES, 1.0)
                if n == 0:
                    log.info("first words after %.2f s", self._clock() - started)
                player.stdin.write(pcm(audio.samples))
                await player.stdin.drain()
            player.stdin.close()
            await player.wait()
        except BrokenPipeError, ConnectionResetError:
            pass  # stopped: the player was killed
        finally:
            if player.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    player.kill()
                with contextlib.suppress(Exception):
                    await player.wait()
            self._used = self._clock()
            self._schedule_unload()

    async def _loaded(self) -> Synth:
        if self._unload is not None:
            self._unload.cancel()
            self._unload = None
        async with self._loading:  # warm() and a reply at once load it once
            if self._synth is None:
                started = self._clock()
                self._synth = await asyncio.to_thread(self._make)
                log.info("speech model loaded in %.2f s", self._clock() - started)
            return self._synth

    def _schedule_unload(self) -> None:
        if self._unload is not None:
            self._unload.cancel()
        with contextlib.suppress(RuntimeError):  # no loop: nothing to schedule
            self._unload = asyncio.get_running_loop().call_later(IDLE_S, self._let_go)

    def _let_go(self) -> None:
        if not self.speaking and self._synth is not None:
            self._synth = None
            log.info("speech model let go after %.0f s unused", IDLE_S)
