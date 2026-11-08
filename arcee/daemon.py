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
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from dbus_fast import BusType, DBusError, Message, MessageType, RequestNameReply
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusBool, DBusDouble, DBusStr, DBusUInt32
from dbus_fast.service import ServiceInterface, method, signal

from arcee.config import load_env
from arcee.context.conversation import Conversation, Turn
from arcee.core.agent import Escalated, Event, Guarded, ModelTurn, Planned, StepStarted, ToolRan
from arcee.core.protocol import ConfirmRequest
from arcee.jobs import Job
from arcee.models.errors import ModelUnavailableError
from arcee.runner import TaskRunner
from arcee.settings import load as load_settings
from arcee.settings import save_listen
from arcee.tools.voice import LONGEST_ASKED
from arcee.voice import Recorder, Transcriber, VoiceError
from arcee.voice_local import WakeListener, installed
from arcee.voice_reply import SPEECH, Speaker, speakable, voice_id

BUS_NAME: Final = "me.iteshxt.Arcee.Agent"
OBJECT_PATH: Final = "/me/iteshxt/Arcee/Agent"
BUSY_ERROR: Final = "me.iteshxt.Arcee.Error.Busy"
RECENT_LIMIT: Final = 20

log = logging.getLogger("arceed")


def short_model(name: str) -> str:
    """ "openai/gpt-oss-20b" as "gpt-oss-20b"; a local tag as it is."""
    return name.rsplit("/", 1)[-1]


@dataclass(slots=True)
class TaskStats:
    """A request's model calls, for the line under its reply in the app."""

    models: list[str] = field(default_factory=list[str])
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0  # the models' own time, rate-limit waits left out

    def add(self, turn: ModelTurn) -> None:
        name = short_model(turn.model)
        if name not in self.models:
            self.models.append(name)
        self.calls += 1
        self.tokens_in += turn.tokens_in
        self.tokens_out += turn.tokens_out
        self.seconds += max(0.0, turn.seconds - turn.waited)

    def summary(self, core: str) -> dict[str, Any]:
        speed = self.tokens_out / self.seconds if self.seconds > 0.05 else 0.0
        return {
            "core": core,
            "models": self.models,
            "model_calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model_seconds": round(self.seconds, 2),
            "tokens_per_s": round(speed),
        }


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
    "media_control": "Controlling the music",
    "media_now_playing": "Checking what's playing",
    "fs_usage": "Measuring folder sizes",
    "calendar_list": "Checking your calendar",
    "calendar_free": "Looking for free time",
    "calendar_add": "Adding {} to your calendar",
    "calendar_update": "Changing {}",
    "calendar_delete": "Deleting {}",
    "mail_list": "Checking your inbox",
    "mail_search": "Searching your mail for {}",
    "mail_read": "Reading the email",
    "mail_attachments": "Saving the attachments",
    "mail_draft": "Writing a draft",
    "mail_send": "Sending the email",
    "mail_update": "Tidying your mail",
}
THINKING: Final = "Thinking…"
SUBJECT_ARGS: Final = ("name", "app", "url", "query", "path", "what", "target", "title")
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


class Speech(Protocol):
    """What turns recorded speech into text: Groq's Whisper (a fake in tests)."""

    async def transcribe(self, pcm: bytes) -> str: ...


# How long the wake-word listener waits before trying again after it fails (the mic
# unplugged, PipeWire restarting).
WAKE_RETRY_S = 10.0
# The wake word held while the user trains it: it comes back by itself after this, in
# case whatever held it went away without saying so.
HOLD_WAKE_S = 180.0
# A spoken request's question opens the mic for its answer, and again if the answer was
# not understood: this many times in all, then the buttons alone.
ANSWER_TRIES: Final = 2
# Said after the wake word (or Whisper's take on "Arcee stop": "Arcee stopped."), these
# stop what is running instead of becoming a request.
STOP_WORDS: Final = re.compile(
    r"^(hey\s+)?((arcee|rc|r\s?c|our\s?c)\W*)?(stop(ped)?|cancel|never\s?mind|enough|"
    r"that'?s (all|enough)|quiet|shut up|hold on|wait)(\s+(it|now|please|that|arcee))*$",
    re.IGNORECASE,
)


def is_stop(text: str) -> bool:
    return bool(STOP_WORDS.match(re.sub(r"[^\w\s']", " ", text).strip()))


# A spoken answer to a question on screen ("yes", "go ahead", "the second one").
YES: Final = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|allow|do it|please do|of course|fine|"
    r"alright|all right|right)\b",
    re.IGNORECASE,
)
NO: Final = re.compile(r"^(no|nope|nah|don'?t|do not|deny|cancel|never|not now)\b", re.IGNORECASE)
# "Always allow" said: yes, and never ask again for this kind of action.
ALWAYS: Final = re.compile(
    r"^(always( allow( it)?)?|allow (it )?always|(yes )?(and )?don'?t ask (me )?again)\b",
    re.IGNORECASE,
)
ORDINALS: Final = ("first", "second", "third", "fourth", "fifth")
# Said for a request that failed: the reason is on screen, and rarely reads well aloud.
FAILED_ALOUD: Final = "Sorry, that did not work. The reason is on screen."
# Routines (routine.add): run up to this late, waiting this long for Arcee to be free.
ROUTINE_LATE_S: Final = 2 * 3600.0
ROUTINE_WAIT_S: Final = 30 * 60.0
ROUTINE_POLL_S: Final = 5.0
JOB_LOOK_S: Final = 1.0  # a finished job waits this often for its task to end


async def notify(title: str, body: str) -> None:
    """A plain desktop notification from Arcee (a job done, a watch given up)."""
    try:
        process = await asyncio.create_subprocess_exec(
            "notify-send",
            "-a",
            "Arcee",
            "-i",
            "dialog-information",
            title,
            body,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
    except OSError:
        log.warning("could not notify: %s: %s", title, body)


PREVIEW_ALOUD: Final = "Hi, I'm Arcee. I've set a reminder for 5 PM to call mom."


def question_aloud(asked: dict[str, Any]) -> str:
    """A question as said aloud, with how to answer it."""
    text = speakable(str(asked.get("text", "")))
    if asked.get("kind") == "confirm":
        allow = "Say allow, always allow, or deny." if asked.get("allowable") else ""
        return f"{text} {allow or 'Say allow or deny.'}".strip()
    options = [str(o) for o in asked.get("options", [])][: len(ORDINALS)]
    if len(options) >= 2:
        return f"{text} {', '.join(options[:-1])}, or {options[-1]}?"
    return text


def spoken_answer(
    text: str, kind: str, options: Sequence[str], allowable: bool = False
) -> str | None:
    """What the user said, as an answer to a question of `kind` with `options`; None
    when it answers nothing ("yes or no?"). "always" for a confirmation the user may
    allow for good (`allowable`)."""
    said = " ".join(re.sub(r"[^\w\s']", " ", text).split()).lower()
    yes, no = bool(YES.match(said)), bool(NO.match(said))
    if kind == "confirm" and allowable and ALWAYS.match(said):
        return "always"
    if kind == "confirm":
        return "yes" if yes else "no" if no else None
    words = " ".join(said.split())
    for option in options:
        wanted = " ".join(re.sub(r"[^\w\s']", " ", option).lower().split())
        if wanted and re.search(rf"\b{re.escape(wanted)}\b", words):
            return option  # whole words: "not sure" is not "no"
    for n, ordinal in enumerate(ORDINALS[: len(options)]):
        if re.search(rf"\b{ordinal}\b", said):
            return options[n]
    # "yes" to "Go ahead" / "No": the choice that says yes, or no.
    for option in options:
        lowered = option.lower()
        if (yes and YES.match(lowered)) or (no and NO.match(lowered)):
            return option
    if options and (yes or no):
        return None
    return text.strip()


async def keep_listening(service: AgentService) -> None:
    """Run the wake-word listener for as long as arceed runs, again after a failure."""
    while True:
        listener = WakeListener(
            on_wake=service.on_wake,
            on_request=service.on_request,
            on_stop=service.on_stop,
            on_level=service.Level,
        )
        service.wake = listener
        try:
            await listener.run()
        except VoiceError as exc:
            log.warning("not listening for the wake word: %s", exc)
            return  # the models are missing: nothing to retry until they are not
        except Exception:
            log.exception("the wake-word listener failed; starting it again")
        finally:
            service.wake = None
        await asyncio.sleep(WAKE_RETRY_S)


class AgentService(ServiceInterface):
    def __init__(
        self,
        runner: TaskRunner,
        *,
        recorder: Callable[[Callable[[float], None]], Recorder] = Recorder,
        transcriber: Speech | None = None,
        speaker: Speaker | None = None,
    ) -> None:
        super().__init__(BUS_NAME)
        self._runner = runner
        self._make_recorder = recorder
        self._transcriber = transcriber or Transcriber()
        # Says the answers to spoken requests (decision 0019), when the user turned it on.
        self._speaker = speaker or Speaker()
        self._speaking: asyncio.Task[None] | None = None  # `_say`, while it talks
        self._said_in_task = False  # the request had something said (`voice.say`)
        runner.say_aloud = self.say_aloud  # `voice.say`, for the requests run here
        self._recorder: Recorder | None = None
        self._listening: asyncio.Task[None] | None = None
        # The wake-word listener, when the user turned it on; paused for the mic button.
        self.wake: WakeListener | None = None
        self._wake_task: asyncio.Task[None] | None = None  # keep_listening, while on
        self._hearing: asyncio.Future[None] | None = None  # a woken request, transcribing
        self._held: asyncio.TimerHandle | None = None  # HoldWake's own end
        self._dismissed = False  # the mic button's recording was cancelled
        self._spoken = False  # the running request was spoken: its questions are heard too
        self._answer_tries: dict[str, int] = {}  # the mic opened for each question
        self._for_answer = False  # the recording now is for a question's answer
        self._stats = TaskStats()  # the running request's models, tokens and time
        # The turns a new request continues, until the user closes the pill (EndConversation).
        self.conversation = Conversation()
        self._task: asyncio.Task[None] | None = None
        self._request = ""
        self._step = ""
        self._questions: dict[str, asyncio.Future[str]] = {}
        self._asked: dict[str, dict[str, Any]] = {}  # the questions, to answer by voice
        self._ids = itertools.count(1)
        # Ends the service (set by `serve` to disconnect its bus).
        self.on_quit: Callable[[], None] = lambda: None

    # --- methods --------------------------------------------------------------------

    @method()
    def Run(self, request: DBusStr) -> DBusStr:
        if self.busy:
            raise DBusError(BUSY_ERROR, f"Arcee is already working on: {self._request}")
        self.start_request(request)
        return "started"

    def start_request(self, request: str) -> None:
        self._request = request.strip()
        self.hush()
        self._task = asyncio.ensure_future(self._run(self._request))

    async def run_routine(self, request: str, late: float) -> bool:
        """A routine came due (routine.add): its request runs as if the user had asked
        it, in a conversation of its own, once Arcee is free. One missed by more than
        ROUTINE_LATE_S (the computer off) is skipped. Whether it ran."""
        if late > ROUTINE_LATE_S:
            log.info("routine skipped, %d min late: %s", late // 60, request)
            return False
        waited = 0.0
        while self.busy and waited < ROUTINE_WAIT_S:
            await asyncio.sleep(ROUTINE_POLL_S)
            waited += ROUTINE_POLL_S
        if self.busy:
            log.info("routine skipped, Arcee stayed busy: %s", request)
            return False
        self.conversation.end()
        self.start_request(request)
        return True

    async def job_finished(self, job: Job) -> None:
        """A background job ended (decision 0020). The task that started it may still
        look (control.wait_for, job.check): once that task is over, a job it never saw
        end is told: by its follow-up request (`then`), or a notification."""
        while self._runner.current_task == job.task_id:  # noqa: ASYNC110 (the runner's)
            await asyncio.sleep(JOB_LOOK_S)
        if job.seen:
            return
        if not job.then:
            await notify(f"Done: {job.cmd[:60]}", f"It {job.state()}.")
            return
        from arcee.followups import Detail, request_for

        seen = f"The background command `{job.cmd}` ({job.id}) {job.state()}. " + (
            f"job.check(job={job.id!r}) shows its output."
        )
        detail = Detail(request=job.request, set_at=job.started)
        if not await self.run_routine(request_for(job.then, detail, seen), 0.0):
            await notify(f"Done: {job.cmd[:60]}", f"It {job.state()}.")

    @method()
    def Cancel(self) -> None:
        """Stop everything under way: the request running, and one being heard or
        transcribed, and an answer being said (Esc, Stop, or "Arcee stop")."""
        self._stop_task()
        self.hush()
        dropped = False
        if self.wake is not None and self.wake.drop():
            dropped = True
        if self._recorder is not None:
            self._dismissed = True
            self._recorder.stop()
            dropped = True
        if self._hearing is not None and not self._hearing.done():
            self._hearing.cancel()
            dropped = True
        if dropped:
            self.Listening(False)

    def _stop_task(self) -> bool:
        """Stop the request running, if one is; whether one was."""
        if self.busy and self._task is not None:
            self._runner.stopped = True
            self._task.cancel()
            return True
        return False

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
        self.hush()  # the mic would hear Arcee's own voice
        self._recorder = self._make_recorder(self._level)
        self._dismissed = False
        if self.wake is not None:
            self.wake.paused = True
        self._listening = asyncio.ensure_future(self._listen(self._recorder))

    def _hear_answer(self, question_id: str) -> None:
        """Open the mic for the answer to a spoken request's question, as a conversation
        would (the user started by voice; buttons alone made them reach for the mouse)."""
        tries = self._answer_tries.get(question_id, 0)
        if not self._spoken or tries >= ANSWER_TRIES or question_id not in self._asked:
            return
        self._answer_tries[question_id] = tries + 1
        self._for_answer = True
        self.StartListening()

    @method()
    def HoldWake(self, hold: DBusBool) -> None:
        """Stop hearing the wake word (while it is trained), or hear it again."""
        if self._held is not None:
            self._held.cancel()
            self._held = None
        if self.wake is not None:
            self.wake.paused = hold
            if not hold:
                self.wake.reload()  # what was trained meanwhile
        if hold:
            self._held = asyncio.get_running_loop().call_later(
                HOLD_WAKE_S, lambda: self.HoldWake(False)
            )

    @method()
    async def ResumeConversation(self, conversation_id: DBusStr) -> None:
        await self.resume(conversation_id)

    async def resume(self, conversation_id: str) -> None:
        """Take up an earlier conversation from History (the Agent page's chats): the
        next request continues it, its last turns given as before."""
        rows = await self._runner.store.conversation(conversation_id)
        if not rows:
            raise DBusError("me.iteshxt.Arcee.Error.NotFound", "no such conversation")
        turns = [Turn(t.request, t.reply or "", t.status, tuple(did)) for t, did in rows]
        self.conversation.resume(conversation_id, turns)

    @method()
    def EndConversation(self) -> None:
        """The user closed the pill (or started a New conversation): the next request
        starts afresh. Closing it also stops an answer being said."""
        self.conversation.end()
        self.hush()

    @method()
    def PreviewVoice(self, voice: DBusStr) -> None:
        """Say a sentence in one of the voices, by name (Settings → Voice)."""
        self.preview_voice(voice)

    def preview_voice(self, voice: str) -> None:
        if installed(SPEECH):
            self._say(PREVIEW_ALOUD, voice=voice)

    @method()
    def Say(self, text: DBusStr) -> DBusStr:
        """Say `text` aloud (the Agent page's Read aloud); empty, or why it cannot."""
        return self.say_text(text)

    def say_text(self, text: str) -> str:
        return self.say_aloud(speakable(text, longest=LONGEST_ASKED)) or ""

    @method()
    def Hush(self) -> None:
        """Stop saying anything."""
        self.hush()

    def say_aloud(self, text: str) -> str | None:
        """Start saying `text` (already speakable); None, or why it cannot."""
        if not installed(SPEECH):
            return "Arcee's voice is not downloaded: turn on Answer aloud in Settings → Voice"
        if self.busy:
            self._said_in_task = True  # the reply is not said over it
        self._say(text)
        return None

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

    def listen_for_wake(self, on: bool) -> None:
        """Start the wake-word listener, or stop it: off, the microphone is closed."""
        running = self._wake_task is not None and not self._wake_task.done()
        if on and not running:
            self._wake_task = asyncio.ensure_future(keep_listening(self))
        elif not on and self._wake_task is not None:
            self._wake_task.cancel()
            self._wake_task = None

    @method()
    def SetWakeWord(self, on: DBusBool) -> None:
        """Listen for "hey Arcee", or stop listening at all (the panel's switch, the
        app's): at once, and from the next login too."""
        save_listen(on)
        self.listen_for_wake(on)
        self.WakeWordChanged(on)

    @property
    def hears_wake(self) -> bool:
        return self._wake_task is not None and not self._wake_task.done()

    @method()
    def WakeWordOn(self) -> DBusBool:
        return self.hears_wake

    @signal()
    def WakeWordChanged(self, on: bool) -> DBusBool:
        return on

    @method()
    def Info(self) -> DBusStr:
        """What the Agent page shows when idle: the models in use and the wake word."""
        ladder = getattr(self._runner, "ladder", [])
        return json.dumps(
            {
                "core": getattr(self._runner, "core", ""),
                "models": [short_model(m.profile.model) for m in ladder],
                "wake_word": self.hears_wake,
            }
        )

    @method()
    def Status(self) -> DBusStr:
        return json.dumps(
            {
                "state": "running" if self.busy else "idle",
                "request": self._request,
                "step": self._step if self.busy else "",
                "turns": len(self.conversation.turns) if self.conversation.active else 0,
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

    @signal()
    def Answered(self, question_id: str) -> DBusStr:
        return question_id

    # --- voice ------------------------------------------------------------------------

    def _level(self, loudness: float) -> None:
        self.Level(loudness)

    async def _listen(self, recorder: Recorder) -> None:
        """Record (the mic button), then hear it like a woken request."""
        self.Listening(True)
        try:
            audio, spoke = await recorder.record()
        except VoiceError as exc:
            self.Listening(False)
            self.Heard(json.dumps({"error": str(exc)}))
            return
        finally:
            self._recorder = None
            self._for_answer = False
            if self.wake is not None:
                self.wake.paused = False
        self.Listening(False)
        if self._dismissed:
            return  # cancelled: nothing is sent or run
        await self._hear(audio if spoke else b"")

    # --- the wake word (WakeListener's callbacks) ---------------------------------------

    def on_wake(self) -> None:
        self.Listening(True)

    def on_request(self, audio: bytes) -> None:
        self.Listening(False)
        self._hearing = asyncio.ensure_future(self._hear(audio, woken=True))

    def on_stop(self) -> None:
        self.Cancel()

    async def _hear(self, audio: bytes, *, woken: bool = False) -> None:
        """Transcribe, tell the panel and the app the text, and run it."""
        heard: dict[str, str]
        if not audio:
            heard = {"error": "I did not hear anything"}
        else:
            started = time.monotonic()
            try:
                heard = {"text": await self._transcriber.transcribe(audio)}
            except VoiceError as exc:
                heard = {"error": str(exc)}
            # The voice gate (spec section 14): end of speech to transcript under 1.5 s.
            # Speech ended END_SILENCE_S before the wake listener handed the audio over.
            log.info(
                "heard %.1f s of speech, transcribed in %.2f s by %s",
                len(audio) / 2 / 16000,
                time.monotonic() - started,
                type(self._transcriber).__name__,
            )
        text = heard.get("text", "").strip()
        unanswered = bool(self._asked) and not text  # nothing heard while a question waits
        if woken and self.wake is not None:
            # Words that were not a stop: the wake that led to them was meant, and its
            # "hey Arcee" is learned (decision 0015). Nothing heard: it is not.
            self.wake.learn(bool(text) and not is_stop(text))
        if text and is_stop(text):
            heard = {"error": "Stopped." if self._stop_task() else "Nothing to stop."}
            text = ""
        elif text and self._asked:
            # A question is waiting: what was said answers it.
            question_id, asked = next(reversed(self._asked.items()))
            answer = spoken_answer(
                text, asked["kind"], asked.get("options", []), bool(asked.get("allowable"))
            )
            if answer is None:
                heard = {
                    "error": "Say “allow” or “deny”."
                    if asked["kind"] == "confirm"
                    else "Say one of the choices."
                }
                unanswered = True
            else:
                heard = {"text": text, "answer": answer}
                self.Answer(question_id, answer)
            text = ""
        elif text and self.busy:
            heard = {"error": f"Still working on: {self._request}. Say “Arcee stop” first."}
            text = ""
        self.Heard(json.dumps(heard))
        if unanswered and self._asked:  # listen once more for it
            self._hear_answer(next(reversed(self._asked)))
        # What was heard runs here, once, whether the panel or the app is open.
        if text:
            self._request = text
            self._task = asyncio.ensure_future(self._run(text, spoken=True))

    # --- the agent's user: questions go to the panel ----------------------------------

    async def ask(self, question: str, options: Sequence[str] = ()) -> str:
        return await self._question({"kind": "ask", "text": question, "options": list(options)})

    async def confirm(self, request: ConfirmRequest) -> bool:
        answer = await self._question(
            {
                "kind": "confirm",
                "text": request.sentence,
                "preview": list(request.args_preview),
                "allowable": request.allowable,
            }
        )
        answer = answer.strip().lower()
        if answer == "always" and request.allowable:
            self._runner.allow_always(request.tool)  # from now on, and remembered
        return answer in {"y", "yes", "allow", "always"}

    async def _question(self, payload: dict[str, Any]) -> str:
        question_id = f"q{next(self._ids)}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._questions[question_id] = future
        self._asked[question_id] = payload
        # The user answers with the mouse: give control back so that is not a pause.
        presence = self._runner.presence
        held = presence.held
        await presence.release()
        self._set_step("Waiting for your answer")
        self.Question(json.dumps({"id": question_id, **payload}))
        if self._spoken and self._speaks():
            # Said first, then the mic opens for the answer: it would hear the question.
            self._say(question_aloud(payload), then=lambda: self._hear_answer(question_id))
        else:
            self._hear_answer(question_id)
        try:
            return await future
        finally:
            self._questions.pop(question_id, None)
            self._asked.pop(question_id, None)
            self._answer_tries.pop(question_id, None)
            if self._speaking is not None and not self._speaking.done():
                self.hush()  # answered with a click while the question was being said
            if self._for_answer and self._recorder is not None:
                # Answered with a click while the mic listened: what it hears is not a
                # request ("yes" would have run as one).
                self._dismissed = True
                self._recorder.stop()
            self.Answered(question_id)  # every place showing it closes it
            if held:
                await presence.acting()

    # --- running ----------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self, request: str, *, spoken: bool = False) -> None:
        self._spoken = spoken
        self._said_in_task = False
        self._stats = TaskStats()
        if spoken and self._speaks():
            self._speaker.warm()  # loaded while it thinks: the answer is heard sooner
        self.Started(request)
        self._set_step(THINKING)
        payload: dict[str, Any]
        try:
            result = await self._runner.run(
                request, self, self._progress, spoken=spoken, conversation=self.conversation
            )
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
            self._spoken = False
            with contextlib.suppress(Exception):
                await self._runner.presence.release()
        payload |= self._stats.summary(getattr(self._runner, "core", ""))
        self.Finished(json.dumps(payload))
        reply = str(payload.get("reply") or "")
        said = self._said_in_task  # "read it out loud": that is being said, not the reply
        if (
            spoken
            and reply
            and not said
            and payload["status"] in {"done", "failed"}
            and (self._speaks())
        ):
            self._say(speakable(reply) if payload["status"] == "done" else FAILED_ALOUD)

    # --- saying it aloud (decision 0019) -------------------------------------------------

    def _speaks(self) -> bool:
        """Whether answers to spoken requests are said: on, and the voice downloaded."""
        return load_settings().speak_replies and installed(SPEECH)

    def _say(self, text: str, then: Callable[[], None] | None = None, *, voice: str = "") -> None:
        """Say `text` (in `voice`, or the user's), the wake word not listening meanwhile
        (it would hear Arcee's own voice), then `then` unless it was hushed."""
        self.hush()
        self._speaking = asyncio.ensure_future(self._saying(text, then, voice))

    async def _saying(self, text: str, then: Callable[[], None] | None, voice: str) -> None:
        wake = self.wake
        quieted = wake is not None and not wake.paused
        if quieted and wake is not None:
            wake.paused = True
        finished = False
        try:
            await self._speaker.say(text, voice_id(voice or load_settings().voice))
            finished = True
        except asyncio.CancelledError:
            pass
        except Exception:  # a voice that fails must not take the reply or the service
            log.exception("could not say the reply")
            finished = True
        finally:
            listening = self._listening is not None and not self._listening.done()
            # Back on, unless the mic button or training has taken the mic meanwhile.
            if quieted and wake is not None and self.wake is wake:
                wake.paused = listening or self._held is not None
        if finished and then is not None:
            then()

    def hush(self) -> bool:
        """Stop saying anything at once; whether something was being said."""
        task, self._speaking = self._speaking, None
        stopped = self._speaker.stop()
        if task is not None and not task.done():
            task.cancel()
            return True
        return stopped

    def _progress(self, event: Event) -> None:
        if isinstance(event, ModelTurn):
            self._stats.add(event)
        if (line := describe(event)) is not None:
            self.Progress(line)
        if (step := doing(event)) is not None:
            self._set_step(step)

    def _set_step(self, step: str) -> None:
        if step != self._step:
            self._step = step
            self.Step(step)


async def watch_shell_stop(bus: MessageBus, service: AgentService) -> None:
    """Cancel the request when the user stops Arcee from the shell.

    The extension emits Stopped for the stop shortcut (Super+Shift+Escape) and the
    Stop pill. Without this only the panel's own Stop reached arceed: the pill and
    the shortcut took the glow away while the request went on.
    """
    from arcee.platform.shell import BUS_NAME as SHELL_INTERFACE

    def on_message(message: Message) -> None:
        if (
            message.message_type == MessageType.SIGNAL
            and message.interface == SHELL_INTERFACE
            and message.member == "Stopped"
        ):
            service.Cancel()

    bus.add_message_handler(on_message)
    await bus.call(
        Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch",
            signature="s",
            body=[f"type='signal',interface='{SHELL_INTERFACE}',member='Stopped'"],
        )
    )


async def start_reminders(bus: MessageBus, service: AgentService) -> asyncio.Task[None] | None:
    """Fire timers and reminders while arceed runs (decision 0018)."""
    from arcee.context.store import ReminderRow
    from arcee.reminders import DesktopNotifier, Scheduler

    store = getattr(service._runner, "store", None)  # pyright: ignore[reportPrivateUsage]
    if store is None:
        return None
    notifier = DesktopNotifier(bus, store)
    try:
        await notifier.watch()
    except Exception:  # the snooze button only; reminders still fire
        log.exception("not watching the reminder notifications")

    running: set[asyncio.Task[Any]] = set()

    def later(work: Awaitable[Any]) -> None:
        # A routine or follow-up waits for Arcee to be free (up to half an hour): the
        # scheduler goes on firing timers meanwhile.
        task = asyncio.ensure_future(work)
        running.add(task)
        task.add_done_callback(running.discard)

    async def fire(reminder: ReminderRow, late: float) -> None:
        if reminder.kind == "routine":
            later(service.run_routine(reminder.text, late))
        elif reminder.kind == "followup":
            from arcee import followups

            later(
                followups.fire(
                    reminder.id,
                    reminder.text,
                    reminder.detail,
                    late,
                    store=store,
                    run_request=service.run_routine,
                    notify=notify,
                )
            )
        else:
            await notifier.show(reminder, late)

    scheduler = Scheduler(store, fire)
    store.on_reminders(scheduler.poke)
    return asyncio.ensure_future(scheduler.run())


async def serve(runner: TaskRunner, bus: MessageBus, *, listen: bool = False) -> None:
    """Export the service on `bus` and run until the bus disconnects."""
    service = AgentService(runner)
    service.on_quit = bus.disconnect
    bus.export(OBJECT_PATH, service)
    await watch_shell_stop(bus, service)
    reply = await bus.request_name(BUS_NAME)
    if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
        raise SystemExit(f"{BUS_NAME} is already running")
    log.info("arceed ready on the session bus as %s", BUS_NAME)
    service.listen_for_wake(listen)
    reminders = await start_reminders(bus, service)
    from arcee.jobs import JOBS

    JOBS.on_finished = service.job_finished  # background jobs (decision 0020)
    days = asyncio.ensure_future(runner.keep_days())  # day and week summaries
    try:
        await bus.wait_for_disconnect()
    finally:
        service.listen_for_wake(False)
        days.cancel()
        if reminders is not None:
            reminders.cancel()


async def _main() -> None:
    from arcee.platform.shell import ShellPresence

    load_env()
    settings = load_settings()
    runner = TaskRunner(
        core=settings.core,
        policy=settings.policy(),
        presence=ShellPresence(),
        memory=settings.memory,
        sharing=settings.personal_to_cloud,
    )
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        await serve(runner, bus, listen=settings.listen)
    finally:
        await runner.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())
