"""The wake word as this user says it: trained from their voice, matched by its sound.

The keyword spotter (voice_local) knows English, and "Arcee" is not English: with some
voices and mics it never hears it (decision 0015, amended). Training fixes that, the way
phone assistants do: the user says "hey Arcee" three times and one ordinary sentence.
What is kept is the sound of the three (log-mel frames, 24 numbers per 20 ms), not the
audio: it cannot be played back. Speech then wakes Arcee when it holds a stretch close
enough to one of them, by dynamic time warping, so it allows for saying it faster or
slower. The ordinary sentence sets how close is close enough.

Plain Python, no numpy: about 3% of a core for the features and 4% for the matching,
only while someone speaks (the listener's VAD gate).
"""

import json
import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from arcee.voice import RATE, VoiceError

FFT: Final = 512
WIN: Final = 400  # 25 ms
HOP: Final = 320  # 20 ms: a frame of the listener's (80 ms) is four of these
BINS: Final = 24
WINDOW: Final = 80  # frames matched against, at least: the last 1.6 s of speech
SAMPLES: Final = 3  # "hey Arcee"s asked for when training
STOP_SAMPLES: Final = 2  # and "Arcee stop"s
# A trained wake word is never looser than this, whatever the samples suggest.
LOOSEST: Final = 0.3
SHORTEST_S: Final = 0.3
LONGEST_S: Final = 2.5

Frames = list[list[float]]


def _mel(hz: float) -> float:
    return 1127 * math.log(1 + hz / 700)


def _filters() -> list[list[tuple[int, float]]]:
    """Triangular mel filters over the FFT's bins, 60 Hz to 7.6 kHz, as (bin, weight)."""
    low, high = _mel(60), _mel(7600)
    points = [low + (high - low) * i / (BINS + 1) for i in range(BINS + 2)]
    bins = [700 * (math.exp(m / 1127) - 1) * FFT / RATE for m in points]
    filters: list[list[tuple[int, float]]] = []
    for k in range(BINS):
        a, b, c = bins[k], bins[k + 1], bins[k + 2]
        row: list[tuple[int, float]] = []
        for j in range(FFT // 2 + 1):
            weight = (j - a) / (b - a) if a <= j <= b else (c - j) / (c - b) if b < j <= c else 0
            if weight > 0:
                row.append((j, weight))
        filters.append(row)
    return filters


_FILTERS: Final = _filters()
_HAMMING: Final = [0.54 - 0.46 * math.cos(2 * math.pi * i / (WIN - 1)) for i in range(WIN)]
_BITS: Final = FFT.bit_length() - 1
_REVERSED: Final = [int(f"{i:0{_BITS}b}"[::-1], 2) for i in range(FFT)]
_TWIDDLE: Final = [
    complex(math.cos(-2 * math.pi * k / FFT), math.sin(-2 * math.pi * k / FFT))
    for k in range(FFT // 2)
]


def _fft(x: list[float]) -> list[complex]:
    a = [complex(x[_REVERSED[i]]) for i in range(FFT)]
    size = 2
    while size <= FFT:
        half, step = size // 2, FFT // size
        for start in range(0, FFT, size):
            for k in range(half):
                t = _TWIDDLE[k * step] * a[start + k + half]
                u = a[start + k]
                a[start + k], a[start + k + half] = u + t, u - t
        size *= 2
    return a


def _frame(window: list[float]) -> list[float]:
    """One frame's log-mel energies."""
    mean = sum(window) / WIN
    centred = [v - mean for v in window]
    emphasised = [
        (centred[i] - 0.97 * centred[i - 1 if i else 0]) * _HAMMING[i] for i in range(WIN)
    ]
    spectrum = _fft(emphasised + [0.0] * (FFT - WIN))
    power = [c.real * c.real + c.imag * c.imag for c in spectrum[: FFT // 2 + 1]]
    return [math.log(sum(power[j] * w for j, w in row) + 1e-6) for row in _FILTERS]


class Features:
    """Log-mel frames of a stream of samples, the last `keep` of them."""

    def __init__(self, keep: int = WINDOW) -> None:
        self.frames: deque[list[float]] = deque(maxlen=keep)
        self._pending: list[float] = []

    def push(self, samples: list[float]) -> None:
        self._pending += samples
        while len(self._pending) >= WIN:
            self.frames.append(_frame(self._pending[:WIN]))
            del self._pending[:HOP]


def log_mel(samples: list[float]) -> Frames:
    features = Features(keep=len(samples) // HOP + 1)
    features.push(samples)
    return list(features.frames)


def normalise(frames: Frames) -> Frames:
    """Each band less its mean over `frames` (the mic's colour), each frame to length 1."""
    if not frames:
        return []
    means = [sum(f[k] for f in frames) / len(frames) for k in range(BINS)]
    out: Frames = []
    for f in frames:
        centred = [v - m for v, m in zip(f, means, strict=True)]
        length = math.sqrt(sum(v * v for v in centred)) or 1.0
        out.append([v / length for v in centred])
    return out


def distance(template: Frames, query: Frames) -> float:
    """How far `template` is from its best match anywhere in `query`: 0 is the same.

    Subsequence dynamic time warping: the whole template, against any stretch of the
    query, by cosine distance per frame, averaged along the alignment.
    """
    if not template or not query:
        return 1.0
    big = 1e9
    width = len(query)
    previous = [0.0] * (width + 1)  # the template may start anywhere in the query
    previous_steps = [0] * (width + 1)
    for t in template:
        current = [big] * (width + 1)
        steps = [0] * (width + 1)
        for j in range(1, width + 1):
            q = query[j - 1]
            cost = 1 - sum(a * b for a, b in zip(t, q, strict=True))
            best, length = min(
                (previous[j - 1], previous_steps[j - 1]),
                (previous[j], previous_steps[j]),
                (current[j - 1], steps[j - 1]),
            )
            current[j] = best + cost
            steps[j] = length + 1
        previous, previous_steps = current, steps
    return min(previous[j] / previous_steps[j] for j in range(1, width + 1) if previous_steps[j])


@dataclass(frozen=True, slots=True)
class Trained:
    """The user's wake word (and "Arcee stop", when trained): the sound of their samples,
    and how close counts."""

    templates: list[Frames]
    threshold: float
    stops: list[Frames] = field(default_factory=list[Frames])
    stop_threshold: float = 0.0

    def closest(self, query: Frames) -> float:
        return min(distance(t, query) for t in self.templates)

    def match(self, recent: Frames) -> str:
        """ "wake", "stop" or "" for the speech just heard."""
        query = normalise(recent)
        wake = self.closest(query)
        if self.stops:
            stop = min(distance(t, query) for t in self.stops)
            if stop <= self.stop_threshold and stop < wake:
                return "stop"
        return "wake" if wake <= self.threshold else ""

    def heard(self, recent: Frames) -> bool:
        return self.match(recent) == "wake"

    @property
    def shortest(self) -> int:
        return min(len(t) for t in [*self.templates, *self.stops])

    @property
    def window(self) -> int:
        """Frames of speech to match against: room for the longest sample (said slowly,
        or "hey… Arcee" with a pause) and a little either side."""
        return max(WINDOW, max(len(t) for t in [*self.templates, *self.stops]) + 25)


@dataclass(frozen=True, slots=True)
class Training:
    """How training went, to show the user."""

    trained: Trained
    spread: float  # how far apart the samples were (the furthest one from the rest)
    other: float  # how close the ordinary sentence came
    stop_spread: float = 0.0  # the same for "Arcee stop", when trained
    stop_other: float = 0.0


def _check_lengths(samples: list[list[float]], phrase: str) -> None:
    for sample in samples:
        seconds = len(sample) / RATE
        if seconds < SHORTEST_S:
            raise VoiceError(f"one of the samples was too short: say the whole {phrase}")
        if seconds > LONGEST_S:
            raise VoiceError(f"one of the samples was too long: say just {phrase}, then stop")


def _spread(templates: list[Frames]) -> float:
    """Each sample against the rest, as the listener will hear it: by its closest."""
    return max(min(distance(t, held) for t in templates if t is not held) for held in templates)


def _nearest(templates: list[Frames], speech: Frames, window: int) -> float:
    """How close `templates` come to any stretch of `speech` the listener would keep."""
    return min(
        min(distance(t, normalise(speech[i : i + window])) for t in templates)
        for i in range(0, max(1, len(speech) - window + 1), WINDOW // 4)
    )


def train(
    samples: list[list[float]], other: list[float], stops: list[list[float]] | None = None
) -> Training:
    """The wake word from the user's `samples` of it (and "Arcee stop" from `stops`),
    kept apart from `other` speech and from each other."""
    stops = stops or []
    _check_lengths(samples, "“Hey Arcee”")
    _check_lengths(stops, "“Arcee stop”")
    templates = [normalise(log_mel(s)) for s in samples]
    stop_templates = [normalise(log_mel(s)) for s in stops]
    spread = _spread(templates)
    if spread > LOOSEST:
        raise VoiceError("the samples sounded too different: say it the same way each time")
    window = Trained(templates, LOOSEST, stop_templates).window
    sentence = log_mel(other)
    near = _nearest(templates, sentence, window)
    if near <= spread:
        raise VoiceError("the sentence sounded like “Hey Arcee”: try a different sentence")
    stop_spread = stop_near = 0.0
    stop_threshold = 0.0
    if stop_templates:
        stop_spread = _spread(stop_templates)
        if stop_spread > LOOSEST:
            raise VoiceError("the “Arcee stop” samples sounded too different: say it the same way")
        # "Arcee stop" must not wake Arcee, nor "Hey Arcee" stop it.
        crossed = min(min(distance(t, s) for t in templates) for s in stop_templates)
        near = min(near, crossed)
        stop_near = min(
            _nearest(stop_templates, sentence, window),
            min(min(distance(t, s) for t in stop_templates) for s in templates),
        )
        if near <= spread or stop_near <= stop_spread:
            raise VoiceError("“Hey Arcee” and “Arcee stop” sounded too alike: say each clearly")
        stop_threshold = min(LOOSEST, stop_spread + 0.4 * (stop_near - stop_spread))
    threshold = min(LOOSEST, spread + 0.4 * (near - spread))
    trained = Trained(templates, round(threshold, 4), stop_templates, round(stop_threshold, 4))
    return Training(trained, spread, near, stop_spread, stop_near)


def trained_path() -> Path:
    from arcee.voice_local import models_dir

    return models_dir() / "my-wake-word.json"


def save(trained: Trained) -> None:
    path = trained_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    def rounded(templates: list[Frames]) -> list[Frames]:
        return [[[round(v, 4) for v in f] for f in t] for t in templates]

    data = {
        "threshold": trained.threshold,
        "templates": rounded(trained.templates),
        "stop_threshold": trained.stop_threshold,
        "stops": rounded(trained.stops),
    }
    path.write_text(json.dumps(data))


def load() -> Trained | None:
    """The user's trained wake word, if they trained one."""
    try:
        data = json.loads(trained_path().read_text())

        def frames(templates: list[list[list[float]]]) -> list[Frames]:
            return [[list(map(float, f)) for f in t] for t in templates]

        return Trained(
            frames(data["templates"]),
            float(data["threshold"]),
            frames(data.get("stops", [])),  # trained before "Arcee stop" was: none
            float(data.get("stop_threshold", 0.0)),
        )
    except OSError, ValueError, KeyError, TypeError:
        return None


def forget() -> None:
    trained_path().unlink(missing_ok=True)


async def hold_wake(hold: bool) -> None:
    """Ask arceed to stop hearing the wake word while it is trained (or to hear it again,
    with what was trained). Nothing to do when arceed is not running."""
    from dbus_fast import BusType, Message, MessageFlag
    from dbus_fast.aio import MessageBus
    from dbus_fast.errors import DBusError

    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except OSError, DBusError:
        return
    try:
        await bus.call(
            Message(
                destination="me.iteshxt.Arcee.Agent",
                path="/me/iteshxt/Arcee/Agent",
                interface="me.iteshxt.Arcee.Agent",
                member="HoldWake",
                signature="b",
                body=[hold],
                flags=MessageFlag.NO_AUTOSTART,  # not running: nothing to hold
            )
        )
    finally:
        bus.disconnect()
