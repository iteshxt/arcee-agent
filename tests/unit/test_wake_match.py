"""The wake word trained on the user's voice: features, matching, training, keeping it."""

import math
import random
from pathlib import Path

import pytest

from arcee import wake_match
from arcee.voice import VoiceError
from arcee.wake_match import HOP, WIN, Trained, distance, log_mel, normalise, train


@pytest.fixture(autouse=True)
def data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("ARCEE_DATA_DIR", str(tmp_path))
    return tmp_path


def sweep(start_hz: float, end_hz: float, seconds: float, seed: int = 0) -> list[float]:
    """A tone gliding from one pitch to another, with a little noise: a stand-in word."""
    noise = random.Random(seed)
    n = int(16000 * seconds)
    phase, out = 0.0, []
    for i in range(n):
        phase += 2 * math.pi * (start_hz + (end_hz - start_hz) * i / n) / 16000
        out.append(0.3 * math.sin(phase) + 0.01 * noise.uniform(-1, 1))
    return out


def silence(seconds: float, seed: int = 9) -> list[float]:
    noise = random.Random(seed)
    return [0.01 * noise.uniform(-1, 1) for _ in range(int(16000 * seconds))]


def test_a_frame_every_20_ms() -> None:
    frames = log_mel(sweep(300, 900, 1.0))
    assert len(frames) == (16000 - WIN) // HOP + 1
    assert all(len(f) == wake_match.BINS for f in frames)


def test_the_same_sound_is_no_distance_and_a_different_one_is_far() -> None:
    word = normalise(log_mel(sweep(300, 1200, 0.8)))
    other = normalise(log_mel(sweep(2500, 600, 0.8, seed=1)))

    assert distance(word, word) == pytest.approx(0, abs=1e-6)
    assert distance(word, other) > 0.3


def test_the_word_is_found_inside_longer_speech() -> None:
    word = normalise(log_mel(sweep(300, 1200, 0.8)))
    said = normalise(log_mel(silence(0.5) + sweep(300, 1200, 0.9, seed=2) + silence(0.4)))

    assert distance(word, said) < 0.15


def test_training_sets_how_close_counts_between_the_samples_and_the_sentence() -> None:
    samples = [sweep(300, 1200, s, seed=i) for i, s in enumerate((0.8, 0.9, 0.85))]
    sentence = sweep(2500, 600, 1.5, seed=7)

    training = train(samples, sentence)

    assert training.spread < training.trained.threshold < training.other
    assert len(training.trained.templates) == 3
    heard = log_mel(silence(0.3) + sweep(300, 1200, 0.95, seed=5))
    assert training.trained.heard(heard)
    assert not training.trained.heard(log_mel(sweep(2500, 600, 1.2, seed=8)))


def test_training_refuses_samples_too_short_or_too_long() -> None:
    sentence = sweep(2500, 600, 1.5)
    with pytest.raises(VoiceError, match="too short"):
        train([sweep(300, 1200, 0.1)] * 3, sentence)
    with pytest.raises(VoiceError, match="too long"):
        train([sweep(300, 1200, 3.0)] * 3, sentence)


def test_training_refuses_samples_that_differ() -> None:
    samples = [sweep(300, 1200, 0.8), sweep(3000, 500, 0.8), sweep(800, 4000, 0.8)]
    with pytest.raises(VoiceError, match="too different"):
        train(samples, sweep(2500, 600, 1.5))


def test_training_refuses_a_sentence_that_sounds_like_the_word() -> None:
    samples = [sweep(300, 1200, s, seed=i) for i, s in enumerate((0.8, 0.9, 0.85))]
    with pytest.raises(VoiceError, match="sounded like"):
        train(samples, sweep(300, 1200, 0.8, seed=1))


def test_the_trained_word_is_kept_and_forgotten() -> None:
    assert wake_match.load() is None
    trained = Trained([normalise(log_mel(sweep(300, 1200, 0.5)))], 0.2)

    wake_match.save(trained)
    loaded = wake_match.load()

    assert loaded is not None
    assert loaded.threshold == 0.2
    assert loaded.shortest == trained.shortest
    assert loaded.closest(trained.templates[0]) == pytest.approx(0, abs=1e-3)
    wake_match.forget()
    assert wake_match.load() is None


def test_a_damaged_file_is_no_training(data: Path) -> None:
    wake_match.trained_path().parent.mkdir(parents=True)
    wake_match.trained_path().write_text("{not json")
    assert wake_match.load() is None


def test_a_slow_or_paused_wake_word_is_matched_whole() -> None:
    # "hey… Arcee" with a pause runs past 1.6 s: the listener keeps enough to hold it.
    quick = Trained([normalise(log_mel(sweep(300, 1200, 0.8)))], 0.2)
    paused = Trained(
        [normalise(log_mel(sweep(300, 600, 0.6) + silence(0.6) + sweep(600, 1200, 0.9)))], 0.2
    )

    assert quick.window == wake_match.WINDOW
    assert paused.window > len(paused.templates[0])
