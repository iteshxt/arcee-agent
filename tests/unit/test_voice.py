import asyncio
import io
import struct
import sys
import wave
from pathlib import Path

import httpx
import pytest

from arcee.voice import (
    CHUNK_BYTES,
    CHUNK_S,
    END_SILENCE_S,
    MODEL,
    EndPointer,
    Recorder,
    SpeechUsage,
    Transcriber,
    VoiceError,
    level,
    to_wav,
)


def tone(amplitude: int, chunks: int = 1) -> bytes:
    count = CHUNK_BYTES // 2
    return struct.pack(f"<{count}h", *([amplitude, -amplitude] * (count // 2))) * chunks


def test_level_is_rms_loudness() -> None:
    assert level(tone(0)) == 0
    assert level(tone(16384)) == pytest.approx(0.5)


def test_wav_wraps_the_samples() -> None:
    with wave.open(io.BytesIO(to_wav(tone(100))), "rb") as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 16000, 2)


def test_a_pause_after_speech_ends_the_recording() -> None:
    ender = EndPointer()
    assert not ender.done(0.3)  # speech
    quiet_chunks = round(END_SILENCE_S / CHUNK_S)
    assert not any(ender.done(0.0) for _ in range(quiet_chunks - 1))
    assert ender.done(0.0)
    assert ender.spoke


async def test_recorder_reads_the_stream_and_reports_levels() -> None:
    # A stand-in for pw-record: two loud chunks, then silence until stopped.
    script = (
        "import sys, struct, time\n"
        f"loud = {tone(16384, 2)!r}\n"
        f"quiet = {tone(0)!r}\n"
        "sys.stdout.buffer.write(loud); sys.stdout.flush()\n"
        "while True:\n"
        "    sys.stdout.buffer.write(quiet); sys.stdout.flush(); time.sleep(0.005)\n"
    )
    levels: list[float] = []
    recorder = Recorder(levels.append, command=(sys.executable, "-c", script))

    audio, spoke = await asyncio.wait_for(recorder.record(), 10)

    assert spoke
    assert levels[:2] == [pytest.approx(0.5), pytest.approx(0.5)]
    assert len(audio) == len(levels) * CHUNK_BYTES


async def test_transcription_uses_whisper_with_the_speech_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GROQ_API_KEY_TTS", "speech-key")
    seen: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": " open chrome and run youtube "})

    client = httpx.AsyncClient(transport=httpx.MockTransport(server))

    text = await Transcriber(client, SpeechUsage(tmp_path / "speech.json")).transcribe(tone(100))

    assert text == "open chrome and run youtube"
    assert seen[0].headers["authorization"] == "Bearer speech-key"
    body = seen[0].content
    assert MODEL.encode() in body
    assert b"speech.wav" in body


async def test_no_speech_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY_TTS", raising=False)

    with pytest.raises(VoiceError, match="set GROQ_API_KEY_TTS"):
        await Transcriber().transcribe(tone(100))


def test_speech_usage_counts_the_last_day_and_hour(tmp_path: Path) -> None:
    clock = [1_000_000.0]
    usage = SpeechUsage(tmp_path / "speech.json", now=lambda: clock[0])

    usage.record(3.0)  # a short clip counts as 10 s
    clock[0] += 2 * 3600
    usage.record(42.0)
    clock[0] += 23 * 3600  # the first clip is now over a day old

    assert usage.snapshot() == {
        "audio_s_day": 42,
        "audio_s_day_limit": 28_800,
        "audio_s_hour": 0,
        "audio_s_hour_limit": 7_200,
        "requests_day": 1,
        "requests_day_limit": 2_000,
    }


async def test_a_transcription_is_counted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GROQ_API_KEY_TTS", "k")
    usage = SpeechUsage(tmp_path / "speech.json")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "hi"}))
    )

    await Transcriber(client, usage).transcribe(tone(100, chunks=400))  # 20 s of audio

    assert usage.snapshot()["audio_s_day"] == 20
