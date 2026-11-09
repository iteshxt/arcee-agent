"""`arcee-cli`: run one task from the terminal (roadmap 1.17).

Confirmations and questions are answered on stdin. Every step is printed: the
model's call, the result, guards and escalations, so a run doubles as a trace.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Literal, TextIO

from arcee import install
from arcee.browser.client import BrowserClient, BrowserError
from arcee.config import load_env
from arcee.context.examples import ExampleLibrary
from arcee.context.store import Store, data_dir
from arcee.core.agent import (
    Escalated,
    Event,
    Guarded,
    Limits,
    ModelTurn,
    Planned,
    StepStarted,
    ToolRan,
)
from arcee.core.presence import NoPresence, Presence
from arcee.core.protocol import ConfirmRequest
from arcee.eval.cases import load as load_cases
from arcee.eval.runner import CaseResult, run_cases, summary
from arcee.eval.tasks import TaskResult, load_tasks, report_markdown, run_task, task_summary
from arcee.install import SHELL_EXTENSION
from arcee.models.base import ModelAdapter
from arcee.models.errors import ModelUnavailableError
from arcee.models.ollama import DEFAULT_HOST
from arcee.models.presets import (
    DEFAULT_CORE,
    core_ladder,
    planner_for,
    rate_budget,
    share_budget,
)
from arcee.platform.screen import GnomeScreen
from arcee.platform.shell import ShellBridge, ShellPresence, ShellUnavailableError, StopWatcher
from arcee.runner import TaskRunner
from arcee.settings import load as load_settings
from arcee.tools.builtin import builtin_registry


class Style:
    def __init__(self, stream: TextIO) -> None:
        self._on = stream.isatty() and "NO_COLOR" not in os.environ

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._on else text

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)


class Terminal:
    """Prints events and answers the agent's questions and confirmations on stdin."""

    def __init__(self, *, verbose: bool, out: TextIO = sys.stdout) -> None:
        self._verbose = verbose
        self._out = out
        self._style = Style(out)
        # Given back while the user answers, so typing the answer is not taken for the
        # user taking over (which would pause Arcee).
        self.presence: Presence = NoPresence()

    def print(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    def on_event(self, event: Event) -> None:
        s = self._style
        match event:
            case ModelTurn(
                call=call, thinking=thinking, model=model, seconds=seconds, waited=waited
            ):
                if waited >= 1:
                    self.print(s.dim(f"  \u23f3 waited {waited:.0f}s for the rate budget"))
                if thinking and self._verbose:
                    self.print(s.dim(f"  thinking: {' '.join(thinking.split())[:300]}"))
                if call is not None:
                    args = " ".join(f"{k}={v!r}" for k, v in call.args.items())
                    self.print(
                        f"{s.bold('→')} {call.name} {args}  {s.dim(f'{model}, {seconds:.1f}s')}"
                    )
            case ToolRan(result=result):
                mark = s.green("✓") if result.status == "ok" else s.red("✗")
                self.print(f"  {mark} {result.summary}  {s.dim(f'{result.duration_ms} ms')}")
            case Guarded(what=what):
                self.print(s.yellow(f"  ⚑ guard: {what}"))
            case Escalated(tier=tier, model=model, reason=reason):
                label = f"tier {tier + 1}, {model}" + (", thinking" if tier == 1 else "")
                self.print(s.yellow(f"  ↑ escalating to {label}: {reason[:120]}"))
            case Planned(plan=plan, seconds=seconds, attempt=attempt):
                title = "plan" if attempt == 0 else f"new plan (replan {attempt})"
                self.print(s.bold(f"{title}: ") + plan.goal + "  " + s.dim(f"{seconds:.1f}s"))
                for step in plan.steps:
                    tools = ", ".join(step.tools)
                    self.print(f"  {step.id}. {step.do}  " + s.dim(f"[{tools}]"))
            case StepStarted(step=step, total=total, do=do):
                if total > 1:
                    self.print(s.bold(f"step {step}/{total}: ") + do)

    async def confirm(self, request: ConfirmRequest) -> bool:
        lines = [self._style.yellow(f"? {request.sentence}")]
        lines += [f"    {target}" for target in request.args_preview]
        self.print("\n".join(lines))
        answer = await self._input("  Allow? [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    async def ask(self, question: str, options: Sequence[str] = ()) -> str:
        self.print(self._style.yellow(f"? {question}"))
        if options:
            self.print("    " + " / ".join(options))
        return (await self._input("  > ")).strip()

    async def _input(self, prompt: str) -> str:
        held = self.presence.held
        await self.presence.release()
        try:
            return await self._read(prompt)
        finally:
            if held:
                await self.presence.acting()

    async def _read(self, prompt: str) -> str:
        # On a daemon thread, not the default executor: a pending input() there kept
        # Python from exiting on Ctrl+C and printed a threading traceback.
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[str] = loop.create_future()

        def settle(value: str) -> None:
            if not answer.done():
                answer.set_result(value)

        def read() -> None:
            try:
                value = input(prompt)
            except EOFError, KeyboardInterrupt:
                value = ""
            with contextlib.suppress(RuntimeError):  # Arcee already exited
                loop.call_soon_threadsafe(settle, value)

        threading.Thread(target=read, daemon=True, name="arcee-input").start()
        return await answer


async def _usable(ladder: Sequence[ModelAdapter], terminal: Terminal) -> list[ModelAdapter]:
    """The ladder's tiers whose model is pulled; empty when the first tier is missing."""
    healthy = await asyncio.gather(*(model.health() for model in ladder))
    if not healthy[0]:
        return []
    for model, ok in zip(ladder, healthy, strict=True):
        if not ok:
            terminal.print(
                f"note: {model.profile.model} is not pulled, so escalation skips it "
                f"(ollama pull {model.profile.model})"
            )
    return [model for model, ok in zip(ladder, healthy, strict=True) if ok]


async def run(
    request: str,
    *,
    host: str,
    limits: Limits,
    verbose: bool,
    plan: Literal["auto", "always", "never"],
    examples: bool = False,
    core: str | None = None,
    memory: bool = True,
) -> int:
    terminal = Terminal(verbose=verbose)
    presence = terminal.presence = ShellPresence()
    settings = load_settings()
    runner = TaskRunner(
        core=core or settings.core,
        host=host,
        limits=limits,
        plan=plan,
        examples=examples,
        presence=presence,
        policy=settings.policy(),
        memory=memory and settings.memory,
        sharing=settings.personal_to_cloud,
    )
    try:
        notes, problem = await runner.check()
        for note in notes:
            terminal.print(note)
        if problem:
            terminal.print(problem)
            return 2
        running = asyncio.ensure_future(runner.run(request, terminal, terminal.on_event))
        stop_key = StopKey(running, terminal, on_stop=lambda: setattr(runner, "stopped", True))
        try:
            result = await running
        except ModelUnavailableError as exc:
            terminal.print(f"model unavailable: {exc}")
            return 2
        except asyncio.CancelledError:
            if stop_key.pressed:
                terminal.print(f"\nstopped with {STOP_SHORTCUT}")
                return 130
            raise
        finally:
            stop_key.close()
        style = Style(sys.stdout)
        terminal.print()
        terminal.print(style.bold("Arcee: ") + result.reply)
        terminal.print(
            style.dim(
                f"({result.status}: {result.steps} steps, {result.model_calls} model calls, "
                f"{result.seconds:.1f}s, {result.task_id})"
            )
        )
        return 0 if result.status == "done" else 1
    finally:
        await runner.aclose()


class StopKey:
    """Cancels `task` when the user stops Arcee (shortcut or Stop pill); says when paused."""

    def __init__(
        self,
        task: asyncio.Future[Any],
        terminal: Terminal,
        on_stop: Callable[[], None] = lambda: None,
    ) -> None:
        self.pressed = False
        loop = asyncio.get_running_loop()

        def stop() -> None:
            self.pressed = True
            on_stop()
            task.cancel()

        def later(action: Callable[[], None]) -> Callable[[], None]:
            # The watcher calls from its own thread; act on the event loop.
            def run() -> None:
                loop.call_soon_threadsafe(action)

            return run

        self._watcher = StopWatcher(
            later(stop),
            on_pause=later(
                lambda: terminal.print("  ⏸ paused: you have control (Resume or Stop at the top)")
            ),
            on_resume=later(lambda: terminal.print("  ▶ resumed")),
        )
        self._watcher.start()

    def close(self) -> None:
        self._watcher.stop()
        # Give the screen back: the glow goes when the task ends.
        with contextlib.suppress(Exception):
            ShellBridge().end_control()


async def history(task_id: str | None, limit: int) -> int:
    """List recent tasks, or show one task's steps."""
    style = Style(sys.stdout)
    store = Store(data_dir())
    try:
        if task_id is None:
            for task in await store.recent(limit):
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(task.started))
                mark = style.green("done") if task.status == "done" else style.red(task.status)
                print(f"{task.id}  {when}  {mark:<6}  {task.request[:70]}")
            return 0
        found = await store.task(task_id)
        if found is None:
            print(f"no task {task_id}")
            return 1
        task, steps = found
        print(style.bold(task.request))
        for step in steps:
            approval = f", approved by {step.approved_by}" if step.approved_by else ""
            print(
                f"  {step.n}. {step.tool} {json.dumps(step.args)}  "
                + style.dim(f"[{step.tier}{approval}] {step.status} {step.duration_ms} ms")
            )
            print(f"     {step.summary}")
        print(style.bold("Arcee: ") + (task.reply or ""))
        print(style.dim(f"({task.status}: {task.steps} steps, {task.model_calls} model calls)"))
        return 0
    finally:
        store.close()


async def evaluate(
    cases_path: Path,
    *,
    host: str,
    tier: int,
    repeat: int,
    only: str,
    report: Path | None,
    raw: bool,
    examples: bool,
    core: str = DEFAULT_CORE,
) -> int:
    """Run the eval suite on one tier of the local ladder and print the results."""
    cases = [case for case in load_cases(cases_path) if only in case.id or only in case.scenario]
    ladder = core_ladder(core, host)
    if tier > len(ladder):
        print(f"core {core!r} has {len(ladder)} tier(s)")
        return 2
    model = ladder[tier - 1]
    style = Style(sys.stdout)
    try:
        if not await model.health():
            print(f"{model.profile.model} is not available: ollama pull {model.profile.model}")
            return 2
        thinking = ", thinking" if model.profile.thinking else ""
        print(f"{len(cases)} cases x{repeat} on tier {tier} ({model.profile.model}{thinking})\n")

        def show(result: CaseResult) -> None:
            mark = style.green("PASS") if result.passed else style.red("FAIL")
            line = f"{mark} {result.case.id:<34} {result.seconds:5.1f}s"
            if result.guarded:
                line += "  " + style.yellow(f"[guard: {result.guarded}]")
            if not result.passed:
                line += "  " + style.dim("; ".join(result.problems)[:150])
            print(line, flush=True)

        library = ExampleLibrary.load() if examples else None
        results = await run_cases(
            model,
            builtin_registry(),
            cases,
            repeat=repeat,
            guards=not raw,
            examples=library,
            on_result=show,
        )
    finally:
        await asyncio.gather(*(m.aclose() for m in ladder))
    print("\n" + summary(results))
    if report is not None:
        text = json.dumps([_row(r) for r in results], indent=1)
        await asyncio.to_thread(report.write_text, text)
    return 0


async def evaluate_tasks(
    tasks_path: Path,
    *,
    host: str,
    only: str,
    plan: str,
    examples: bool,
    core: str = "local",
    report: Path | None = None,
) -> int:
    """Run whole tasks end to end in throwaway homes and check their outcomes."""
    tasks = [task for task in load_tasks(tasks_path) if only in task.id]
    ladder = core_ladder(core, host)
    planner_model = planner_for(core, host)
    share_budget(ladder, planner_model)
    style = Style(sys.stdout)
    terminal = Terminal(verbose=False)
    library = ExampleLibrary.load() if examples else None
    results: list[TaskResult] = []
    usable: list[Any] = []
    try:
        usable = await _usable(ladder, terminal)
        if not usable:
            print(f"{ladder[0].profile.model} is not available")
            return 2
        print(f"{len(tasks)} tasks, plan={plan}, examples={'on' if examples else 'off'}\n")
        for task in tasks:
            result = await run_task(
                task,
                usable,
                planner_model=planner_model,
                plan=plan,
                examples=library,
            )
            results.append(result)
            mark = style.green("PASS") if result.passed else style.red("FAIL")
            line = (
                f"{mark} {task.id:<28} {result.steps:>2} steps {result.model_calls:>2} calls "
                f"{result.seconds:5.1f}s" + (" planned" if result.planned else "")
            )
            if not result.passed:
                line += "  " + style.dim("; ".join(result.problems)[:140])
            print(line, flush=True)
    finally:
        await asyncio.gather(*(m.aclose() for m in [*ladder, planner_model]))
    print("\n" + task_summary(results))
    if report is not None and results:
        from datetime import datetime

        models = ", ".join(m.profile.model for m in usable)
        text = report_markdown(
            results,
            title=f"Whole-task eval, {core} core, {datetime.now():%Y-%m-%d %H:%M}",
            notes=[
                f"{len(results)} tasks from `{tasks_path}`"
                + (f" matching {only!r}" if only else ""),
                f"models: {models}; planner: {planner_model.profile.model}; plan={plan}",
                "tools: every built-in tool; network, desktop and account tools stubbed",
            ],
        )
        await asyncio.to_thread(_write, report, text)
        print(f"report: {report}")
    return 0


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _print_report(report: install.Report) -> int:
    for line in report.lines:
        print(line)
    if report.next:
        print()
        for line in report.next:
            print(line)
    return 0


def browser_setup() -> int:
    return _print_report(install.browser_bridge())


STOP_SHORTCUT: Final = "Super+Shift+Escape"


def shell_extension() -> int:
    """Link the Arcee GNOME Shell extension into the user's extensions folder."""
    return _print_report(install.shell_extension())


async def shell_check() -> int:
    bridge = ShellBridge()
    available = await asyncio.to_thread(bridge.available)
    if not available:
        print(f"the Arcee shell extension is not running ({SHELL_EXTENSION}); see shell-extension")
        return 1
    windows = await asyncio.to_thread(bridge.windows)
    print(f"connected to the Arcee shell extension; {len(windows)} windows")
    for window in windows[:10]:
        print(f"  {'*' if window['focused'] else ' '} {window['title'][:70]}")
    return 0


def daemon_install() -> int:
    """The systemd user unit, D-Bus activation and app launcher for arceed and the app."""
    return _print_report(install.daemon())


async def screen_check(seconds: int) -> int:
    """Take control (glow and Stop pill), read the screen with local OCR, then hold
    control so the user can try the pause. Nothing leaves the machine."""
    bridge = ShellBridge()
    if not await asyncio.to_thread(bridge.available):
        print(f"the Arcee shell extension is not running ({SHELL_EXTENSION}); see shell-extension")
        return 1
    screen = GnomeScreen(bridge)
    seen: set[str] = set()
    try:
        shot = await screen.capture()
        lines = await screen.read(shot)
        print(f"screen {shot.screen_width}x{shot.screen_height}: {len(lines)} lines of text read")
        print(f"control is on for {seconds} s: move the mouse or press a key to pause,")
        print(f"then Resume or Stop in the pill at the top ({STOP_SHORTCUT} also stops)")
        for _ in range(seconds * 2):
            state = await asyncio.to_thread(bridge.control_state)
            if state not in seen:
                seen.add(state)
                print(f"  {state}")
            if state == "off":
                break
            await asyncio.sleep(0.5)
    except ShellUnavailableError as exc:
        print(f"failed: {exc}")
        return 1
    finally:
        await asyncio.to_thread(bridge.end_control)
    print("paused by your input: works" if "paused" in seen else "never paused")
    return 0


def show_limits() -> int:
    """What Groq last reported for each model, with what has refilled since."""
    snapshot = rate_budget().snapshot()
    if not snapshot:
        print("no rate limits recorded yet; they appear after the first Groq request")
        return 0
    for model, s in snapshot.items():
        parts = [f"{model:<24}"]
        if "tokens_limit" in s:
            parts.append(f"tokens {s['tokens_left']:>5}/{s['tokens_limit']} this minute")
        if "day_tokens_limit" in s:
            parts.append(f"{s['day_tokens_left']:>7,}/{s['day_tokens_limit']:,} today")
        if "requests_limit" in s:
            parts.append(f"requests {s['requests_left']:>4}/{s['requests_limit']} today")
        print("   ".join(parts))
    return 0


async def browser_check() -> int:
    client = BrowserClient()
    try:
        info = await client.request("ping")
        tabs = await client.request("tabs")
    except BrowserError as exc:
        print(f"not connected: {exc}")
        return 1
    finally:
        await client.aclose()
    print(f"connected to the Arcee extension {info['version']}; {len(tabs)} tabs open")
    for tab in tabs[:10]:
        print(f"  {'*' if tab['active'] else ' '} {tab['title'][:70]}")
    return 0


async def browser_reload() -> int:
    """After the extension's files change: no trip to chrome://extensions."""
    client = BrowserClient()
    try:
        before = (await client.request("ping"))["version"]
        await client.request("reload")
    except BrowserError as exc:
        print(f"could not reload: {exc}")
        print("(extensions older than 0.3.6 cannot reload themselves: use chrome://extensions)")
        return 1
    finally:
        await client.aclose()
    print(f"Chrome is reloading the Arcee extension (was {before})")
    return 0


async def voice_test(seconds: float, file: str | None) -> int:
    """Record (or read) a few seconds and show what tier one hears: speech, words, wake."""
    import wave

    from arcee import voice_local, wake_match
    from arcee.voice import RECORD_COMMAND, Transcriber, VoiceError

    if missing := voice_local.missing():
        print(f"voice models not downloaded: {', '.join(m.name for m in missing)}")
        return 2
    saved = data_dir() / "voice-test.wav"
    if file:
        with wave.open(file) as w:
            pcm = w.readframes(w.getnframes())
    else:
        print(f'Recording {seconds:g} s: say "Hey Arcee" three times, with a pause between...')
        process = await asyncio.create_subprocess_exec(
            *RECORD_COMMAND, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        assert process.stdout is not None
        pcm = await process.stdout.readexactly(int(seconds * 16000) * 2)
        process.terminate()
        await process.wait()
        with wave.open(str(saved), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        print(f"saved: {saved} (stays on this computer)")
    samples = voice_local.samples(pcm)

    # Speech: what the VAD cut out, and what Whisper hears in each piece.
    vad = voice_local.make_vad()
    pieces: list[list[float]] = []
    for start in range(0, len(samples), voice_local.FRAME):
        vad.accept_waveform(samples[start : start + voice_local.FRAME])
        while not vad.empty():
            pieces.append(list(vad.front.samples))
            vad.pop()
    vad.flush()
    while not vad.empty():
        pieces.append(list(vad.front.samples))
        vad.pop()
    print(f"\nspeech: {len(pieces)} piece(s) heard")
    transcriber = Transcriber()
    for n, piece in enumerate(pieces, 1):
        try:
            words = await transcriber.transcribe(voice_local._pcm(piece))  # pyright: ignore[reportPrivateUsage]
        except VoiceError as exc:
            words = f"(not transcribed: {exc})"
        print(f"  {n}. {len(piece) / 16000:.1f} s: {words!r}")

    # The wake word: each spelling, at the listener's sensitivity and lower ones.
    print(
        "\nwake word (times heard, by spelling and threshold; the listener uses "
        f"{voice_local.THRESHOLD}):"
    )
    lines = [line for line in voice_local.KEYWORDS.splitlines() if "@HEY_ARCEE" in line]
    for line in lines:
        tokens = line.split(" @")[0]
        counts: list[str] = []
        for threshold in (voice_local.THRESHOLD, 0.15, 0.1, 0.06):
            spotter = voice_local.make_spotter(f"{tokens} :1.5 #{threshold} @HIT\n")
            stream = spotter.create_stream()
            hits = 0
            for start in range(0, len(samples) + 8000, voice_local.FRAME):
                piece = samples[start : start + voice_local.FRAME] or [0.0] * voice_local.FRAME
                stream.accept_waveform(16000, piece)
                while spotter.is_ready(stream):
                    spotter.decode_stream(stream)
                    if spotter.get_result(stream):
                        hits += 1
                        spotter.reset_stream(stream)
            counts.append(f"{threshold}: {hits}")
        print(f"  {tokens:24} " + "   ".join(counts))
    voice_local.make_spotter()  # the listener's own keywords file back as it was
    if (trained := wake_match.load()) is not None:
        heard = wake_match.log_mel(voice_local.Cleaner(voice_local.make_denoiser()).whole(samples))
        closest = min(
            trained.closest(wake_match.normalise(heard[i : i + wake_match.WINDOW]))
            for i in range(0, max(1, len(heard) - wake_match.WINDOW + 1), 8)
        )
        verdict = "wakes" if closest <= trained.threshold else "does not wake"
        print(
            f"\nyour trained wake word: closest {closest:.3f} "
            f"(wakes at {trained.threshold:.3f} or less): {verdict}"
        )
    return 0


# A voice this loud is clipped by the mic: distorted, it no longer sounds like the
# samples the wake word was trained on.
CLIPPING: Final = 0.95


async def voice_watch(words: bool) -> int:
    """The listener's judgement of each thing it hears, live: loudness, how near the
    wake word and "Arcee stop", the verdict, and (with words) what Whisper heard."""
    from datetime import datetime

    from arcee import voice_local, wake_match
    from arcee.voice import Transcriber, VoiceError

    if missing := voice_local.missing():
        print(f"voice models not downloaded: {', '.join(m.name for m in missing)}")
        return 2
    trained = wake_match.load()
    transcriber = Transcriber() if words else None
    pending: set[asyncio.Task[None]] = set()

    def stamp() -> str:
        return datetime.now().strftime("%H:%M:%S")

    async def say_words(prefix: str, pcm: bytes) -> None:
        if transcriber is None or not pcm:
            return
        try:
            text = await transcriber.transcribe(pcm)
        except VoiceError as exc:
            text = f"(not transcribed: {exc})"
        print(f"          {prefix} {text!r}", flush=True)

    def later(prefix: str, pcm: bytes) -> None:
        task = asyncio.ensure_future(say_words(prefix, pcm))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def burst(b: voice_local.Burst) -> None:
        loud = f"loud {b.loudness:.3f} (room {b.room:.3f})"
        if b.loudness >= CLIPPING:
            loud += " CLIPPING: turn the mic's input volume down"
        if b.kind == "noise":
            print(f"{stamp()}  sound, not speech  {loud}", flush=True)
            return
        verdict = {"wake": "WAKE", "stop": "STOP", "speech": "no"}[b.kind]
        near = ""
        if trained is not None:
            near = f"  wake {b.wake:.3f} (≤ {trained.threshold:.3f})"
            if trained.stops:
                near += f"  stop {b.stop:.3f} (≤ {trained.stop_threshold:.3f})"
        print(f"{stamp()}  speech {b.seconds:.1f} s  {loud}{near}  → {verdict}", flush=True)
        later("heard:", b.pcm)

    listener = voice_local.WakeListener(
        on_wake=lambda: print(f"{stamp()}  … listening for the request", flush=True),
        on_request=lambda pcm: (
            print(f"{stamp()}  request {len(pcm) / 2 / 16000:.1f} s", flush=True),
            later("request:", pcm),
        )[0],
        on_stop=lambda: None,
        on_burst=burst,
    )
    print("Watching the microphone (Ctrl+C to stop). Say “Hey Arcee” as you normally would.")
    if trained is None:
        print("No trained wake word: only the general keyword spotter listens.")
    else:
        print(
            f"Trained: wakes at a distance ≤ {trained.threshold:.3f}; "
            + ("“Arcee stop” trained." if trained.stops else "“Arcee stop” not trained.")
        )
    if words:
        print("What is heard goes to Groq's Whisper to show the words (--no-words: it does not).")
    print("arceed's own listening is paused meanwhile, so nothing runs.\n")

    async def hold() -> None:
        while True:  # arceed lets go of a hold after 3 minutes: renew it
            await wake_match.hold_wake(True)
            await asyncio.sleep(60)

    holding = asyncio.ensure_future(hold())
    try:
        await listener.run()
    except KeyboardInterrupt, asyncio.CancelledError:
        pass
    except VoiceError as exc:
        print(f"the listener stopped: {exc}")
    finally:
        holding.cancel()
        await wake_match.hold_wake(False)
        for task in pending:
            task.cancel()
    return 0


async def voice_train(forget: bool) -> int:
    """Train the wake word on the user's voice: three "Hey Arcee"s, two "Arcee stop"s
    and a sentence."""
    from arcee import voice_local, wake_match
    from arcee.voice import VoiceError

    if forget:
        wake_match.forget()
        await wake_match.hold_wake(False)  # arceed takes it up
        print("Forgotten: the wake word is heard by the general model only.")
        return 0
    if missing := voice_local.missing():
        print(f"Downloading {', '.join(m.name for m in missing)}...")
        await voice_local.download_missing()
    vad = voice_local.make_vad()
    print("Training “Hey Arcee” on your voice. Speak as you normally would, from where")
    print("you usually sit. (Arcee stops listening for it until this is done.)\n")
    await wake_match.hold_wake(True)
    try:
        samples: list[list[float]] = []
        for n in range(1, wake_match.SAMPLES + 1):
            print(f"  {n}/{wake_match.SAMPLES}  say “Hey Arcee”... ", end="", flush=True)
            samples.append(await voice_local.record_phrase(vad=vad))
            print(f"got it ({len(samples[-1]) / 16000:.1f} s)")
        stops: list[list[float]] = []
        for n in range(1, wake_match.STOP_SAMPLES + 1):
            print(f"  {n}/{wake_match.STOP_SAMPLES}  say “Arcee stop”... ", end="", flush=True)
            stops.append(await voice_local.record_phrase(vad=vad))
            print(f"got it ({len(stops[-1]) / 16000:.1f} s)")
        print("\n  Now an ordinary sentence, for example “Hey, what's the weather like")
        print("  today?”... ", end="", flush=True)
        other = await voice_local.record_phrase(vad=vad)
        print("got it\n")
        training = voice_local.train(samples, other, stops)
    except VoiceError as exc:
        print(f"\nNot trained: {exc}. Run it again to retry.")
        await wake_match.hold_wake(False)
        return 1
    wake_match.save(training.trained)
    await wake_match.hold_wake(False)  # arceed hears it from now on
    print(
        f"Trained. Your samples were {training.spread:.3f} apart; the sentence came to "
        f"{training.other:.3f}; it wakes at {training.trained.threshold:.3f} or closer."
    )
    print(
        f"“Arcee stop”: {training.stop_spread:.3f} apart; it stops at "
        f"{training.trained.stop_threshold:.3f} or closer."
    )
    print("Kept: the sound of the samples (not the audio), in", wake_match.trained_path())
    return 0


async def memory_learn(core: str, host: str, limit: int, facts: bool) -> int:
    """Digest past tasks that have no summary: what memory would have learned then."""
    from arcee.context.digest import digest_task
    from arcee.models.presets import digest_model_for

    store = Store(data_dir())
    model = digest_model_for(core, host)
    learned = failed = 0
    try:
        tasks = await store.unsummarised(limit)
        print(f"{len(tasks)} past tasks to learn from, on {model.profile.model}")
        for n, task_id in enumerate(tasks, 1):
            try:
                digest = await digest_task(model, store, task_id, facts=facts)
            except Exception as exc:  # one task's failure is not the run's
                failed += 1
                print(f"  {n}/{len(tasks)} {task_id}: failed ({type(exc).__name__}: {exc})")
                continue
            count = len(digest.facts) if digest else 0
            learned += count
            outcome = "skipped" if digest is None else f"{count} facts"
            print(f"  {n}/{len(tasks)} {task_id}: {outcome}")
    finally:
        await model.aclose()
        store.close()
    print(f"done: {learned} facts learned, {failed} tasks failed")
    return 0 if failed == 0 else 1


async def memory_days(core: str, host: str) -> int:
    """Write the day and week summaries arceed has not written yet."""
    from datetime import date

    from arcee.context.periods import catch_up
    from arcee.models.presets import digest_model_for

    store = Store(data_dir())
    model = digest_model_for(core, host)
    try:
        print(f"summarising the last days and weeks on {model.profile.model}")
        written = await catch_up(model, store, date.today())
    finally:
        await model.aclose()
        store.close()
    print(f"done: {written} day and week summaries written")
    return 0


def _row(result: CaseResult) -> dict[str, object]:
    d = result.decision
    return {
        "id": result.case.id,
        "scenario": result.case.scenario,
        "passed": result.passed,
        "problems": list(result.problems),
        "kind": d.kind,
        "tool": d.tool,
        "args": dict(d.args),
        "text": d.text,
        "error": d.error,
        "seconds": round(result.seconds, 2),
        "guarded": result.guarded,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arcee-cli", description="Run Arcee tasks and evals from the terminal."
    )
    load_env()
    host = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="carry out a request")
    run_cmd.add_argument("request", nargs="+", help="what to do, in plain words")
    run_cmd.add_argument("--max-steps", type=int, default=25)
    run_cmd.add_argument("--max-seconds", type=float, default=600.0)
    run_cmd.add_argument("--host", default=host)
    run_cmd.add_argument("-v", "--verbose", action="store_true", help="show model reasoning")
    run_cmd.add_argument("--examples", action="store_true", help=EXAMPLES_HELP)
    run_cmd.add_argument("--core", default=None, help=CORE_HELP + " (default: the app's choice)")
    run_cmd.add_argument(
        "--plan",
        choices=("auto", "always", "never"),
        default="auto",
        help="plan the task first: auto plans multi-step requests only",
    )
    run_cmd.add_argument(
        "--no-memory",
        action="store_true",
        help="neither recall memory nor remember this task (tests, throwaway runs)",
    )

    voice_cmd = sub.add_parser(
        "voice-test", help="record a few seconds and show what the wake word listener hears"
    )
    voice_cmd.add_argument("--seconds", type=float, default=10.0)
    voice_cmd.add_argument("--file", help="analyse this recording (16 kHz mono WAV) instead")

    watch_cmd = sub.add_parser(
        "voice-watch", help="show live what the wake-word listener hears, until Ctrl+C"
    )
    watch_cmd.add_argument(
        "--no-words", action="store_true", help="do not send what is heard to Groq's Whisper"
    )

    train_cmd = sub.add_parser("voice-train", help='train "Hey Arcee" to your voice')
    train_cmd.add_argument("--forget", action="store_true", help="forget the trained wake word")

    days_cmd = sub.add_parser(
        "memory-days", help="write the summaries of past days and weeks (memory)"
    )
    days_cmd.add_argument(
        "--core", default="local", help="the model that writes them (default: local)"
    )
    days_cmd.add_argument("--host", default=host)

    learn_cmd = sub.add_parser(
        "memory-learn", help="learn from past tasks that have no summary yet (memory)"
    )
    learn_cmd.add_argument(
        "--core", default="local", help="the model that digests them (default: local)"
    )
    learn_cmd.add_argument("--host", default=host)
    learn_cmd.add_argument("-n", type=int, default=1000, help="at most this many tasks")
    learn_cmd.add_argument(
        "--facts",
        action="store_true",
        help="also learn facts about the user (always on except with --core local, "
        "whose models mistake results for facts)",
    )

    history_cmd = sub.add_parser("history", help="list recent tasks, or show one")
    history_cmd.add_argument("task_id", nargs="?", help="a task id from the list")
    history_cmd.add_argument("-n", type=int, default=15, help="how many tasks to list")

    sub.add_parser("shell-extension", help="install the GNOME Shell extension (window focus)")
    sub.add_parser("shell-check", help="check that the GNOME Shell extension is running")
    sub.add_parser(
        "daemon-install", help="register arceed with systemd and D-Bus so the panel can start it"
    )
    screen = sub.add_parser(
        "screen-check", help="show the control glow, read the screen locally, test the pause"
    )
    screen.add_argument("--seconds", type=int, default=15, help="how long to hold control")
    sub.add_parser("limits", help="show the cloud models' remaining rate limits")
    sub.add_parser("browser-setup", help="register the Chrome extension's native host")
    sub.add_parser("browser-check", help="check that the Chrome extension is connected")
    sub.add_parser(
        "browser-reload", help="make Chrome load the Arcee extension's files again (0.3.6+)"
    )

    tasks_cmd = sub.add_parser("eval-tasks", help="run whole tasks and check their outcomes")
    tasks_cmd.add_argument("--tasks", type=Path, default=Path("tests/eval/tasks.toml"))
    tasks_cmd.add_argument("--only", default="", help="task id substring")
    tasks_cmd.add_argument("--plan", choices=("auto", "always", "never"), default="auto")
    tasks_cmd.add_argument("--examples", action="store_true", help=EXAMPLES_HELP)
    tasks_cmd.add_argument("--core", default=DEFAULT_CORE, help=CORE_HELP)
    tasks_cmd.add_argument("--host", default=host)
    tasks_cmd.add_argument("--report", type=Path, default=None, help="write a Markdown report")

    eval_cmd = sub.add_parser("eval", help="score the Core on single-decision cases")
    eval_cmd.add_argument("--cases", type=Path, default=Path("tests/eval/cases.toml"))
    eval_cmd.add_argument("--tier", type=int, choices=(1, 2, 3), default=1)
    eval_cmd.add_argument("--repeat", type=int, default=1)
    eval_cmd.add_argument("--only", default="", help="case id or scenario substring")
    eval_cmd.add_argument("--report", type=Path, help="write per-case results as JSON")
    eval_cmd.add_argument(
        "--raw", action="store_true", help="judge the raw model, without the reply guards"
    )
    eval_cmd.add_argument("--examples", action="store_true", help=EXAMPLES_HELP)
    eval_cmd.add_argument("--core", default=DEFAULT_CORE, help=CORE_HELP)
    eval_cmd.add_argument("--host", default=host)

    args = parser.parse_args(argv)
    try:
        if args.command == "history":
            return asyncio.run(history(args.task_id, args.n))
        if args.command == "shell-extension":
            return shell_extension()
        if args.command == "shell-check":
            return asyncio.run(shell_check())
        if args.command == "daemon-install":
            return daemon_install()
        if args.command == "screen-check":
            return asyncio.run(screen_check(args.seconds))
        if args.command == "limits":
            return show_limits()
        if args.command == "browser-setup":
            return browser_setup()
        if args.command == "browser-check":
            return asyncio.run(browser_check())
        if args.command == "browser-reload":
            return asyncio.run(browser_reload())
        if args.command == "voice-watch":
            return asyncio.run(voice_watch(not args.no_words))
        if args.command == "voice-train":
            return asyncio.run(voice_train(args.forget))
        if args.command == "voice-test":
            return asyncio.run(voice_test(args.seconds, args.file))
        if args.command == "memory-days":
            return asyncio.run(memory_days(args.core, _host(args.host)))
        if args.command == "memory-learn":
            facts = args.facts or args.core != "local"
            return asyncio.run(memory_learn(args.core, _host(args.host), args.n, facts))
        if args.command == "eval-tasks":
            return asyncio.run(
                evaluate_tasks(
                    args.tasks,
                    host=_host(args.host),
                    only=args.only,
                    plan=args.plan,
                    examples=args.examples,
                    core=args.core,
                    report=args.report,
                )
            )
        if args.command == "eval":
            return asyncio.run(
                evaluate(
                    args.cases,
                    host=_host(args.host),
                    tier=args.tier,
                    repeat=args.repeat,
                    only=args.only,
                    report=args.report,
                    raw=args.raw,
                    examples=args.examples,
                    core=args.core,
                )
            )
        limits = Limits(max_steps=args.max_steps, max_seconds=args.max_seconds)
        request = " ".join(args.request)
        return asyncio.run(
            run(
                request,
                host=_host(args.host),
                limits=limits,
                verbose=args.verbose,
                plan=args.plan,
                examples=args.examples,
                core=args.core,
                memory=not args.no_memory,
            )
        )
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
    except (ModelUnavailableError, ValueError) as exc:
        print(f"arcee-cli: {exc}", file=sys.stderr)
        return 2


# Off by default: on LFM3-Omni they lowered the eval from 91% to 89% and whole
# tasks from 15/15 to 13/15, and made runs slower (decision 0006).
CORE_HELP = (
    "groq (default: gpt-oss-20b, gpt-oss-120b for hard requests), local (the Ollama ladder), "
    "or <provider>:<model id>"
)
EXAMPLES_HELP = "add the closest worked examples to prompts (off by default; decision 0006)"


def _host(value: str) -> str:
    # OLLAMA_HOST is often written without a scheme, e.g. 127.0.0.1:11434.
    return value if "://" in value else f"http://{value}"


if __name__ == "__main__":
    sys.exit(main())
