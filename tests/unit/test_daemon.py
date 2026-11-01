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

from arcee.core.agent import Event, ToolRan
from arcee.core.presence import NoPresence
from arcee.core.protocol import ConfirmRequest, ExecutorResult
from arcee.daemon import BUS_NAME, OBJECT_PATH, AgentService, describe
from arcee.runner import TaskResult, User

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon")


class FakeRunner:
    """Asks to confirm one action, reports one step, then answers or waits to be cancelled."""

    def __init__(self, *, hang: bool = False) -> None:
        self.presence = NoPresence()
        self.stopped = False
        self.hang = hang

    async def run(self, request: str, user: User, on_event: Callable[[Event], None]) -> TaskResult:
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


def test_progress_lines_are_plain() -> None:
    result = ExecutorResult.model_construct(status="error", summary="no such file")
    assert describe(ToolRan(2, result)) == "✗ no such file"


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
    async def transcribe(self, pcm: bytes) -> str:
        assert pcm == b"pcm"
        return "open chrome and run youtube"


async def test_speaking_streams_levels_then_the_text(bus_address: str) -> None:
    service_bus = await connect(bus_address)
    service = AgentService(
        FakeRunner(),  # pyright: ignore[reportArgumentType]
        recorder=FakeRecorder,  # pyright: ignore[reportArgumentType]
        transcriber=FakeTranscriber(),  # pyright: ignore[reportArgumentType]
    )
    service_bus.export(OBJECT_PATH, service)
    await service_bus.request_name(BUS_NAME)
    bus, agent = await client(bus_address)
    levels: list[float] = []
    heard: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()
    agent.on_level(levels.append)
    agent.on_heard(lambda payload: heard.set_result(json.loads(payload)))

    await agent.call_start_listening()
    result = await asyncio.wait_for(heard, 5)

    assert result == {"text": "open chrome and run youtube"}
    assert levels == [0.1, 0.4, 0.2]
    bus.disconnect()
    service_bus.disconnect()
