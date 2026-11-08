# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""Spoken replies (decision 0019): answers said aloud, on this computer.

Made by Kitten TTS nano 0.8 (KittenML, Apache-2.0: 57 MB, eight voices) through
sherpa-onnx, which is handed the phonemes (arcee.phonemes, by Ubuntu's libespeak-ng).
The .deb carries the voice in /usr/share/arcee/models; elsewhere it is downloaded from
Hugging Face when first wanted and made ready for sherpa-onnx here (`fetch`). It is
loaded while a request is thought about and let go after IDLE_S unused. The float32
model, not KittenML's int8 one (24 MB): on this CPU the int8 one took 0.5 s for each
second of speech, the float32 one 0.2 s; more threads did not help either.

The reply is said a sentence at a time, each made while the one before plays, through
`pw-play`. What reads well on screen (Markdown, links, lists, °C) is turned into what
sounds right first (`speakable`).
"""

import array
import asyncio
import contextlib
import io
import logging
import re
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import httpx

from arcee.voice import VoiceError
from arcee.voice_local import VoiceModel, model_path

log = logging.getLogger(__name__)

HUGGING_FACE: Final = "https://huggingface.co/KittenML/kitten-tts-nano-0.8-fp32/resolve/main"
SPEECH: Final = VoiceModel(
    "voice",
    "says Arcee's answers aloud",
    HUGGING_FACE,
    ("model.onnx", "voices.bin", "tokens.txt"),
    60,
)
TOKENS: Final = Path(__file__).parent / "data" / "kitten-tokens.txt"
SAMPLE_RATE: Final = 24000


@dataclass(frozen=True, slots=True)
class Voice:
    name: str  # KittenTTS's own name for it
    key: str  # its style in voices.npz
    about: str  # how it sounds, for Settings → Voice
    pace: float  # KittenTTS's speed for it: set so every voice speaks about 215 wpm


# In the order of their styles in voices.bin (the speaker ids). How each sounds was
# measured on the same sentence (2026-11-08): median pitch and its spread (YIN). Bruno
# 121 Hz; Jasper, Hugo and Leo about 195 Hz, wide; Bella, Luna and Rosie 197-214 Hz;
# Kiki 255 Hz. At one speed they ran from 133 words a minute (Bella, Leo) to 196, and
# at the old 0.8 they all sounded drawn out (the user, 2026-11-09). Each pace is now set
# so all of them speak about 215 words a minute on a three-sentence answer.
VOICES: Final[tuple[Voice, ...]] = (
    Voice("Jasper", "expr-voice-2-m", "A light man's voice", 0.96),
    Voice("Bella", "expr-voice-2-f", "Calm and warm, even in tone", 1.34),
    Voice("Bruno", "expr-voice-3-m", "The deepest voice, steady", 0.9),
    Voice("Luna", "expr-voice-3-f", "Natural and expressive", 1.13),
    Voice("Hugo", "expr-voice-4-m", "Lively, with a wide range", 0.96),
    Voice("Rosie", "expr-voice-4-f", "Clear and even", 1.05),
    Voice("Leo", "expr-voice-5-m", "Relaxed and low-key", 1.3),
    Voice("Kiki", "expr-voice-5-f", "The brightest voice", 0.86),
)
DEFAULT_VOICE: Final = "Luna"
THREADS: Final = 2
IDLE_S: Final = 300.0  # the model is let go after this long unused
# A reply longer than this is cut at a sentence, and the rest left on screen.
LONGEST: Final = 600
REST_ON_SCREEN: Final = "The rest is on screen."
# A first sentence longer than this is said in two, cut at its first comma (or the
# like) after 12 characters: the first words come about half as soon.
FIRST_PIECE: Final = 60


def voice_id(name: str) -> int:
    """The speaker id of the voice called `name`; the default's when there is none."""
    names = [voice.name.lower() for voice in VOICES]
    wanted = name.lower() if name.lower() in names else DEFAULT_VOICE.lower()
    return names.index(wanted)


_URL: Final = re.compile(r"https?://\S+|www\.\S+")
_MD_LINK: Final = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_CODE_BLOCK: Final = re.compile(r"```.*?```", re.DOTALL)
_SENTENCE: Final = re.compile(r"(?<=[.!?…])\s+(?=[\"'“(]?[A-Z0-9])")


class _Pauses:
    """The first comma, semicolon, colon or dash after 12 characters, before a limit."""

    _pause = re.compile(r"[,;:—]\s+")

    def partition(self, text: str, limit: int) -> tuple[str, str, str]:
        for match in self._pause.finditer(text, 12):
            if match.start() > limit:
                break
            return text[: match.start()], match.group(), text[match.end() :]
        return text, "", ""


_PAUSE: Final = _Pauses()
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


def speakable(reply: str, longest: int = LONGEST) -> str:
    """`reply` as it should sound: no Markdown, code or addresses, list items as
    sentences, and at most `longest` characters, ended at a sentence."""
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
    if len(text) <= longest:
        return text
    kept = ""
    for sentence in sentences(text):
        if len(kept) + len(sentence) > longest:
            break
        kept = f"{kept} {sentence}".strip()
    if not kept:  # one very long sentence: cut it at a word
        kept = text[:longest].rsplit(" ", 1)[0] + "…"
    return f"{kept} {REST_ON_SCREEN}"


def sentences(text: str) -> list[str]:
    """`text` in the pieces it is made in: sentences, short ones joined to the next, and
    a long first one cut at its first pause, so the first words are heard sooner."""
    out: list[str] = []
    for piece in _SENTENCE.split(text.strip()):
        if out and len(out[-1]) < 25:
            out[-1] = f"{out[-1]} {piece}"
        elif piece:
            out.append(piece)
    if out and len(out[0]) > FIRST_PIECE:
        head, pause, rest = _PAUSE.partition(out[0], FIRST_PIECE)
        if pause:
            out[0:1] = [head + pause.strip(), rest]
    return out


class Synth(Protocol):
    sample_rate: int

    def say(self, text: str, voice: int) -> list[float]: ...


class Kitten:
    """Kitten through sherpa-onnx, given its phonemes."""

    def __init__(self, folder: Path) -> None:
        import sherpa_onnx

        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kitten=sherpa_onnx.OfflineTtsKittenModelConfig(
                    model=str(folder / "model.onnx"),
                    voices=str(folder / "voices.bin"),
                    tokens=str(folder / "tokens.txt"),
                ),
                num_threads=THREADS,
            )
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self._config = sherpa_onnx.GenerationConfig
        self.sample_rate = int(self._tts.sample_rate)

    def say(self, text: str, voice: int) -> list[float]:
        from arcee.phonemes import symbols

        chosen = VOICES[voice % len(VOICES)]
        config = self._config()
        config.sid = voice % len(VOICES)
        config.speed = chosen.pace
        config.tokens = [symbols(text)]
        return list(self._tts.generate(text, config).samples)


def make_synth() -> Synth:
    return Kitten(model_path(SPEECH))


async def fetch(
    on_progress: Callable[[float], None] = lambda fraction: None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Download Kitten from Hugging Face and make it ready for sherpa-onnx: its styles
    as voices.bin, and the metadata sherpa-onnx reads from the model (`prepare`)."""
    files = {"kitten_tts_nano_v0_8.onnx": 56_767_095, "voices.npz": 3_278_902}
    total = sum(files.values())
    got: dict[str, bytes] = {}
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
    try:
        for name in files:
            data = bytearray()
            async with client.stream("GET", f"{HUGGING_FACE}/{name}") as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data += chunk
                    done = sum(len(v) for v in got.values()) + len(data)
                    on_progress(min(1.0, done / total))
            got[name] = bytes(data)
    except httpx.HTTPError as exc:
        raise VoiceError(f"could not download Arcee's voice: {type(exc).__name__}") from exc
    finally:
        if own:
            await client.aclose()
    from arcee.voice_local import models_dir

    target = models_dir() / SPEECH.name
    await asyncio.to_thread(prepare, got["kitten_tts_nano_v0_8.onnx"], got["voices.npz"], target)
    on_progress(1.0)


def prepare(model: bytes, voices: bytes, target: Path) -> None:
    """KittenML's files as sherpa-onnx loads them, into `target`."""
    styles: list[bytes] = []
    with zipfile.ZipFile(io.BytesIO(voices)) as archive:
        for voice in VOICES:
            npy = archive.read(f"{voice.key}.npy")
            header = int.from_bytes(npy[8:10], "little")
            if b"'<f4'" not in npy[10 : 10 + header] or b"(400, 256)" not in npy[10 : 10 + header]:
                raise VoiceError(f"{voice.key}: not the style Kitten 0.8 has")
            styles.append(npy[10 + header :])
    names = [v.key for v in VOICES]
    metadata = {
        "model_type": "kitten-tts",
        "language": "English",
        "has_espeak": "1",
        "sample_rate": str(SAMPLE_RATE),
        "version": "1",
        "voice": "en-us",
        "style_dim": "400,256",
        "n_speakers": str(len(names)),
        "speaker2id": ",".join(f"{n}->{i}" for i, n in enumerate(names)),
        "id2speaker": ",".join(f"{i}->{n}" for i, n in enumerate(names)),
        "speaker_names": ",".join(names),
        "model_url": "https://huggingface.co/KittenML/kitten-tts-nano-0.8-fp32",
    }
    target.mkdir(parents=True, exist_ok=True)
    (target / "voices.bin").write_bytes(b"".join(styles))
    (target / "tokens.txt").write_bytes(TOKENS.read_bytes())
    # Fields appended to a protobuf message are merged into it: the model's own bytes
    # stay as they are, with sherpa-onnx's metadata (ModelProto.metadata_props) after.
    (target / "model.onnx").write_bytes(model + _metadata(metadata))
    if not all((target / name).is_file() for name in SPEECH.files):
        raise VoiceError("Arcee's voice was not made ready")


def _metadata(entries: dict[str, str]) -> bytes:
    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            low, n = n & 0x7F, n >> 7
            out.append(low | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def text(field: int, value: str) -> bytes:
        raw = value.encode()
        return bytes([field << 3 | 2]) + varint(len(raw)) + raw

    out = b""
    for key, value in entries.items():
        entry = text(1, key) + text(2, value)
        out += bytes([14 << 3 | 2]) + varint(len(entry)) + entry
    return out


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

    def say(self, text: str, voice: int = 0) -> asyncio.Task[None]:
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
                audio = await asyncio.to_thread(synth.say, part, voice)
                if n == 0:
                    log.info("first words after %.2f s", self._clock() - started)
                player.stdin.write(pcm(audio))
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
