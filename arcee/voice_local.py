# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""The wake word, the first of voice's two tiers (decision 0015), via sherpa-onnx.

Tier one runs here: `WakeListener` reads the microphone through `pw-record`, and only
a small keyword spotter sees the audio until it hears "hey Arcee"; Silero VAD then
cuts out the request that follows. Tier two is Groq's Whisper, which transcribes that
request, and only that: nothing before the wake word is stored or sent. "Arcee stop"
stops what is running, with no transcription at all. The two small models (7 MB)
are downloaded on request (`download`) into the data folder. A wake word the user
trained to their own voice (wake_match) is heard alongside the spotter.
"""

import array
import asyncio
import bz2
import contextlib
import io
import logging
import tarfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from arcee import wake_match
from arcee.context.store import data_dir
from arcee.voice import RATE, RECORD_COMMAND, VoiceError

log = logging.getLogger(__name__)

RELEASES: Final = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True, slots=True)
class VoiceModel:
    name: str
    purpose: str
    url: str
    # Files kept from the archive (the rest, other precisions and test audio, is not).
    files: tuple[str, ...]
    size_mb: int  # what is kept, to show before downloading


SPOTTER: Final = VoiceModel(
    "wake",
    'hears "hey Arcee" and "Arcee stop"',
    f"{RELEASES}/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2",
    (
        "tokens.txt",
        "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    ),
    6,
)
VAD: Final = VoiceModel(
    "vad",
    "finds where you start and stop speaking",
    f"{RELEASES}/asr-models/silero_vad.onnx",
    ("silero_vad.onnx",),
    1,
)
MODELS: Final = (SPOTTER, VAD)

# The wake phrases in the spotter's word pieces (its bpe.model, encoded once). "Arcee"
# is not an English word, so each phrase comes in spellings that sound alike, all
# reported under one name.
KEYWORDS: Final = """\
▁HE Y ▁A R CE E @HEY_ARCEE
▁HE Y ▁A R SE E @HEY_ARCEE
▁HE Y ▁ R ▁C @HEY_ARCEE
▁A R CE E ▁ST O P @ARCEE_STOP
▁ R ▁C ▁ST O P @ARCEE_STOP
"""
WAKE: Final = "HEY_ARCEE"
STOP: Final = "ARCEE_STOP"
THRESHOLD: Final = 0.25
FRAME: Final = 1280  # 80 ms at 16 kHz
# After the wake word: the request ends after this much silence, or at most this long.
END_SILENCE_S: Final = 0.7
MAX_REQUEST_S: Final = 20.0
NO_REQUEST_S: Final = 6.0
# The spotter runs only while the VAD hears speech: fed the last 0.8 s on the way in
# (the VAD is sure of speech a moment after it starts), and kept on 0.6 s after it.
# Running it on every frame cost 3.5% of a core in a quiet room; the VAD costs 0.7%.
PREROLL_FRAMES: Final = 10
HANGOVER_FRAMES: Final = 8
# A trained wake word (wake_match) is checked every this many frames of speech (160 ms).
MATCH_EVERY: Final = 2
# The loudness gate before the VAD: a frame wakes it when it is GATE_RATIO times the
# room's level (its quietest frame of the last 10 s) and at least GATE_MIN; it sleeps
# after GATE_CALM_FRAMES (2 s) without speech or loudness.
GATE_RATIO: Final = 2.0
GATE_MIN: Final = 0.004
GATE_WINDOW_FRAMES: Final = 125
GATE_CALM_FRAMES: Final = 25
# For its first 10 s the room's level is at most this: started while someone talks, it
# would not take their voice for the room (a hiss, about 0.022 on the user's mic,
# still stays under the gate).
GATE_FIRST_MAX: Final = 0.015
# Logged as a near miss: speech within this many times the wake word's threshold.
NEAR_MISS: Final = 1.5


def models_dir() -> Path:
    return data_dir() / "models"


def model_path(model: VoiceModel) -> Path:
    return models_dir() / model.name


def installed(model: VoiceModel) -> bool:
    return all((model_path(model) / name).is_file() for name in model.files)


def missing() -> list[VoiceModel]:
    return [m for m in MODELS if not installed(m)]


async def download(
    model: VoiceModel,
    on_progress: Callable[[float], None] = lambda fraction: None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Fetch `model`, keep its files in the models folder; reports the fraction done."""
    target = model_path(model)
    target.mkdir(parents=True, exist_ok=True)
    data = bytearray()
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
    try:
        async with client.stream("GET", model.url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            async for chunk in response.aiter_bytes():
                data += chunk
                if total:
                    on_progress(min(1.0, len(data) / total))
    except httpx.HTTPError as exc:
        raise VoiceError(f"could not download {model.name}: {type(exc).__name__}") from exc
    finally:
        if own:
            await client.aclose()
    await asyncio.to_thread(_unpack, model, bytes(data), target)


async def download_missing(on_progress: Callable[[float], None] = lambda fraction: None) -> None:
    """Fetch whichever of the models is missing; reports the fraction of all of it."""
    needed = missing()
    total = sum(m.size_mb for m in needed) or 1
    done = 0
    for model in needed:

        def progress(fraction: float, model: VoiceModel = model, done: int = done) -> None:
            on_progress((done + fraction * model.size_mb) / total)

        await download(model, progress)
        done += model.size_mb
    on_progress(1.0)


def _unpack(model: VoiceModel, data: bytes, target: Path) -> None:
    if not model.url.endswith(".tar.bz2"):
        (target / model.files[0]).write_bytes(data)
        return
    wanted = set(model.files)
    with tarfile.open(fileobj=io.BytesIO(bz2.decompress(data)), mode="r:") as archive:
        for member in archive.getmembers():
            name = Path(member.name).name
            if member.isfile() and name in wanted:
                source = archive.extractfile(member)
                if source is not None:
                    (target / name).write_bytes(source.read())
    if not installed(model):
        raise VoiceError(f"{model.name}: the download did not hold the expected files")


def samples(pcm: bytes) -> list[float]:
    """16-bit little-endian PCM to floats in [-1, 1]."""
    return [s / 32768 for s in array.array("h", pcm)]


def make_spotter(phrases: str = KEYWORDS) -> Any:
    import sherpa_onnx

    d = model_path(SPOTTER)
    keywords = models_dir() / "keywords.txt"
    keywords.write_text(phrases)
    return sherpa_onnx.KeywordSpotter(
        tokens=str(d / "tokens.txt"),
        encoder=str(d / SPOTTER.files[1]),
        decoder=str(d / SPOTTER.files[2]),
        joiner=str(d / SPOTTER.files[3]),
        keywords_file=str(keywords),
        num_threads=1,
        keywords_threshold=THRESHOLD,
    )


def make_vad() -> Any:
    import sherpa_onnx

    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(model_path(VAD) / "silero_vad.onnx")
    config.silero_vad.min_silence_duration = END_SILENCE_S
    config.silero_vad.max_speech_duration = MAX_REQUEST_S
    config.sample_rate = RATE
    return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=MAX_REQUEST_S + 10)


@dataclass(frozen=True, slots=True)
class Burst:
    """One stretch of sound the listener judged, for `arcee-cli voice-watch`."""

    kind: str  # "wake", "stop", "speech" (not the wake word) or "noise" (loud, not speech)
    seconds: float
    loudness: float  # its loudest frame, 0..1
    room: float  # the room's level the gate had learnt
    wake: float  # nearest to the trained wake word (1.0: never compared)
    stop: float  # and to "Arcee stop"
    pcm: bytes = b""


class WakeListener:
    """Listens for the wake word for as long as it runs.

    `on_wake()` when it hears "hey Arcee", `on_request(pcm)` with what was said after
    it (b"" when nothing was), `on_stop()` for "Arcee stop", `on_level(loudness)` while
    a request is being heard.
    """

    def __init__(
        self,
        *,
        on_wake: Callable[[], None],
        on_request: Callable[[bytes], None],
        on_stop: Callable[[], None],
        on_level: Callable[[float], None] = lambda loudness: None,
        command: tuple[str, ...] = RECORD_COMMAND,
        keywords: str = KEYWORDS,
        on_burst: Callable[[Burst], None] | None = None,
    ) -> None:
        # Each stretch of sound judged, with its audio (voice-watch); arceed has none.
        self._on_burst = on_burst
        self._keywords = keywords
        self._on_wake = on_wake
        self._on_request = on_request
        self._on_stop = on_stop
        self._on_level = on_level
        self._command = command
        self.paused = False  # the mic button records on its own meanwhile
        self.trained: wake_match.Trained | None = None  # the user's own, when trained
        self._hearing = False
        self._drop = False

    def drop(self) -> bool:
        """Throw away the request being heard (Esc, Stop); whether there was one."""
        if self._hearing:
            self._drop = True
        return self._hearing

    def reload(self) -> None:
        """Take up a newly trained (or forgotten) wake word."""
        self.trained = wake_match.load()

    @property
    def needs_spotter(self) -> bool:
        """The English keyword spotter, for an untrained voice. With both "hey Arcee" and
        "Arcee stop" trained it is not loaded (its model alone is 38 MB)."""
        return self.trained is None or not self.trained.stops

    async def run(self) -> None:
        if missing_models := [m.name for m in (SPOTTER, VAD) if not installed(m)]:
            raise VoiceError(f"voice models not downloaded: {', '.join(missing_models)}")
        self.reload()
        spotter, vad = await asyncio.to_thread(
            lambda: (make_spotter(self._keywords) if self.needs_spotter else None, make_vad())
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
        except FileNotFoundError as exc:
            raise VoiceError("pw-record is missing (PipeWire's tools)") from exc
        assert process.stdout is not None
        try:
            await self._listen(process.stdout, spotter, vad)
        finally:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()

    async def _listen(self, audio: asyncio.StreamReader, spotter: Any, vad: Any) -> None:
        from arcee.voice import level

        stream = spotter.create_stream() if spotter is not None else None
        heard = wake_match.Features()  # the speech so far, for a trained wake word
        frames_spotted = 0
        closest = 1.0  # this burst's nearest to the wake word, for the near-miss log
        hearing: bytearray | None = None  # the request after the wake word
        heard_for = 0.0
        recent: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        spotting = False
        quiet = 0
        # The loudness gate: the VAD sleeps while the room is at its usual level.
        levels: deque[float] = deque(maxlen=GATE_WINDOW_FRAMES)
        floor = 0.0
        awake = False
        calm = 0
        # For voice-watch: each stretch's loudest frame, speech in it, its audio, and
        # how near it came to "Arcee stop".
        peak, any_speech, sound, closest_stop = 0.0, False, bytearray(), 1.0
        speech = False  # the VAD's last word, for the room's level
        while True:
            try:
                chunk = await audio.readexactly(FRAME * 2)
            except asyncio.IncompleteReadError:
                return  # the recorder ended (the mic went away)
            if self.paused:
                continue
            if hearing is not None and self._drop:
                hearing, self._hearing, self._drop = None, False, False
                vad.reset()
                continue
            if hearing is not None:
                hearing += chunk
                heard_for += FRAME / RATE
                self._on_level(level(chunk))
                vad.accept_waveform(samples(chunk))
                if not vad.empty():  # a whole utterance has ended
                    pcm = _pcm(vad.front.samples)
                    vad.pop()
                    hearing, self._hearing = None, False
                    self._on_request(pcm)
                elif heard_for >= NO_REQUEST_S and not vad.is_speech_detected():
                    hearing, self._hearing = None, False
                    vad.reset()
                    self._on_request(b"")
                elif heard_for >= MAX_REQUEST_S:
                    vad.flush()
                    pcm = _pcm(vad.front.samples) if not vad.empty() else bytes(hearing)
                    vad.reset()
                    hearing, self._hearing = None, False
                    self._on_request(pcm)
                continue
            # Tier one's own gates. First the room's loudness: the VAD (1.4% of a core)
            # wakes only when a frame is clearly louder than the room's own level (a
            # steady hiss included), and sleeps again after GATE_CALM_FRAMES quiet.
            loudness = level(chunk)
            # The room's level is its quietest moment of the last GATE_WINDOW_FRAMES: a
            # steady hiss or fan sets it; talk, typing or a knock on the laptop cannot
            # (an average climbed with them, to 0.4, and shut the gate on "hey Arcee").
            levels.append(loudness)
            floor = min(levels)
            if len(levels) < GATE_WINDOW_FRAMES:  # just started: maybe mid-sentence
                floor = min(floor, GATE_FIRST_MAX)
            loud = loudness > max(floor * GATE_RATIO, GATE_MIN)
            recent.append(chunk)
            if self._on_burst is not None and (awake or loud):
                peak = max(peak, loudness)
                if spotting:
                    sound += chunk
            if not awake:
                if not loud:
                    continue
                # It slept only after 2 s with no speech: its state is silence, kept.
                awake, calm, any_speech, peak = True, 0, False, loudness
                for earlier in list(recent)[:-1]:  # the start of "hey", before it was loud
                    vad.accept_waveform(samples(earlier))
            frame = samples(chunk)
            # Then the VAD: matching runs only while someone speaks, from a little before.
            vad.accept_waveform(frame)
            while not vad.empty():  # speech before any wake word is not kept
                vad.pop()
            speech = vad.is_speech_detected()
            any_speech = any_speech or speech
            calm = 0 if (speech or spotting or loud) else calm + 1
            if calm > GATE_CALM_FRAMES:
                awake = False
                if not any_speech:
                    self._burst("noise", 0.0, peak, floor, 1.0, 1.0, b"")
            if speech:
                quiet = 0
                if not spotting:
                    spotting = True
                    if spotter is not None:
                        stream = spotter.create_stream()
                    keep = trained.window if (trained := self.trained) else wake_match.WINDOW
                    heard, frames_spotted, closest = wake_match.Features(keep), 0, 1.0
                    closest_stop, sound = 1.0, bytearray(b"".join(recent))
                    feed = [samples(c) for c in recent]  # heard before the VAD was sure
                else:
                    feed = [frame]
            elif spotting:
                quiet += 1
                feed = [frame]
                if quiet > HANGOVER_FRAMES:
                    spotting = False
                    self._near_miss(closest)
                    self._burst(
                        "speech",
                        len(sound) / 2 / RATE,
                        peak,
                        floor,
                        closest,
                        closest_stop,
                        bytes(sound),
                    )
                    peak = 0.0
                    continue
            else:
                continue
            found = ""
            if spotter is not None and stream is not None:
                for piece in feed:
                    stream.accept_waveform(RATE, piece)
                found = _spot(spotter, stream)
            if (trained := self.trained) is not None and not found:
                for piece in feed:
                    heard.push(piece)
                frames_spotted += 1
                if frames_spotted % MATCH_EVERY == 0 and len(heard.frames) >= trained.shortest:
                    wake, stop = trained.distances(list(heard.frames))
                    closest, closest_stop = min(closest, wake), min(closest_stop, stop)
                    found = {"wake": WAKE, "stop": STOP}.get(trained.judge(wake, stop), "")
                    if found:
                        heard = wake_match.Features(heard.frames.maxlen or wake_match.WINDOW)
            if found:
                kind = "stop" if found == STOP else "wake"
                seconds = len(sound) / 2 / RATE
                self._burst(kind, seconds, peak, floor, closest, closest_stop, bytes(sound))
                peak = 0.0
            if found == STOP:
                self._on_stop()
            elif found == WAKE:
                log.info("wake word heard")
                self._on_wake()
                hearing, heard_for, spotting = bytearray(), 0.0, False
                self._hearing, self._drop = True, False
                closest = 1.0
                vad.reset()

    def _burst(
        self,
        kind: str,
        seconds: float,
        peak: float,
        room: float,
        wake: float,
        stop: float,
        pcm: bytes,
    ) -> None:
        if self._on_burst is not None:
            self._on_burst(Burst(kind, seconds, peak, room, wake, stop, pcm))

    def _near_miss(self, closest: float) -> None:
        """Speech that came close to the wake word without reaching it: logged, so a
        voice that is missed can be seen (journalctl --user -u arceed)."""
        trained = self.trained
        if trained is not None and trained.threshold < closest <= trained.threshold * NEAR_MISS:
            log.info(
                "near miss: %.3f from the wake word (it wakes at %.3f)", closest, trained.threshold
            )


# Training (wake_match) waits this long for each phrase to start.
PHRASE_WAIT_S: Final = 8.0


async def record_phrase(
    on_level: Callable[[float], None] = lambda loudness: None,
    command: tuple[str, ...] = RECORD_COMMAND,
    vad: Any = None,
) -> list[float]:
    """One spoken phrase from the microphone, cut out by the VAD, as samples."""
    from arcee.voice import level

    vad = vad if vad is not None else await asyncio.to_thread(make_vad)
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except FileNotFoundError as exc:
        raise VoiceError("pw-record is missing (PipeWire's tools)") from exc
    assert process.stdout is not None
    waited = 0.0
    try:
        while True:
            try:
                chunk = await process.stdout.readexactly(FRAME * 2)
            except asyncio.IncompleteReadError as exc:
                raise VoiceError("the microphone stopped") from exc
            on_level(level(chunk))
            vad.accept_waveform(samples(chunk))
            if not vad.empty():
                phrase = list(vad.front.samples)
                vad.pop()
                vad.reset()
                return phrase
            waited += FRAME / RATE
            if waited >= PHRASE_WAIT_S and not vad.is_speech_detected():
                raise VoiceError("I did not hear anything")
            if waited >= PHRASE_WAIT_S + MAX_REQUEST_S:
                raise VoiceError("that went on too long")
    finally:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        await process.wait()


def _spot(spotter: Any, stream: Any) -> str:
    """The keyword the spotter heard in what it was fed, or ""."""
    found = ""
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        if result := spotter.get_result(stream):
            found = result
            spotter.reset_stream(stream)
    return found


def _pcm(values: Any) -> bytes:
    return array.array("h", (max(-32768, min(32767, int(v * 32767))) for v in values)).tobytes()
