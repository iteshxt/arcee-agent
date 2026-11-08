"""Spoken replies (decision 0019): what is said, how it is said, and when arceed says it."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcee import voice_reply
from arcee.daemon import FAILED_ALOUD, AgentService, question_aloud
from arcee.settings import Settings, save
from arcee.voice_reply import LONGEST, REST_ON_SCREEN, Speaker, sentences, speakable


def test_markdown_links_and_lists_are_said_as_sentences() -> None:
    reply = (
        "## Tomorrow\n\n"
        "- **10:30** Design review\n"
        "- 13:00 Lunch at [Cafe Lota](https://maps.example/lota)\n\n"
        "Details: https://calendar.example/day and `notes_2026.txt`."
    )
    assert speakable(reply) == (
        "Tomorrow. 10 30 AM Design review. 1 PM Lunch at Cafe Lota. "
        "Details: the link on screen and notes 2026.txt."
    )


@pytest.mark.parametrize(
    ("written", "said"),
    [
        ("18 °C and cloudy", "18 degrees Celsius and cloudy"),  # was "18 to see"
        ("It is 64°F", "It is 64 degrees Fahrenheit"),
        ("Battery at 80%", "Battery at 80 percent"),
        ("Wind 12 km/h", "Wind 12 kilometres an hour"),
        ("At 09:05", "At 9 oh 5 AM"),
        ("At 00:00", "At 12 AM"),
        ("At 17:45", "At 5 45 PM"),
        ("At 5:30 pm", "At 5:30 pm"),  # already said as people say it
        ("Tom & Jerry", "Tom and Jerry"),
    ],
)
def test_units_and_times_are_said_in_words(written: str, said: str) -> None:
    assert speakable(written) == f"{said}."


def test_a_long_reply_is_cut_at_a_sentence_and_points_to_the_screen() -> None:
    reply = " ".join(f"This is sentence number {n} of a long answer." for n in range(40))
    said = speakable(reply)
    assert said.endswith(REST_ON_SCREEN)
    assert len(said) <= LONGEST + len(REST_ON_SCREEN) + 1
    assert said.removesuffix(REST_ON_SCREEN).strip().endswith("answer.")


def test_short_sentences_are_made_with_the_next() -> None:
    assert sentences("Done. I set a timer for ten minutes. It rings at 5 PM.") == [
        "Done. I set a timer for ten minutes.",
        "It rings at 5 PM.",
    ]


def test_questions_say_how_to_answer() -> None:
    assert question_aloud({"kind": "confirm", "text": "Open YouTube?"}) == (
        "Open YouTube? Say allow or deny."
    )
    assert question_aloud({"kind": "confirm", "text": "Open YouTube?", "allowable": True}) == (
        "Open YouTube? Say allow, always allow, or deny."
    )
    assert question_aloud(
        {"kind": "ask", "text": "Which file?", "options": ["notes.txt", "todo.txt", "plan.md"]}
    ) == ("Which file? notes.txt, todo.txt, or plan.md?")


class FakeSynth:
    sample_rate = 8000

    def __init__(self) -> None:
        self.said: list[tuple[str, int]] = []

    def generate(self, text: str, sid: int = 0, speed: float = 1.0) -> Any:
        self.said.append((text, sid))
        return SimpleNamespace(samples=[0.5] * 80)


async def test_the_speaker_says_each_sentence_into_one_player(tmp_path: Path) -> None:
    out = tmp_path / "played.raw"
    made: list[FakeSynth] = []

    def make() -> FakeSynth:
        made.append(FakeSynth())
        return made[-1]

    speaker = Speaker(make, command=lambda rate: ("sh", "-c", f"cat > {out}"))
    speaker.warm()  # while the request is thought about
    await speaker.say("Done. I set a timer for ten minutes. It rings at five.", 3)

    assert len(made) == 1  # warmed and said: loaded once
    assert made[0].said == [
        ("Done. I set a timer for ten minutes.", 3),
        ("It rings at five.", 3),
    ]
    assert out.read_bytes() == (int(0.5 * 32767)).to_bytes(2, "little", signed=True) * 160
    assert not speaker.speaking


async def test_stopping_cuts_the_speaker_off(tmp_path: Path) -> None:
    speaker = Speaker(FakeSynth, command=lambda rate: ("sh", "-c", "sleep 30"))
    task = speaker.say("Something long to say.")
    await asyncio.sleep(0.3)
    assert speaker.speaking
    assert speaker.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not speaker.speaking
    assert not speaker.stop()  # nothing to stop now


async def test_the_model_is_let_go_when_unused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_reply, "IDLE_S", 0.05)
    made: list[FakeSynth] = []
    speaker = Speaker(lambda: made.append(FakeSynth()) or made[-1], command=lambda r: ("true",))
    await speaker.say("Hello there, this is a test.")
    assert speaker._synth is not None  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.15)
    assert speaker._synth is None  # pyright: ignore[reportPrivateUsage]


# --- arceed ----------------------------------------------------------------------------


class FakeSpeaker:
    """Says things in 50 ms, noting what and when; `stop` ends it at once."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self._task: asyncio.Task[None] | None = None

    def warm(self) -> None:
        self.log.append("warm")

    def say(self, text: str, voice: int = 0) -> asyncio.Task[None]:
        self.log.append(f"say: {text}")
        self._task = asyncio.ensure_future(asyncio.sleep(0.05))
        return self._task

    def stop(self) -> bool:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self.log.append("hushed")
            return True
        return False


class FakeRecorder:
    def __init__(self, log: list[str], said: list[str]) -> None:
        self.log, self.said = log, said

    def __call__(self, on_level: Any) -> FakeRecorder:
        return self

    def stop(self) -> None:
        return None

    async def record(self) -> tuple[bytes, bool]:
        self.log.append("mic open")
        return b"pcm", True


class Transcriber:
    def __init__(self, said: list[str]) -> None:
        self.said = said

    async def transcribe(self, pcm: bytes) -> str:
        return self.said.pop(0)


class Runner:
    """Asks to confirm, then answers with Markdown."""

    def __init__(self) -> None:
        from arcee.core.presence import NoPresence

        self.presence = NoPresence()
        self.stopped = False

    async def run(self, request: str, user: Any, on_event: Any, **_: Any) -> Any:
        from arcee.core.protocol import ConfirmRequest
        from arcee.runner import TaskResult

        allowed = await user.confirm(
            ConfirmRequest(
                request_id="c_1",
                task_id="t_1",
                tool="app.open",
                sentence="Open YouTube?",
                risk="low",
                args_preview=(),
                allowable=True,
            )
        )
        reply = "**Opened** YouTube at 18:30." if allowed else "Left it closed."
        return TaskResult("t_1", "done", reply, 1, 1, 0.1)


def service(log: list[str], said: list[str], monkeypatch: pytest.MonkeyPatch) -> AgentService:
    from arcee import daemon

    monkeypatch.setattr(daemon, "installed", lambda model: True)
    agent = AgentService(
        Runner(),  # pyright: ignore[reportArgumentType]
        recorder=FakeRecorder(log, said),  # pyright: ignore[reportArgumentType]
        transcriber=Transcriber(said),  # pyright: ignore[reportArgumentType]
        speaker=FakeSpeaker(log),  # pyright: ignore[reportArgumentType]
    )
    for name in ("Question", "Answered", "Heard", "Listening", "Started", "Step", "Progress"):
        setattr(agent, name, lambda *_a: None)
    return agent


async def test_a_spoken_request_is_answered_aloud_its_question_said_before_the_mic_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save(Settings(speak_replies=True, voice=4))
    log: list[str] = []
    agent = service(log, ["open youtube", "yes"], monkeypatch)
    finished: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    agent.Finished = finished.set_result  # pyright: ignore[reportAttributeAccessIssue]

    agent.on_request(b"pcm")  # woken, "open youtube"
    await asyncio.wait_for(finished, 5)
    await asyncio.sleep(0.1)

    assert log == [
        "warm",
        "say: Open YouTube? Say allow, always allow, or deny.",
        "mic open",  # only once the question was said
        "say: Opened YouTube at 6 30 PM.",
    ]


async def test_nothing_is_said_when_it_is_off_or_the_request_was_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    agent = service(log, ["open youtube", "yes"], monkeypatch)
    finished: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    agent.Finished = finished.set_result  # pyright: ignore[reportAttributeAccessIssue]
    agent.on_request(b"pcm")  # spoken, but "Answer aloud" is off
    await asyncio.wait_for(finished, 5)
    assert log == ["mic open"]  # the question heard by voice, as before; nothing said

    save(Settings(speak_replies=True))
    log.clear()
    task = asyncio.ensure_future(agent._run("open youtube"))  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.05)
    task.cancel()
    assert log == []  # typed: nothing said


async def test_a_failure_is_said_briefly_and_a_new_request_hushes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save(Settings(speak_replies=True))
    log: list[str] = []
    agent = service(log, [], monkeypatch)
    agent.Finished = lambda _p: None  # pyright: ignore[reportAttributeAccessIssue]

    async def fails(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    agent._runner.run = fails  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    await agent._run("open youtube", spoken=True)  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.01)
    assert log[-1] == f"say: {FAILED_ALOUD}"

    agent.EndConversation()  # the pill closed: quiet at once
    assert log[-1] == "hushed"


async def test_the_wake_word_does_not_listen_while_arcee_talks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcee.voice_local import WakeListener

    save(Settings(speak_replies=True))
    log: list[str] = []
    agent = service(log, [], monkeypatch)
    agent.Finished = lambda _p: None  # pyright: ignore[reportAttributeAccessIssue]
    wake = WakeListener(on_wake=lambda: None, on_request=lambda _p: None, on_stop=lambda: None)
    agent.wake = wake

    agent._say("Hello there, this is Arcee.")  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.01)
    assert wake.paused  # it would hear Arcee's own voice
    await asyncio.sleep(0.1)
    assert not wake.paused  # and hears "hey Arcee" again after
