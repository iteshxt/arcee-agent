"""Speaking a request: record from the microphone, transcribe with Groq Whisper.

The panel's mic button starts a recording here (through arceed). PipeWire's
`pw-record` streams 16 kHz mono samples; each 50 ms chunk's loudness goes to the
panel for its waveform. The recording ends when the user clicks again, or by
itself after a pause once they have spoken, and Groq's `whisper-large-v3-turbo`
turns it into text. Speech uses its own key, GROQ_API_KEY_TTS, so it never eats
into the chat models' budget. The audio leaves the machine for Groq; nothing is
kept on disk.
"""

import asyncio
import contextlib
import io
import math
import os
import struct
import wave
from collections.abc import Callable
from typing import Final

import httpx

KEY_ENV: Final = "GROQ_API_KEY_TTS"
TRANSCRIBE_URL: Final = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL: Final = "whisper-large-v3-turbo"
RATE: Final = 16_000
CHUNK_S: Final = 0.05
CHUNK_BYTES: Final = int(RATE * CHUNK_S) * 2  # 16-bit mono
# Loudness (0..1 RMS) above which a chunk counts as speech.
SPEECH_LEVEL: Final = 0.02
# After speech, this much quiet ends the recording; with no speech, this much ends it.
END_SILENCE_S: Final = 1.5
NO_SPEECH_S: Final = 8.0
MAX_SECONDS: Final = 60.0
RECORD_COMMAND: Final = (
    "pw-record",
    "--raw",
    "--rate",
    str(RATE),
    "--channels",
    "1",
    "--format",
    "s16",
    "-",
)


class VoiceError(RuntimeError):
    """Recording or transcription failed, in words for the user."""


def level(chunk: bytes) -> float:
    """Root-mean-square loudness of 16-bit samples, 0 (silence) to 1."""
    count = len(chunk) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", chunk[: count * 2])
    return math.sqrt(sum(s * s for s in samples) / count) / 32768


def to_wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return out.getvalue()


class EndPointer:
    """Decides when a recording is over, chunk by chunk."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.spoke = False
        self._quiet = 0.0

    def done(self, chunk_level: float) -> bool:
        self.elapsed += CHUNK_S
        if chunk_level >= SPEECH_LEVEL:
            self.spoke, self._quiet = True, 0.0
        else:
            self._quiet += CHUNK_S
        if self.elapsed >= MAX_SECONDS:
            return True
        return self._quiet >= (END_SILENCE_S if self.spoke else NO_SPEECH_S)


class Recorder:
    """One recording from the default microphone, until `stop()` or a pause."""

    def __init__(
        self,
        on_level: Callable[[float], None],
        command: tuple[str, ...] = RECORD_COMMAND,
    ) -> None:
        self._on_level = on_level
        self._command = command
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def record(self) -> tuple[bytes, bool]:
        """The audio, and whether anyone spoke."""
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise VoiceError("pw-record is missing (PipeWire's tools)") from exc
        assert process.stdout is not None
        audio = bytearray()
        ender = EndPointer()
        try:
            while not self._stop.is_set():
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.readexactly(CHUNK_BYTES), timeout=2
                    )
                except asyncio.IncompleteReadError, TimeoutError:
                    break
                audio += chunk
                loudness = level(chunk)
                self._on_level(loudness)
                if ender.done(loudness):
                    break
        finally:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            await process.wait()
        return bytes(audio), ender.spoke


class Transcriber:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def transcribe(self, pcm: bytes) -> str:
        key = os.environ.get(KEY_ENV)
        if not key:
            raise VoiceError(f"set {KEY_ENV} in .env to speak to Arcee")
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            response = await client.post(
                TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {key}"},
                data={"model": MODEL, "response_format": "json", "temperature": "0"},
                files={"file": ("speech.wav", to_wav(pcm), "audio/wav")},
            )
        except httpx.HTTPError as exc:
            raise VoiceError(f"could not reach Groq: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code != 200:
            raise VoiceError(f"transcription failed (HTTP {response.status_code})")
        return str(response.json().get("text", "")).strip()
