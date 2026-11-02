"""Local voice (decision 0015): model downloads, and the wake-word listener's flow."""

import array
import asyncio
import bz2
import io
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from arcee import voice_local
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

    def __init__(self, speech_frames: int | None) -> None:
        self.speech_frames = speech_frames
        self.seen: list[float] = []
        self.done = False

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
        return self.speech_frames is not None

    def reset(self) -> None:
        self.seen = []

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
