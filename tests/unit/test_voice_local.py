"""Local voice (decision 0015): model downloads, and the wake-word listener's flow."""

import array
import asyncio
import bz2
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest

from arcee import voice_local, wake_match
from arcee.voice import VoiceError
from arcee.voice_local import FRAME, STOP, WAKE, VoiceModel, WakeListener, installed, samples


@pytest.fixture(autouse=True)
def data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("ARCEE_DATA_DIR", str(tmp_path))
    return tmp_path


def archive(files: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(f"model-dir/{name}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return bz2.compress(raw.getvalue())


MODEL = VoiceModel(
    "test", "for tests", "https://example.test/m.tar.bz2", ("a.onnx", "tokens.txt"), 1
)


async def test_a_download_keeps_only_the_files_used() -> None:
    body = archive({"a.onnx": b"A", "tokens.txt": b"T", "a.fp32.onnx": b"big", "test.wav": b"W"})
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=body))
    )
    seen: list[float] = []

    await voice_local.download(MODEL, seen.append, client=client)

    folder = voice_local.model_path(MODEL)
    assert sorted(p.name for p in folder.iterdir()) == ["a.onnx", "tokens.txt"]
    assert installed(MODEL)
    assert seen[-1] == 1.0
    await client.aclose()


async def test_an_archive_without_the_files_is_refused() -> None:
    body = archive({"other.onnx": b"X"})
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=body))
    )

    with pytest.raises(VoiceError, match="did not hold the expected files"):
        await voice_local.download(MODEL, client=client)
    await client.aclose()


def test_samples_are_scaled_to_one() -> None:
    pcm = array.array("h", [0, 16384, -32768]).tobytes()
    assert samples(pcm) == [0.0, 0.5, -1.0]


# --- the listener, with stand-ins for the spotter and the voice detector ----------------


class Spotter:
    """Reports a keyword after the given frame numbers."""

    def __init__(self, keywords: dict[int, str]) -> None:
        self.keywords = keywords
        self.frame = 0
        self.ready = False

    def create_stream(self) -> Spotter:
        return self

    def accept_waveform(self, rate: int, frame: list[float]) -> None:
        self.frame += 1
        self.ready = True

    def is_ready(self, _stream: Any) -> bool:
        ready, self.ready = self.ready, False
        return ready

    def decode_stream(self, _stream: Any) -> None:
        pass

    def get_result(self, _stream: Any) -> str:
        return self.keywords.get(self.frame, "")

    def reset_stream(self, _stream: Any) -> None:
        pass


class Segment:
    def __init__(self, values: list[float]) -> None:
        self.samples = values


class Vad:
    """Ends an utterance after `speech_frames` frames of the request, if any."""

    def __init__(self, speech_frames: int | None, talking: bool = True) -> None:
        self.speech_frames = speech_frames
        self.seen: list[float] = []
        self.done = False
        self.talking = talking  # before the wake word: does the room hold speech?

    def accept_waveform(self, frame: list[float]) -> None:
        self.seen += frame
        if self.speech_frames is not None and len(self.seen) >= self.speech_frames * FRAME:
            self.done = True

    def empty(self) -> bool:
        return not self.done

    @property
    def front(self) -> Segment:
        return Segment(self.seen)

    def pop(self) -> None:
        self.done = False
        self.seen = []

    def is_speech_detected(self) -> bool:
        return self.talking

    def reset(self) -> None:
        self.seen = []
        self.talking = self.speech_frames is not None  # after the wake word: a request?

    def flush(self) -> None:
        pass


def audio(frames: int) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(array.array("h", [1000] * FRAME * frames).tobytes())
    reader.feed_eof()
    return reader


def listener(events: list[Any]) -> WakeListener:
    return WakeListener(
        on_wake=lambda: events.append("wake"),
        on_request=lambda pcm: events.append(("request", len(pcm))),
        on_stop=lambda: events.append("stop"),
    )


async def test_hey_arcee_then_a_request() -> None:
    events: list[Any] = []

    await listener(events)._listen(audio(10), Spotter({2: WAKE}), Vad(speech_frames=3))

    assert events == ["wake", ("request", 3 * FRAME * 2)]


async def test_arcee_stop_needs_no_request() -> None:
    events: list[Any] = []

    await listener(events)._listen(audio(5), Spotter({3: STOP}), Vad(speech_frames=None))

    assert events == ["stop"]


async def test_waking_and_saying_nothing_ends_the_request_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_local, "NO_REQUEST_S", 2 * FRAME / 16000)
    events: list[Any] = []

    await listener(events)._listen(audio(6), Spotter({1: WAKE}), Vad(speech_frames=None))

    assert events == ["wake", ("request", 0)]


async def test_a_paused_listener_hears_nothing() -> None:
    events: list[Any] = []
    paused = listener(events)
    paused.paused = True  # the mic button is recording

    await paused._listen(audio(5), Spotter({2: WAKE}), Vad(speech_frames=1))

    assert events == []


async def test_the_spotter_sleeps_while_no_one_speaks() -> None:
    # The VAD gates the spotter: in a quiet room it is never fed (3.5% of a core saved).
    events: list[Any] = []
    spotter = Spotter({2: WAKE})

    await listener(events)._listen(audio(10), spotter, Vad(speech_frames=3, talking=False))

    assert spotter.frame == 0
    assert events == []


async def test_speech_reaches_the_spotter_with_what_came_just_before() -> None:
    events: list[Any] = []
    spotter = Spotter({})
    vad = Vad(speech_frames=None, talking=False)
    reader = asyncio.StreamReader()
    reader.feed_data(array.array("h", [1000] * FRAME * 4).tobytes())

    task = asyncio.ensure_future(listener(events)._listen(reader, spotter, vad))
    await asyncio.sleep(0.01)
    vad.talking = True  # the VAD becomes sure of speech on the fifth frame
    reader.feed_data(array.array("h", [1000] * FRAME).tobytes())
    reader.feed_eof()
    await task

    assert spotter.frame == 5  # the four quiet frames before it, then the fifth


class Trained:
    """A stand-in trained wake word: heard at its `after`th check, and only then."""

    def __init__(self, after: int, said: str = "wake") -> None:
        self.after = after
        self.said = said
        self.checks = 0
        self.shortest = 1
        self.window = 80

    threshold = 0.2
    stops: ClassVar[list[Any]] = []

    def distances(self, recent: list[list[float]]) -> tuple[float, float]:
        self.checks += 1
        hit = self.checks == self.after
        return (
            0.0 if hit and self.said == "wake" else 0.25,
            0.0 if hit and self.said == "stop" else 1.0,
        )

    def span(self, recent: list[list[float]]) -> list[list[float]]:
        return recent

    def judge(self, wake: float, stop: float) -> str:
        return "stop" if stop == 0.0 else "wake" if wake == 0.0 else ""


async def test_a_trained_wake_word_wakes_when_the_spotter_does_not() -> None:
    events: list[Any] = []
    woken = listener(events)
    woken.trained = Trained(after=2)  # pyright: ignore[reportAttributeAccessIssue]

    await woken._listen(audio(12), Spotter({}), Vad(speech_frames=3))

    assert events == ["wake", ("request", 3 * FRAME * 2)]


async def test_a_trained_wake_word_is_only_checked_while_someone_speaks() -> None:
    events: list[Any] = []
    quiet = listener(events)
    trained = Trained(after=1)
    quiet.trained = trained  # pyright: ignore[reportAttributeAccessIssue]

    await quiet._listen(audio(10), Spotter({}), Vad(speech_frames=3, talking=False))

    assert trained.checks == 0
    assert events == []


async def test_a_phrase_is_recorded_until_the_speaker_stops() -> None:
    script = "import sys; sys.stdout.buffer.write(b'\\x10\\x00' * 1280 * 6)"
    vad = Vad(speech_frames=4)
    levels: list[float] = []

    phrase = await voice_local.record_phrase(
        levels.append, command=("python3", "-c", script), vad=vad
    )

    assert len(phrase) == 4 * FRAME
    assert len(levels) == 4


async def test_no_phrase_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_local, "PHRASE_WAIT_S", 2 * FRAME / 16000)
    script = "import sys; sys.stdout.buffer.write(b'\\x00\\x00' * 1280 * 6)"

    with pytest.raises(VoiceError, match="did not hear anything"):
        await voice_local.record_phrase(
            command=("python3", "-c", script), vad=Vad(speech_frames=None, talking=False)
        )


async def test_the_missing_models_download_with_progress_over_all_of_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[str] = []

    async def download(model: VoiceModel, on_progress: Any) -> None:
        on_progress(0.5)
        fetched.append(model.name)

    monkeypatch.setattr(voice_local, "download", download)
    seen: list[float] = []

    await voice_local.download_missing(seen.append)

    assert fetched == ["wake", "vad", "denoise"]
    assert seen[0] == pytest.approx(3 / 8)  # half of the 6 MB, of 8 MB in all
    assert seen[-1] == 1.0


async def test_a_trained_arcee_stop_stops() -> None:
    events: list[Any] = []
    stopping = listener(events)
    stopping.trained = Trained(after=2, said="stop")  # pyright: ignore[reportAttributeAccessIssue]

    await stopping._listen(audio(8), Spotter({}), Vad(speech_frames=None))

    assert events == ["stop"]


async def test_a_request_being_heard_can_be_dropped() -> None:
    events: list[Any] = []
    woken = listener(events)
    reader = asyncio.StreamReader()
    reader.feed_data(array.array("h", [1000] * FRAME * 3).tobytes())
    task = asyncio.ensure_future(woken._listen(reader, Spotter({2: WAKE}), Vad(speech_frames=6)))
    await asyncio.sleep(0.01)
    assert woken.drop()  # Esc while the request is being said

    reader.feed_data(array.array("h", [1000] * FRAME * 8).tobytes())
    reader.feed_eof()
    await task

    assert events == ["wake"]  # no request: nothing is transcribed or run
    assert not woken.drop()  # nothing being heard now


class Counting(Vad):
    """A VAD that counts the frames it is given."""

    def __init__(self, speech_frames: int | None, talking: bool = True) -> None:
        super().__init__(speech_frames, talking)
        self.fed = 0

    def accept_waveform(self, frame: list[float]) -> None:
        self.fed += 1
        super().accept_waveform(frame)


def frames_of(level: int, n: int) -> bytes:
    return array.array("h", [level, -level] * (FRAME // 2) * n).tobytes()


async def test_the_vad_sleeps_through_a_steady_hiss_and_wakes_for_speech() -> None:
    # 8 s of the user's mic hiss (about 0.02): the gate learns it in a few seconds and
    # the VAD sleeps; then speech ten times louder wakes it, with what came just before.
    events: list[Any] = []
    hiss = Counting(speech_frames=None, talking=False)
    reader = asyncio.StreamReader()
    reader.feed_data(frames_of(700, 100))
    reader.feed_eof()
    await listener(events)._listen(reader, None, hiss)
    assert hiss.fed < 75  # the last seconds of hiss were not heard by the VAD

    both = Counting(speech_frames=None, talking=False)
    reader = asyncio.StreamReader()
    reader.feed_data(frames_of(700, 100) + frames_of(9000, 5))
    reader.feed_eof()
    await listener(events)._listen(reader, None, both)
    assert both.fed - hiss.fed >= 5 + voice_local.PREROLL_FRAMES - 1  # speech, and before it


async def test_no_spotter_once_both_phrases_are_trained() -> None:
    trained_both = listener([])
    trained_both.trained = wake_match.Trained([[[1.0]]], 0.2, [[[1.0]]], 0.2)
    assert not trained_both.needs_spotter
    wake_only = listener([])
    wake_only.trained = wake_match.Trained([[[1.0]]], 0.2)
    assert wake_only.needs_spotter
    assert listener([]).needs_spotter  # not trained: the spotter hears it


def test_a_near_miss_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    near = listener([])
    near.trained = wake_match.Trained([[[1.0]]], 0.2)
    with caplog.at_level(logging.INFO, logger="arcee.voice_local"):
        near._near_miss(0.25)  # pyright: ignore[reportPrivateUsage]
        near._near_miss(0.9)  # nowhere near: not logged  # pyright: ignore[reportPrivateUsage]
    assert [r.getMessage() for r in caplog.records] == [
        "near miss: 0.250 from the wake word (it wakes at 0.200)"
    ]


async def test_knocks_and_typing_do_not_raise_the_room_level() -> None:
    # The user adjusting the laptop raised the old room level to 0.4, and a normal
    # "hey Arcee" no longer reached the VAD. The level is now the quietest moment.
    before = frames_of(700, 130) + frames_of(20000, 60) + frames_of(700, 30)  # 5 s of knocks
    counted: list[int] = []
    for tail in (b"", frames_of(2100, 5)):  # then a voice three times the hiss
        vad = Counting(speech_frames=None, talking=False)
        reader = asyncio.StreamReader()
        reader.feed_data(before + tail)
        reader.feed_eof()
        await listener([])._listen(reader, None, vad)
        counted.append(vad.fed)
    assert counted[1] - counted[0] >= 5  # the voice still woke the VAD


class Denoiser:
    """sherpa-onnx's OnlineSpeechDenoiser, halving what it hears and holding back 10."""

    def __init__(self) -> None:
        self.resets = 0
        self.held: list[float] = []

    def reset(self) -> None:
        self.resets += 1
        self.held = []

    def run(self, piece: list[float], rate: int) -> Any:
        self.held += [v / 2 for v in piece]
        out, self.held = self.held[:-10], self.held[-10:]
        return SimpleNamespace(samples=out)

    def flush(self) -> Any:
        out, self.held = self.held, []
        return SimpleNamespace(samples=out)


def test_a_cleaner_cleans_a_whole_phrase_to_its_end() -> None:
    phrase = [0.5] * (FRAME * 2 + 300)
    cleaned = voice_local.Cleaner(Denoiser()).whole(phrase)
    assert cleaned == [0.25] * len(phrase)
    assert voice_local.Cleaner(None).whole(phrase) is phrase  # no model: as it is


async def test_the_wake_word_is_matched_on_cleaned_speech(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[list[float]] = []
    real = wake_match.Features

    class Features(real):
        def push(self, samples: list[float]) -> None:
            pushed.append(samples)
            super().push(samples)

    monkeypatch.setattr(wake_match, "Features", Features)
    events: list[Any] = []
    woken = listener(events)
    woken.trained = Trained(after=2)  # pyright: ignore[reportAttributeAccessIssue]
    denoiser = Denoiser()

    await woken._listen(audio(12), Spotter({}), Vad(speech_frames=3), voice_local.Cleaner(denoiser))

    assert "wake" in events
    assert denoiser.resets >= 1  # each stretch of speech starts clean
    heard = [v for piece in pushed for v in piece]
    assert heard
    assert set(heard) == {samples(array.array("h", [1000]).tobytes())[0] / 2}  # cleaned
