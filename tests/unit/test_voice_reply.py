"""Spoken replies (decision 0019): what is said, how it is said, and when arceed says it."""

import asyncio
from pathlib import Path
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


def test_a_long_first_sentence_is_made_in_two_so_it_is_heard_sooner() -> None:
    assert sentences(
        "It's 18 degrees Celsius and cloudy, with rain likely after 5 PM, so take a "
        "jacket. You have two meetings."
    ) == [
        "It's 18 degrees Celsius and cloudy,",
        "with rain likely after 5 PM, so take a jacket.",
        "You have two meetings.",
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

    def say(self, text: str, voice: int) -> list[float]:
        self.said.append((text, voice))
        return [0.5] * 80


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
    save(Settings(speak_replies=True, voice="Hugo"))
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


# --- the voices, Kitten made ready, the .deb's models ------------------------------------


def test_voices_have_names_and_how_they_sound() -> None:
    from arcee.voice_reply import DEFAULT_VOICE, VOICES, voice_id

    names = [v.name for v in VOICES]
    assert names == ["Jasper", "Bella", "Bruno", "Luna", "Hugo", "Rosie", "Leo", "Kiki"]
    assert all(v.about for v in VOICES)
    assert voice_id("bruno") == 2
    assert voice_id("Voice 3") == voice_id(DEFAULT_VOICE) == 3  # unknown: the default


def _fields(data: bytes) -> list[tuple[int, Any]]:
    """Top-level protobuf fields, enough to read ONNX metadata back."""

    def varint(i: int) -> tuple[int, int]:
        result = shift = 0
        while True:
            byte = data[i]
            i += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                return result, i

    out: list[tuple[int, Any]] = []
    i = 0
    while i < len(data):
        key, i = varint(i)
        if key & 7 == 0:
            value, i = varint(i)
        else:
            size, i = varint(i)
            value, i = data[i : i + size], i + size
        out.append((key >> 3, value))
    return out


def test_kitten_is_made_ready_for_sherpa_onnx(tmp_path: Path) -> None:
    import io
    import zipfile

    from arcee.voice_reply import VOICES, prepare

    header = b"{'descr': '<f4', 'fortran_order': False, 'shape': (400, 256), }"
    npz = io.BytesIO()
    with zipfile.ZipFile(npz, "w") as archive:
        for n, voice in enumerate(reversed(VOICES)):  # stored in another order
            body = bytes([n]) * (400 * 256 * 4)
            archive.writestr(
                f"{voice.key}.npy",
                b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header + body,
            )
    model = bytes([1 << 3, 9])  # ModelProto.ir_version = 9: a model, to the parser

    prepare(model, npz.getvalue(), tmp_path)

    styles = (tmp_path / "voices.bin").read_bytes()
    assert len(styles) == len(VOICES) * 400 * 256 * 4
    # In VOICES' order (the speaker ids), whatever the archive's.
    assert styles[0] == len(VOICES) - 1
    assert styles[-1] == 0
    written = (tmp_path / "model.onnx").read_bytes()
    assert written.startswith(model)  # the weights as published
    metadata = {
        dict(_fields(entry))[1].decode(): dict(_fields(entry))[2].decode()
        for field, entry in _fields(written)
        if field == 14
    }
    assert metadata["model_type"] == "kitten-tts"
    assert metadata["style_dim"] == "400,256"
    assert metadata["speaker_names"].split(",")[1] == "expr-voice-2-f"  # Bella, id 1
    assert (tmp_path / "tokens.txt").read_bytes() == voice_reply.TOKENS.read_bytes()


def test_a_model_the_deb_carries_is_used_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcee import voice_local
    from arcee.voice_local import model_path, models_dir
    from arcee.voice_reply import SPEECH

    monkeypatch.setattr(voice_local, "BUNDLED", tmp_path)
    assert model_path(SPEECH) == models_dir() / "voice"  # not in the .deb: the user's
    (tmp_path / "voice").mkdir()
    for name in SPEECH.files[:-1]:
        (tmp_path / "voice" / name).write_text("x")
    assert model_path(SPEECH) == models_dir() / "voice"  # incomplete: not used
    (tmp_path / "voice" / SPEECH.files[-1]).write_text("x")
    assert model_path(SPEECH) == tmp_path / "voice"


def test_phonemes_keep_the_punctuation_and_fit_kittens_symbols() -> None:
    from arcee import phonemes

    try:
        said = phonemes.phonemes("Done. I set it, Arcee!")
    except Exception as exc:  # no libespeak-ng here
        pytest.skip(f"libespeak-ng: {exc}")
    assert said.count(".") == 1
    assert "," in said
    assert said.endswith("!")
    assert "\u02c8" in said  # stress marks (ˈ), as KittenTTS gives them  # noqa: RUF003
    table = {
        line.rstrip("\n").rpartition(" ")[0] or " "
        for line in voice_reply.TOKENS.read_text(encoding="utf-8").splitlines()
    }
    assert set(phonemes.symbols("It's 18 degrees outside; take a jacket.")) <= table


# --- "read it out loud" ------------------------------------------------------------------


async def test_read_it_out_loud_says_the_text() -> None:
    from arcee.tools.base import ToolContext
    from arcee.tools.voice import SayArgs, VoiceSay

    ctx = ToolContext("t_1", Path("/home/user"))
    said: list[str] = []

    def say(text: str) -> str | None:
        said.append(text)
        return None

    result = await VoiceSay(lambda: say).run(SayArgs(text="**Done**: 18 °C at 17:00."), ctx)
    assert result.status == "ok"
    assert said == ["Done: 18 degrees Celsius at 5 PM."]

    no_voice = await VoiceSay(lambda: lambda _t: "the voice is not here").run(
        SayArgs(text="Hello."), ctx
    )
    assert no_voice.status != "ok"
    assert (await VoiceSay(lambda: None).run(SayArgs(text="Hello."), ctx)).status != "ok"


@pytest.mark.parametrize(
    ("request_", "offered"),
    [
        ("Read it out loud", True),
        ("read that aloud please", True),
        ("Read me the summary", True),
        ("say it in your voice", True),
        ("Open Chrome", False),
        ("What's the weather like?", False),
    ],
)
def test_asking_to_hear_it_brings_the_voice_tool(request_: str, offered: bool) -> None:
    from arcee.core.toolsets import VOICE, filters_for
    from arcee.models.base import Message

    assert (set(VOICE) <= set(filters_for([Message("user", request_)]))) is offered


async def test_what_was_asked_aloud_is_not_talked_over_by_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save(Settings(speak_replies=True))
    log: list[str] = []
    agent = service(log, [], monkeypatch)
    agent.Finished = lambda _p: None  # pyright: ignore[reportAttributeAccessIssue]

    async def reads_it(request: str, user: Any, on_event: Any, **_: Any) -> Any:
        from arcee.runner import TaskResult

        assert agent.say_aloud("Once upon a time.") is None  # voice.say, from the request
        return TaskResult("t_1", "done", "Reading it aloud.", 1, 1, 0.1)

    agent._runner.run = reads_it  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    agent._task = asyncio.ensure_future(  # pyright: ignore[reportPrivateUsage]
        agent._run("read the story out loud", spoken=True)  # pyright: ignore[reportPrivateUsage]
    )
    await agent._task  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.01)
    assert [line for line in log if line.startswith("say")] == ["say: Once upon a time."]


async def test_the_app_can_have_a_reply_read_and_hear_a_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcee import daemon

    log: list[str] = []
    agent = service(log, [], monkeypatch)
    voices: list[int] = []
    agent._speaker.say = lambda text, voice=0: (  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        voices.append(voice),
        asyncio.ensure_future(asyncio.sleep(0)),
    )[1]
    assert agent.say_text("**Hello** there.") == ""
    await asyncio.sleep(0.01)
    agent.preview_voice("Bruno")
    await asyncio.sleep(0.01)
    assert voices == [3, 2]  # Luna, the default; then Bruno

    monkeypatch.setattr(daemon, "installed", lambda model: False)
    assert "Settings → Voice" in agent.say_text("Hello.")
