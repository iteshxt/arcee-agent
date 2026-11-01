"""arceed: Arcee as a background service on the session bus (spec section 10, phase 4).

The panel in the GNOME Shell extension talks to it as me.iteshxt.Arcee.Agent: `Run` starts a
request (one at a time), `Progress` signals each step, `Question` asks the user
something (a question, or a confirmation before a risky action) and waits for
`Answer`, and `Finished` carries the reply. `Cancel` stops the request. The models,
tools and store are built once, when the service starts.

D-Bus activation starts it on the first call (`arcee-cli daemon-install`).
"""

import asyncio
import contextlib
import itertools
import json
import logging
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

from dbus_fast import BusType, DBusError, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusBool, DBusDouble, DBusStr, DBusUInt32
from dbus_fast.service import ServiceInterface, method, signal

from arcee.config import load_env
from arcee.core.agent import Escalated, Event, Guarded, ModelTurn, Planned, StepStarted, ToolRan
from arcee.core.protocol import ConfirmRequest
from arcee.models.errors import ModelUnavailableError
from arcee.runner import TaskRunner
from arcee.settings import load as load_settings
from arcee.voice import Recorder, Transcriber, VoiceError

BUS_NAME: Final = "me.iteshxt.Arcee.Agent"
OBJECT_PATH: Final = "/me/iteshxt/Arcee/Agent"
BUSY_ERROR: Final = "me.iteshxt.Arcee.Error.Busy"
RECENT_LIMIT: Final = 20

log = logging.getLogger("arceed")


def describe(event: Event) -> str | None:
    """One plain line for the panel's progress list; None for events it does not show."""
    match event:
        case ModelTurn(call=call) if call is not None:
            return f"→ {call.name}"
        case ToolRan(result=result):
            return f"{'✓' if result.status == 'ok' else '✗'} {result.summary}"
        case Guarded(what=what):
            return f"⚑ {what}"
        case Escalated(model=model):
            return f"↑ {model}"
        case Planned(plan=plan):
            return f"plan: {plan.goal}"
        case StepStarted(step=step, total=total, do=do) if total > 1:
            return f"step {step}/{total}: {do}"
        case _:
            return None


# What each tool is doing, in words a person watching would use. The wire names are
# the tool names with "." as "_". `{}` takes the tool's main argument when it has one.
DOING: Final = {
    "app_open": "Opening {}",
    "app_close": "Closing {}",
    "app_list": "Looking at the open apps",
    "browser_tabs": "Looking at Chrome's tabs",
    "browser_navigate": "Going to {}",
    "browser_click": "Clicking in Chrome",
    "browser_fill": "Typing in Chrome",
    "browser_back": "Going back in Chrome",
    "browser_scroll": "Scrolling in Chrome",
    "browser_read_text": "Reading the page",
    "browser_get_elements": "Looking over the page",
    "control_ask_user": "Waiting for your answer",
    "control_wait": "Waiting a moment",
    "fs_read": "Reading {}",
    "fs_write": "Writing {}",
    "fs_list": "Looking in {}",
    "fs_search": "Searching your files for {}",
    "fs_copy": "Copying files",
    "fs_move": "Moving files",
    "fs_trash": "Moving files to the bin",
    "fs_open_with_default": "Opening {}",
    "kbd_type": "Typing",
    "kbd_key": "Pressing keys",
    "screen_read": "Reading the screen",
    "screen_look": "Looking at the screen",
    "screen_find": "Finding {} on screen",
    "screen_click": "Clicking",
    "screen_scroll": "Scrolling",
    "shell_run": "Running a command",
    "shell_script": "Running a script",
    "web_search": "Searching the web for {}",
    "web_fetch": "Reading {}",
    "window_list": "Looking at the open windows",
    "window_act": "Arranging windows",
    "ui_windows": "Looking at the open windows",
    "ui_tree": "Looking over the window",
    "ui_act": "Clicking in the window",
    "system_status": "Checking the system",
    "network_status": "Checking the network",
}
THINKING: Final = "Thinking…"
SUBJECT_ARGS: Final = ("name", "app", "url", "query", "path", "what", "target")
SUBJECT_CHARS: Final = 40


def doing(event: Event) -> str | None:
    """What Arcee is doing right now, for the pill and the app; None to keep the last."""
    match event:
        case ModelTurn(call=call) if call is not None:
            template = DOING.get(call.name)
            if template is None:
                return f"Using {call.name.replace('_', ' ')}"
            subject = next((str(call.args[k]) for k in SUBJECT_ARGS if call.args.get(k)), "")
            if len(subject) > SUBJECT_CHARS:
                subject = subject[: SUBJECT_CHARS - 1] + "…"
            if "{}" in template and not subject:
                return template.replace(" {}", "").replace("{} ", "").replace("{}", "")
            return template.format(subject)
        case ToolRan() | Escalated():
            return THINKING
        case StepStarted(do=do) if do:
            return do
        case _:
            return None


class AgentService(ServiceInterface):
    def __init__(
        self,
        runner: TaskRunner,
        *,
        recorder: Callable[[Callable[[float], None]], Recorder] = Recorder,
        transcriber: Transcriber | None = None,
    ) -> None:
        super().__init__(BUS_NAME)
        self._runner = runner
        self._make_recorder = recorder
        self._transcriber = transcriber or Transcriber()
        self._recorder: Recorder | None = None
        self._listening: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._request = ""
        self._step = ""
        self._questions: dict[str, asyncio.Future[str]] = {}
        self._ids = itertools.count(1)
        # Ends the service (set by `serve` to disconnect its bus).
        self.on_quit: Callable[[], None] = lambda: None

    # --- methods --------------------------------------------------------------------

    @method()
    def Run(self, request: DBusStr) -> DBusStr:
        if self.busy:
            raise DBusError(BUSY_ERROR, f"Arcee is already working on: {self._request}")
        self._request = request.strip()
        self._task = asyncio.ensure_future(self._run(self._request))
        return "started"

    @method()
    def Cancel(self) -> None:
        if self.busy and self._task is not None:
            self._runner.stopped = True
            self._task.cancel()

    @method()
    def Quit(self) -> None:
        self.Cancel()
        asyncio.get_running_loop().call_later(0.2, self.on_quit)

    @method()
    def Answer(self, question_id: DBusStr, answer: DBusStr) -> None:
        future = self._questions.pop(question_id, None)
        if future is not None and not future.done():
            future.set_result(answer)

    @method()
    def StartListening(self) -> None:
        """Record a spoken request; Heard carries its text when it ends."""
        if self._listening is not None and not self._listening.done():
            return
        self._recorder = self._make_recorder(self._level)
        self._listening = asyncio.ensure_future(self._listen(self._recorder))

    @method()
    def StopListening(self) -> None:
        if self._recorder is not None:
            self._recorder.stop()

    @method()
    def ShowApp(self, page: DBusStr) -> None:
        """Open the Arcee app on `page` (agent, models, history...; empty for its first)."""
        app = Path(sys.executable).parent / "arcee-app"
        subprocess.Popen(
            [str(app), *([f"--page={page}"] if page else [])],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @method()
    def Status(self) -> DBusStr:
        return json.dumps(
            {
                "state": "running" if self.busy else "idle",
                "request": self._request,
                "step": self._step if self.busy else "",
            }
        )

    @method()
    async def Recent(self, limit: DBusUInt32) -> DBusStr:
        rows = await self._runner.store.recent(min(limit, RECENT_LIMIT) or RECENT_LIMIT)
        return json.dumps(
            [
                {"id": r.id, "request": r.request, "status": r.status, "reply": r.reply or ""}
                for r in rows
            ]
        )

    @method()
    def Limits(self) -> DBusStr:
        from arcee.models.presets import rate_budget

        return json.dumps(rate_budget().snapshot())

    # --- signals --------------------------------------------------------------------

    @signal()
    def Started(self, request: str) -> DBusStr:
        return request

    @signal()
    def Progress(self, line: str) -> DBusStr:
        return line

    @signal()
    def Step(self, doing: str) -> DBusStr:
        return doing

    @signal()
    def Question(self, payload: str) -> DBusStr:
        return payload

    @signal()
    def Finished(self, payload: str) -> DBusStr:
        return payload

    @signal()
    def Listening(self, on: bool) -> DBusBool:
        return on

    @signal()
    def Level(self, loudness: float) -> DBusDouble:
        return loudness

    @signal()
    def Heard(self, payload: str) -> DBusStr:
        return payload

    # --- voice ------------------------------------------------------------------------

    def _level(self, loudness: float) -> None:
        self.Level(loudness)

    async def _listen(self, recorder: Recorder) -> None:
        """Record, transcribe, tell the panel and the app the text, and run it."""
        self.Listening(True)
        heard: dict[str, str]
        try:
            audio, spoke = await recorder.record()
            if not spoke:
                heard = {"error": "I did not hear anything"}
            else:
                heard = {"text": await self._transcriber.transcribe(audio)}
        except VoiceError as exc:
            heard = {"error": str(exc)}
        finally:
            self.Listening(False)
            self._recorder = None
        self.Heard(json.dumps(heard))
        # What was heard runs here, once, whether the panel or the app is open.
        text = heard.get("text", "").strip()
        if text and not self.busy:
            self._request = text
            self._task = asyncio.ensure_future(self._run(text))

    # --- the agent's user: questions go to the panel ----------------------------------

    async def ask(self, question: str, options: Sequence[str] = ()) -> str:
        return await self._question({"kind": "ask", "text": question, "options": list(options)})

    async def confirm(self, request: ConfirmRequest) -> bool:
        answer = await self._question(
            {"kind": "confirm", "text": request.sentence, "preview": list(request.args_preview)}
        )
        return answer.strip().lower() in {"y", "yes", "allow"}

    async def _question(self, payload: dict[str, Any]) -> str:
        question_id = f"q{next(self._ids)}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._questions[question_id] = future
        # The user answers with the mouse: give control back so that is not a pause.
        presence = self._runner.presence
        held = presence.held
        await presence.release()
        self._set_step("Waiting for your answer")
        self.Question(json.dumps({"id": question_id, **payload}))
        try:
            return await future
        finally:
            self._questions.pop(question_id, None)
            if held:
                await presence.acting()

    # --- running ----------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self, request: str) -> None:
        self.Started(request)
        self._set_step(THINKING)
        payload: dict[str, Any]
        try:
            result = await self._runner.run(request, self, self._progress)
            payload = {
                "status": result.status,
                "reply": result.reply,
                "task_id": result.task_id,
                "steps": result.steps,
                "seconds": round(result.seconds, 1),
            }
        except asyncio.CancelledError:
            payload = {"status": "stopped" if self._runner.stopped else "cancelled", "reply": ""}
        except ModelUnavailableError as exc:
            payload = {"status": "failed", "reply": f"model unavailable: {exc}"}
        except Exception as exc:  # one failed request must not take the service down
            log.exception("request failed")
            payload = {"status": "failed", "reply": f"{type(exc).__name__}: {exc}"}
        finally:
            for future in self._questions.values():
                future.cancel()
            self._questions.clear()
            with contextlib.suppress(Exception):
                await self._runner.presence.release()
        self.Finished(json.dumps(payload))

    def _progress(self, event: Event) -> None:
        if (line := describe(event)) is not None:
            self.Progress(line)
        if (step := doing(event)) is not None:
            self._set_step(step)

    def _set_step(self, step: str) -> None:
        if step != self._step:
            self._step = step
            self.Step(step)


async def serve(runner: TaskRunner, bus: MessageBus) -> None:
    """Export the service on `bus` and run until the bus disconnects."""
    service = AgentService(runner)
    service.on_quit = bus.disconnect
    bus.export(OBJECT_PATH, service)
    reply = await bus.request_name(BUS_NAME)
    if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
        raise SystemExit(f"{BUS_NAME} is already running")
    log.info("arceed ready on the session bus as %s", BUS_NAME)
    await bus.wait_for_disconnect()


async def _main() -> None:
    from arcee.platform.shell import ShellPresence

    load_env()
    settings = load_settings()
    runner = TaskRunner(core=settings.core, policy=settings.policy(), presence=ShellPresence())
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        await serve(runner, bus)
    finally:
        await runner.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())
