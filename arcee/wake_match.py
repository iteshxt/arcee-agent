"""The wake word as this user says it: trained from their voice, matched by its sound.

The keyword spotter (voice_local) knows English, and "Arcee" is not English: with some
voices and mics it never hears it (decision 0015, amended). Training fixes that, the way
phone assistants do: the user says "hey Arcee" three times and one ordinary sentence.
What is kept is the sound of the three (log-mel frames, 24 numbers per 20 ms), not the
audio: it cannot be played back. Speech then wakes Arcee when it holds a stretch close
enough to one of them, by dynamic time warping, so it allows for saying it faster or
slower. The ordinary sentence sets how close is close enough.

The same samples serve a quiet room, a fan and a crowd: what is matched is denoised
first (voice_local.Cleaner), and each band is held above a floor under the loudest
(`normalise`), so what is left of the room cannot make the word sound different.

Plain Python, no numpy: about 3% of a core for the features and 10% for the matching,
only while someone speaks (the listener's VAD gate).
"""

import json
import math
import operator
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
# "Hey Arcee"s asked for when training. Five (2026-11-04) gave more chances for one
# sample unlike the rest, which loosened the threshold; back to three (2026-11-08).
SAMPLES: Final = 3
STOP_SAMPLES: Final = 2  # and "Arcee stop"s
# Samples learned from wakes the user went on to use: at most this many, newest kept.
MAX_LEARNED: Final = 3
# Learning from wakes is off (2026-11-08): with false wakes still coming, a wake's
# words did not show it was meant, and samples learned from them pulled the wake word
# toward ordinary speech. Learned samples are neither kept nor used while it is off.
LEARNING: Final = False
# Where between the samples' own spread and the ordinary sentence the threshold sits.
# 0.4 let a stretch of the user's talk wake Arcee at 0.098 among real wakes at
# 0.084-0.108 (2026-11-08); 0.25 would have kept it out.
CALIBRATION: Final = 0.25
# A trained wake word is never looser than this, whatever the samples suggest.
LOOSEST: Final = 0.3
SHORTEST_S: Final = 0.3
LONGEST_S: Final = 2.5
# Bands more than this below the loudest (natural log of power: 17 dB) are held at it.
# Whatever noise the denoiser leaves sits down there, different in every room; held to
# one level it looks the same in all of them. Measured on the user's samples with hiss,
# fan noise and babble mixed in (decision 0015): 4 kept their own distance under 0.07
# down to 5 dB of signal over noise, 7 let it rise to 0.23 in babble, and none at all
# left it at 0.6.
FLOOR: Final = 4.0
# How the kept samples were made; samples kept another way are trained again.
FEATURES: Final = 2  # 2: denoised and floored

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
    """Each band held above FLOOR under the loudest, less its mean over `frames` (the
    mic's colour), each frame to length 1."""
    if not frames:
        return []
    low = max(max(f) for f in frames) - FLOOR
    frames = [[v if v > low else low for v in f] for f in frames]
    means = [sum(f[k] for f in frames) / len(frames) for k in range(BINS)]
    out: Frames = []
    for f in frames:
        centred = [v - m for v, m in zip(f, means, strict=True)]
        length = math.sqrt(sum(v * v for v in centred)) or 1.0
        out.append([v / length for v in centred])
    return out


def pooled(frames: Frames) -> Frames:
    """Frames two by two (40 ms), each pair summed and made length 1 again: matching
    at 40 ms does a quarter of the work of 20 ms (172 ms a check with ten samples
    became 20 ms) and a one-second "hey Arcee" is still 25 points."""
    out: Frames = []
    for i in range(0, len(frames) - 1, 2):
        pair = [a + b for a, b in zip(frames[i], frames[i + 1], strict=True)]
        length = math.sqrt(sum(v * v for v in pair)) or 1.0
        out.append([v / length for v in pair])
    return out


def _subsequence(template: Frames, query: Frames) -> tuple[float, int, int]:
    """Subsequence dynamic time warping of pooled frames, its speed held between half
    and twice the template's: the score, and query[start:end].

    Steps (1,1), (2,1) and (1,2) only. Free warping let a whole "hey Arcee" fold onto a
    moment of anyone's speech (a single frame could stand for half the word); held to
    0.5x-2x, it has to be said at about the speed it was trained at.
    """
    n, m = len(template), len(query)
    if n < 2 or m < 2:
        return 1.0, 0, 0
    big = 1e9
    mul = operator.mul

    def costs(t: list[float]) -> list[float]:
        return [1.0 - sum(map(mul, t, q, strict=True)) for q in query]  # both length 1

    cost_row = costs(template[0])
    # Row 0: the template may start anywhere in the query.
    row, steps, starts = list(cost_row), [1] * m, list(range(m))
    older: tuple[list[float], list[int], list[int]] | None = None
    for i in range(1, n):
        cost_next = costs(template[i])
        new, new_steps, new_starts = [big] * m, [0] * m, [0] * m
        for j in range(1, m):
            c = cost_next[j]
            best_d, best_l, best_s = row[j - 1] + c, steps[j - 1] + 1, starts[j - 1]
            score = best_d / best_l
            if j >= 2 and row[j - 2] < big:  # the query runs ahead: twice as slow
                d, length = row[j - 2] + cost_next[j - 1] + c, steps[j - 2] + 2
                if d / length < score:
                    best_d, best_l, best_s, score = d, length, starts[j - 2], d / length
            if older is not None and older[0][j - 1] < big:  # the template runs ahead
                d = older[0][j - 1] + cost_row[j] + c
                length = older[1][j - 1] + 2
                if d / length < score:
                    best_d, best_l, best_s = d, length, older[2][j - 1]
            if best_d < big:
                new[j], new_steps[j], new_starts[j] = best_d, best_l, best_s
        older = (row, steps, starts)
        row, steps, starts, cost_row = new, new_steps, new_starts, cost_next
    ends = [(row[j] / steps[j], j) for j in range(m) if steps[j]]
    if not ends:
        return 1.0, 0, 0
    score, last = min(ends)
    return score, starts[last], last + 1


def best_match(template: Frames, query: Frames) -> tuple[float, int, int]:
    """How far `template` is from its best match in `query` (0 is the same), and where
    that match is: query[start:end] (in the frames given).

    Subsequence dynamic time warping, at 40 ms (`pooled`): the whole template against
    any stretch of the query, by cosine distance per frame, averaged along the path.
    """
    small, wide = pooled(template), pooled(query)
    if not small or not wide:
        return 1.0, 0, 0
    score, start, end = _subsequence(small, wide)
    return score, 2 * start, 2 * end


def distance(template: Frames, query: Frames) -> float:
    """How far `template` is from its best match anywhere in `query`: 0 is the same."""
    return best_match(template, query)[0]


@dataclass(frozen=True, slots=True)
class Trained:
    """The user's wake word (and "Arcee stop", when trained): the sound of their samples,
    and how close counts."""

    templates: list[Frames]
    threshold: float
    stops: list[Frames] = field(default_factory=list[Frames])
    stop_threshold: float = 0.0
    # "Hey Arcee"s heard since, from wakes the user went on to use (learned_from).
    learned: list[Frames] = field(default_factory=list[Frames])

    @property
    def wakes(self) -> list[Frames]:
        return [*self.templates, *self.learned]

    def closest(self, query: Frames) -> float:
        return min(distance(t, query) for t in self.wakes)

    def span(self, recent: Frames) -> Frames:
        """The stretch of `recent` that matched the wake word best: the "hey Arcee" in
        it, as a template of its own."""
        query = normalise(recent)
        _, start, end = min(best_match(t, query) for t in self.wakes)
        return normalise(recent[start:end])

    def distances(self, recent: Frames) -> tuple[float, float]:
        """How far the speech just heard is from the wake word and from "Arcee stop"
        (1.0 when "Arcee stop" was not trained)."""
        query = normalise(recent)
        stop = min((distance(t, query) for t in self.stops), default=1.0)
        return self.closest(query), stop

    def judge(self, wake: float, stop: float) -> str:
        if self.stops and stop <= self.stop_threshold and stop < wake:
            return "stop"
        return "wake" if wake <= self.threshold else ""

    def match(self, recent: Frames) -> str:
        """The speech just heard: "wake", "stop" or ""."""
        return self.judge(*self.distances(recent))

    def heard(self, recent: Frames) -> bool:
        return self.match(recent) == "wake"

    @property
    def shortest(self) -> int:
        return min(len(t) for t in [*self.wakes, *self.stops])

    @property
    def window(self) -> int:
        """Frames of speech to match against: room for the longest sample (said slowly,
        or "hey… Arcee" with a pause) and a little either side."""
        return max(WINDOW, max(len(t) for t in [*self.wakes, *self.stops]) + 25)


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
        stop_threshold = min(LOOSEST, stop_spread + CALIBRATION * (stop_near - stop_spread))
    threshold = min(LOOSEST, spread + CALIBRATION * (near - spread))
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
        "features": FEATURES,
        "threshold": trained.threshold,
        "templates": rounded(trained.templates),
        "stop_threshold": trained.stop_threshold,
        "stops": rounded(trained.stops),
        "learned": rounded(trained.learned),
    }
    path.write_text(json.dumps(data))


def load(check: bool = True) -> Trained | None:
    """The user's trained wake word, if they trained one (and since its samples were
    last made the way they are now). Learned samples that are not `sure` are dropped
    (unless not `check`: only whether there is one)."""
    try:
        data = json.loads(trained_path().read_text())
        if data.get("features", 1) != FEATURES:
            return None  # Settings → Voice asks for it to be trained again

        def frames(templates: list[list[list[float]]]) -> list[Frames]:
            return [[list(map(float, f)) for f in t] for t in templates]

        trained = Trained(
            frames(data["templates"]),
            float(data["threshold"]),
            frames(data.get("stops", [])),  # trained before "Arcee stop" was: none
            float(data.get("stop_threshold", 0.0)),
        )
        learned = frames(data.get("learned", [])) if LEARNING else []
        if check:  # learned before the rule was this strict: only the sure ones
            learned = [heard for heard in learned if sure(trained, heard)]
        return Trained(
            trained.templates, trained.threshold, trained.stops, trained.stop_threshold, learned
        )
    except OSError, ValueError, KeyError, TypeError:
        return None


def sure(trained: Trained, heard: Frames) -> bool:
    """Whether `heard` is as close to the trained samples as they are to each other.

    Words after a wake do not show it was meant. While the user dictated, stretches of
    their sentences woke Arcee; the words that followed made each "meant", and it was
    kept as a sample; ordinary speech then woke it six times in 50 s (2026-11-06). Even
    within three quarters of the threshold, a stretch of "With the agent." was learned
    (0.120; the samples were 0.054-0.071 from each other)."""
    if len(trained.templates) < 2:
        return False
    return min(distance(t, heard) for t in trained.templates) <= _spread(trained.templates)


def learned_from(trained: Trained, heard: Frames) -> Trained:
    """`trained` with `heard` (a "hey Arcee" that woke Arcee and was followed by a real
    request) kept as a sample too: the newest MAX_LEARNED, the trained ones never
    replaced. Kept as their sound, as the trained ones are. Only a `sure` match of the
    trained samples is kept: never one that only a learned sample came near."""
    if not LEARNING or len(heard) < SHORTEST_S * RATE // HOP or not sure(trained, heard):
        return trained
    learned = [*trained.learned, heard][-MAX_LEARNED:]
    return Trained(
        trained.templates, trained.threshold, trained.stops, trained.stop_threshold, learned
    )


def is_trained() -> bool:
    return load(check=False) is not None


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
