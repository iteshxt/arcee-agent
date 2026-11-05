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


def test_arcee_stop_is_trained_apart_from_the_wake_word() -> None:
    wake = [sweep(300, 1200, s, seed=i) for i, s in enumerate((0.8, 0.9, 0.85))]
    stop = [sweep(2000, 3500, s, seed=10 + i) for i, s in enumerate((0.7, 0.75))]
    sentence = sweep(5000, 900, 1.5, seed=7)

    training = train(wake, sentence, stop)
    trained = training.trained

    assert training.stop_spread < trained.stop_threshold < training.stop_other
    assert trained.match(log_mel(silence(0.3) + sweep(2000, 3500, 0.72, seed=20))) == "stop"
    assert trained.match(log_mel(silence(0.3) + sweep(300, 1200, 0.9, seed=21))) == "wake"
    assert trained.match(log_mel(sweep(5000, 900, 1.2, seed=22))) == ""
    wake_match.save(trained)
    loaded = wake_match.load()
    assert loaded is not None
    assert len(loaded.stops) == 2
    assert loaded.stop_threshold == trained.stop_threshold


def test_a_wake_word_saved_without_arcee_stop_still_loads() -> None:
    import json

    word = log_mel(sweep(300, 1200, 0.8))
    wake_match.trained_path().parent.mkdir(parents=True, exist_ok=True)
    saved = {"features": wake_match.FEATURES, "threshold": 0.2, "templates": [normalise(word)]}
    wake_match.trained_path().write_text(json.dumps(saved))
    loaded = wake_match.load()
    assert loaded is not None
    assert loaded.stops == []
    assert loaded.match(word) == "wake"
    assert wake_match.is_trained()


def test_samples_kept_before_denoising_are_trained_again() -> None:
    import json

    word = normalise(log_mel(sweep(300, 1200, 0.8)))
    wake_match.trained_path().parent.mkdir(parents=True, exist_ok=True)
    wake_match.trained_path().write_text(json.dumps({"threshold": 0.2, "templates": [word]}))
    assert wake_match.load() is None  # matched against denoised speech, they would miss
    assert not wake_match.is_trained()


def test_what_is_far_under_the_word_does_not_change_it() -> None:
    word = sweep(300, 1200, 0.8)
    hushed = [v + 0.0005 * random.Random(9).uniform(-1, 1) for v in word]
    gap = [0.0001 * random.Random(n).uniform(-1, 1) for n in range(8000)]
    quieter = [0.00001 * random.Random(n).uniform(-1, 1) for n in range(8000)]
    # Two rooms' leftover noise, a hundred times apart, both far under the word.
    one = normalise(log_mel(gap + hushed))
    other = normalise(log_mel(quieter + hushed))
    assert distance(normalise(log_mel(word)), one) < 0.1  # the word is still found in it
    assert (
        max(
            1 - sum(a * b for a, b in zip(x, y, strict=True))
            for x, y in zip(one, other, strict=True)
        )
        < 1e-6
    )  # the gaps are held at the floor: the same in both rooms


def test_the_match_says_where_the_word_is() -> None:
    word = normalise(log_mel(sweep(300, 1200, 0.8)))
    before, after = log_mel(silence(0.6)), log_mel(silence(0.6, seed=3))
    said = log_mel(silence(0.6) + sweep(300, 1200, 0.8, seed=2) + silence(0.6, seed=3))

    score, start, end = wake_match.best_match(word, normalise(said))

    assert score < 0.25  # more silence around it than in training: a little further
    assert abs(start - len(before)) <= 3
    assert abs((len(said) - end) - len(after)) <= 3


def test_wakes_that_were_meant_are_learned_newest_kept() -> None:
    samples = [sweep(300, 1200, s, seed=i) for i, s in enumerate((0.8, 0.9, 0.85, 0.82, 0.88))]
    trained = train(samples, sweep(2500, 600, 1.5, seed=7)).trained
    assert len(trained.templates) == wake_match.SAMPLES == 5

    for n in range(wake_match.MAX_LEARNED + 2):
        heard = log_mel(silence(0.3) + sweep(300, 1200, 0.8 + n / 50, seed=40 + n))
        trained = wake_match.learned_from(trained, trained.span(heard))

    assert len(trained.learned) == wake_match.MAX_LEARNED
    assert len(trained.templates) == 5  # the trained ones are never replaced
    assert len(trained.wakes) == 5 + wake_match.MAX_LEARNED
    wake_match.save(trained)
    loaded = wake_match.load()
    assert loaded is not None
    assert len(loaded.learned) == wake_match.MAX_LEARNED
    # A sound too short to be "hey Arcee" is not learned.
    assert wake_match.learned_from(loaded, log_mel(sweep(300, 1200, 0.1))[:3]) is loaded


def test_only_a_sure_wake_is_learned() -> None:
    """Words after a wake do not prove it was meant: a stretch that only scraped under
    the threshold (the user dictating, 2026-11-06) is not kept as a sample."""
    samples = [sweep(300, 1200, s, seed=i) for i, s in enumerate((0.8, 0.9, 0.85, 0.82, 0.88))]
    trained = train(samples, sweep(2500, 600, 1.5, seed=7)).trained
    sure = trained.span(log_mel(silence(0.3) + sweep(300, 1200, 0.84, seed=50)))
    assert wake_match.learned_from(trained, sure) is not trained

    word = normalise(log_mel(sweep(300, 1200, 0.85, seed=51)))
    nearest = min(distance(t, word) for t in trained.templates)
    # The same sound, judged against a threshold it only just meets.
    scraped = Trained(trained.templates, round(nearest / wake_match.SURE * 0.95, 4))
    assert wake_match.learned_from(scraped, word).learned == []  # within the threshold...
    assert nearest > wake_match.SURE * scraped.threshold  # ...but not surely
