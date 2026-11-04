"""arceed over a private D-Bus daemon: run a request, answer its question, get the reply."""

import asyncio
import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from dbus_fast import DBusError
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, signal

from arcee.core.agent import Event, ModelTurn, ToolRan
from arcee.core.presence import NoPresence
from arcee.core.protocol import ConfirmRequest, ExecutorResult
from arcee.daemon import (
    BUS_NAME,
    OBJECT_PATH,
    THINKING,
    AgentService,
    describe,
    doing,
    watch_shell_stop,
)
from arcee.models.base import WireCall
from arcee.platform.shell import BUS_NAME as SHELL_BUS_NAME
from arcee.platform.shell import OBJECT_PATH as SHELL_OBJECT_PATH
from arcee.runner import TaskResult, User

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon")


class FakeRunner:
    """Asks to confirm one action, reports one step, then answers or waits to be cancelled."""

    def __init__(self, *, hang: bool = False) -> None:
        self.presence = NoPresence()
        self.stopped = False
        self.hang = hang
        self.spoken = False

    async def run(
        self,
        request: str,
        user: User,
        on_event: Callable[[Event], None],
        *,
        spoken: bool = False,
        conversation: Any = None,
    ) -> TaskResult:
        self.spoken = spoken
        self.conversation = conversation
        if self.hang:
            await asyncio.sleep(60)
        allowed = await user.confirm(
            ConfirmRequest(
                request_id="c_1",
                task_id="t_1",
                tool="fs.trash",
                sentence="Move 1 file to the Trash?",
                risk="medium",
                args_preview=("~/old.txt",),
            )
        )
        result = ExecutorResult.model_construct(
            status="ok", summary="Trashed 1 file", duration_ms=3, tool="fs.trash"
        )
        on_event(ToolRan(1, result))
        reply = "Trashed old.txt." if allowed else "Kept old.txt."
        return TaskResult("t_1", "done", reply, 1, 1, 0.1)


@pytest.fixture
def bus_address() -> Iterator[str]:
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    address = process.stdout.readline().strip()
    yield address
    process.terminate()
    process.wait()
    process.stdout.close()


async def connect(address: str) -> MessageBus:
    return await MessageBus(bus_address=address).connect()


async def client(address: str) -> tuple[MessageBus, Any]:
    bus = await connect(address)
    introspection = await bus.introspect(BUS_NAME, OBJECT_PATH)
    proxy = bus.get_proxy_object(BUS_NAME, OBJECT_PATH, introspection)
    return bus, proxy.get_interface(BUS_NAME)


async def start_service(address: str, runner: FakeRunner) -> MessageBus:
    bus = await connect(address)
    bus.export(OBJECT_PATH, AgentService(runner))  # pyright: ignore[reportArgumentType]
    await bus.request_name(BUS_NAME)
    return bus


async def test_a_request_asks_the_panel_and_finishes(bus_address: str) -> None:
    service_bus = await start_service(bus_address, FakeRunner())
    bus, agent = await client(bus_address)
    progress: list[str] = []
    finished: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    async def on_question(payload: str) -> None:
        question = json.loads(payload)
        assert question["kind"] == "confirm"
        assert question["text"] == "Move 1 file to the Trash?"
        assert question["preview"] == ["~/old.txt"]
        await agent.call_answer(question["id"], "yes")

    answering: set[asyncio.Task[None]] = set()

    def on_question_sync(payload: str) -> None:
        task = asyncio.ensure_future(on_question(payload))
        answering.add(task)
        task.add_done_callback(answering.discard)

    agent.on_question(on_question_sync)
    agent.on_progress(progress.append)
    agent.on_finished(lambda payload: finished.set_result(json.loads(payload)))

    assert await agent.call_run("clean up old.txt") == "started"
    with pytest.raises(DBusError, match=r"already working on: clean up old\.txt"):
        await agent.call_run("something else")
    result = await asyncio.wait_for(finished, 5)

    assert result["status"] == "done"
    assert result["reply"] == "Trashed old.txt."
    assert progress == ["✓ Trashed 1 file"]
    assert json.loads(await agent.call_status())["state"] == "idle"
    bus.disconnect()
    service_bus.disconnect()


async def test_cancel_stops_the_request(bus_address: str) -> None:
    service_bus = await start_service(bus_address, FakeRunner(hang=True))
    bus, agent = await client(bus_address)
    finished: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    agent.on_finished(lambda payload: finished.set_result(json.loads(payload)))

    await agent.call_run("wait forever")
    await agent.call_cancel()
    result = await asyncio.wait_for(finished, 5)

    assert result == {"status": "stopped", "reply": ""}
    bus.disconnect()
    service_bus.disconnect()


class FakeShell(ServiceInterface):
    def __init__(self) -> None:
        super().__init__(SHELL_BUS_NAME)

    @signal()
    def Stopped(self) -> None:
        return None


async def test_the_shell_stop_shortcut_stops_the_request(bus_address: str) -> None:
    service_bus = await connect(bus_address)
    service = AgentService(FakeRunner(hang=True))  # pyright: ignore[reportArgumentType]
    service_bus.export(OBJECT_PATH, service)
    await service_bus.request_name(BUS_NAME)
    await watch_shell_stop(service_bus, service)
    shell_bus = await connect(bus_address)
    shell = FakeShell()
    shell_bus.export(SHELL_OBJECT_PATH, shell)
    bus, agent = await client(bus_address)
    finished: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    agent.on_finished(lambda payload: finished.set_result(json.loads(payload)))

    await agent.call_run("wait forever")
    shell.Stopped()
    result = await asyncio.wait_for(finished, 5)

    assert result == {"status": "stopped", "reply": ""}
    for b in (bus, shell_bus, service_bus):
        b.disconnect()


def test_progress_lines_are_plain() -> None:
    result = ExecutorResult.model_construct(status="error", summary="no such file")
    assert describe(ToolRan(2, result)) == "✗ no such file"


def turn(tool: str, **args: Any) -> ModelTurn:
    return ModelTurn(1, 0, "m", "", WireCall(tool, args), "", 0.1)


def test_the_step_says_what_arcee_is_doing_in_words() -> None:
    assert doing(turn("app_open", name="google-chrome")) == "Opening google-chrome"
    assert doing(turn("web_search", query="james bond")) == "Searching the web for james bond"
    assert doing(turn("browser_tabs")) == "Looking at Chrome's tabs"
    assert doing(turn("app_open")) == "Opening"
    assert doing(turn("brand_new_tool")) == "Using brand new tool"
    long = doing(turn("browser_navigate", url="https://example.com/" + "a" * 80))
    assert long is not None
    assert long.endswith("…")
    assert len(long) < 60


def test_after_a_tool_runs_arcee_is_thinking_again() -> None:
    result = ExecutorResult.model_construct(status="ok", summary="done")
    assert doing(ToolRan(2, result)) == THINKING


class FakeRecorder:
    def __init__(self, on_level: Callable[[float], None]) -> None:
        self.on_level = on_level

    def stop(self) -> None:
        return None

    async def record(self) -> tuple[bytes, bool]:
        for loudness in (0.1, 0.4, 0.2):
            self.on_level(loudness)
        return b"pcm", True


class FakeTranscriber:
    text = "open chrome and run youtube"

    async def transcribe(self, pcm: bytes) -> str:
        assert pcm == b"pcm"
        return self.text


async def test_speaking_streams_levels_then_the_text_and_runs_it(bus_address: str) -> None:
    service_bus = await connect(bus_address)
    service = AgentService(
        FakeRunner(hang=True),  # pyright: ignore[reportArgumentType]
        recorder=FakeRecorder,  # pyright: ignore[reportArgumentType]
        transcriber=FakeTranscriber(),  # pyright: ignore[reportArgumentType]
    )
    service_bus.export(OBJECT_PATH, service)
    await service_bus.request_name(BUS_NAME)
    bus, agent = await client(bus_address)
    levels: list[float] = []
    heard: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    agent.on_level(levels.append)
    agent.on_heard(lambda payload: heard.set_result(json.loads(payload)))
    agent.on_started(started.set_result)

    await agent.call_start_listening()
    result = await asyncio.wait_for(heard, 5)

    assert result == {"text": "open chrome and run youtube"}
    assert levels == [0.1, 0.4, 0.2]
    # The service runs what it heard itself, so the panel and the app need not.
    assert await asyncio.wait_for(started, 5) == "open chrome and run youtube"
    await agent.call_cancel()
    bus.disconnect()
    service_bus.disconnect()


async def test_a_woken_request_is_heard_and_run(bus_address: str) -> None:
    # What the wake-word listener calls: woken, then the request's audio.
    service_bus = await connect(bus_address)
    service = AgentService(
        FakeRunner(hang=True),  # pyright: ignore[reportArgumentType]
        transcriber=FakeTranscriber(),
    )
    service_bus.export(OBJECT_PATH, service)
    await service_bus.request_name(BUS_NAME)
    bus, agent = await client(bus_address)
    listening: list[bool] = []
    heard: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
    started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    agent.on_listening(listening.append)
    agent.on_heard(lambda payload: heard.set_result(json.loads(payload)))
    agent.on_started(started.set_result)

    service.on_wake()
    service.on_request(b"pcm")

    assert await asyncio.wait_for(heard, 5) == {"text": "open chrome and run youtube"}
    assert await asyncio.wait_for(started, 5) == "open chrome and run youtube"
    assert listening == [True, False]  # the panel and the app show it, as for the mic
    await asyncio.sleep(0.05)
    assert service._runner.spoken is True  # pyright: ignore  # the Core is told: spoken

    finished: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    agent.on_finished(lambda payload: finished.set_result(json.loads(payload)))
    service.on_stop()  # "Arcee stop"
    assert (await asyncio.wait_for(finished, 5))["status"] == "stopped"
    bus.disconnect()
    service_bus.disconnect()


async def test_waking_without_a_request_says_so(bus_address: str) -> None:
    service_bus = await connect(bus_address)
    service = AgentService(FakeRunner(), transcriber=FakeTranscriber())  # pyright: ignore[reportArgumentType]
    service_bus.export(OBJECT_PATH, service)
    await service_bus.request_name(BUS_NAME)
    bus, agent = await client(bus_address)
    heard: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
    agent.on_heard(lambda payload: heard.set_result(json.loads(payload)))

    service.on_wake()
    service.on_request(b"")

    assert await asyncio.wait_for(heard, 5) == {"error": "I did not hear anything"}
    assert not service.busy
    bus.disconnect()
    service_bus.disconnect()


async def test_training_holds_the_wake_word_then_it_takes_up_what_was_trained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcee import daemon, wake_match
    from arcee.voice_local import WakeListener

    service = AgentService(FakeRunner(), transcriber=FakeTranscriber())  # pyright: ignore[reportArgumentType]
    listener = WakeListener(
        on_wake=lambda: None, on_request=lambda _pcm: None, on_stop=lambda: None
    )
    service.wake = listener
    trained = wake_match.Trained([[[1.0]]], 0.2)
    monkeypatch.setattr(wake_match, "load", lambda: trained)

    service.HoldWake(True)
    assert listener.paused
    assert listener.trained is None

    service.HoldWake(False)
    assert not listener.paused
    assert listener.trained is trained

    monkeypatch.setattr(daemon, "HOLD_WAKE_S", 0.05)
    service.HoldWake(True)  # and nothing says it is done: it comes back by itself
    await asyncio.sleep(0.15)
    assert not listener.paused


@pytest.mark.parametrize(
    ("text", "stops"),
    [
        ("Stop.", True),
        ("Arcee stopped.", True),
        ("Arcee, stop", True),
        ("cancel that", True),
        ("Never mind.", True),
        ("stop the music in Spotify", False),
        ("What time is it?", False),
    ],
)
def test_stop_words(text: str, stops: bool) -> None:
    from arcee.daemon import is_stop

    assert is_stop(text) is stops


async def test_saying_stop_after_the_wake_word_stops_the_request(bus_address: str) -> None:
    service_bus = await connect(bus_address)
    transcriber = FakeTranscriber()
    service = AgentService(FakeRunner(hang=True), transcriber=transcriber)  # pyright: ignore[reportArgumentType]
    service_bus.export(OBJECT_PATH, service)
    await service_bus.request_name(BUS_NAME)
    bus, agent = await client(bus_address)
    finished: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    agent.on_finished(lambda payload: finished.set_result(json.loads(payload)))
    service.Run("open chrome")
    await asyncio.sleep(0.05)

    transcriber.text = "Arcee stopped."
    service.on_wake()
    service.on_request(b"pcm")

    assert (await asyncio.wait_for(finished, 5))["status"] == "stopped"
    bus.disconnect()
    service_bus.disconnect()


async def test_cancel_drops_a_request_being_heard() -> None:
    from arcee.voice_local import WakeListener

    service = AgentService(FakeRunner(), transcriber=FakeTranscriber())  # pyright: ignore[reportArgumentType]
    heard = WakeListener(on_wake=lambda: None, on_request=lambda _pcm: None, on_stop=lambda: None)
    heard._hearing = True  # pyright: ignore[reportPrivateUsage]  # woken, the request coming
    service.wake = heard
    listening: list[bool] = []
    service.Listening = listening.append  # type: ignore[method-assign]

    service.Cancel()  # Esc

    assert heard._drop  # pyright: ignore[reportPrivateUsage]
    assert listening == [False]


async def test_requests_share_the_conversation_until_it_is_ended(bus_address: str) -> None:
    from arcee.context.conversation import Turn

    service_bus = await connect(bus_address)
    runner = FakeRunner()
    service = AgentService(runner, transcriber=FakeTranscriber())  # pyright: ignore[reportArgumentType]
    service_bus.export(OBJECT_PATH, service)
    await service_bus.request_name(BUS_NAME)
    bus, agent = await client(bus_address)
    service.conversation.add(Turn("Open Chrome.", "Chrome is open.", "done"))

    service.Run("open youtube")
    await asyncio.sleep(0.05)

    assert runner.conversation is service.conversation  # the run continues it
    assert json.loads(await agent.call_status())["turns"] == 1
    await agent.call_end_conversation()  # the user closed the pill
    assert json.loads(await agent.call_status())["turns"] == 0
    bus.disconnect()
    service_bus.disconnect()


@pytest.mark.parametrize(
    ("said", "kind", "options", "answer"),
    [
        ("Yes.", "confirm", [], "yes"),
        ("Yeah, go ahead", "confirm", [], "yes"),
        ("Nope.", "confirm", [], "no"),
        ("Maybe later", "confirm", [], None),
        ("Yes please.", "ask", ["Go ahead", "No"], "Go ahead"),
        ("No.", "ask", ["Go ahead", "No"], "No"),
        ("The second one", "ask", ["Events", "Family"], "Family"),
        ("Family.", "ask", ["Events", "Family"], "Family"),
        ("Hmm, not sure", "ask", ["Go ahead", "No"], "Hmm, not sure"),
        ("At five in the evening", "ask", [], "At five in the evening"),
    ],
)
def test_spoken_answers(said: str, kind: str, options: list[str], answer: str | None) -> None:
    from arcee.daemon import spoken_answer

    assert spoken_answer(said, kind, options) == answer


async def test_a_question_is_answered_by_voice(bus_address: str) -> None:
    service_bus = await connect(bus_address)
    transcriber = FakeTranscriber()
    service = AgentService(FakeRunner(), transcriber=transcriber)  # pyright: ignore[reportArgumentType]
    service_bus.export(OBJECT_PATH, service)
    await service_bus.request_name(BUS_NAME)
    bus, agent = await client(bus_address)
    answered: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    agent.on_answered(answered.set_result)

    asking = asyncio.ensure_future(
        service.ask("This needs your emails to go to Groq. Go ahead?", ["Go ahead", "No"])
    )
    await asyncio.sleep(0.05)
    transcriber.text = "Yes, go ahead."
    service.on_wake()
    service.on_request(b"pcm")

    assert await asyncio.wait_for(asking, 5) == "Go ahead"
    assert await asyncio.wait_for(answered, 5) == "q1"  # the pill and the app close it
    assert not service.busy  # an answer is not a new request
    bus.disconnect()
    service_bus.disconnect()


@pytest.mark.parametrize(
    ("said", "learned"),
    [("Open Chrome.", [True]), ("", [False]), ("Arcee, stop.", [False])],
)
async def test_a_wake_is_learned_only_when_a_real_request_follows(
    said: str, learned: list[bool]
) -> None:
    class Wake:
        def __init__(self) -> None:
            self.learned: list[bool] = []

        def learn(self, meant: bool) -> None:
            self.learned.append(meant)

    transcriber = FakeTranscriber()
    transcriber.text = said
    service = AgentService(FakeRunner(), transcriber=transcriber)  # pyright: ignore[reportArgumentType]
    wake = Wake()
    service.wake = wake  # pyright: ignore[reportAttributeAccessIssue]
    service.Heard = lambda payload: payload  # type: ignore[method-assign]

    await service._hear(b"pcm" if said else b"", woken=True)  # pyright: ignore[reportPrivateUsage]

    assert wake.learned == learned
    if service._task is not None:  # pyright: ignore[reportPrivateUsage]
        service._task.cancel()  # pyright: ignore[reportPrivateUsage]
