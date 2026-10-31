"""`arcee-cli`: run one task from the terminal (roadmap 1.17).

Confirmations and questions are answered on stdin. Every step is printed: the
model's call, the result, guards and escalations, so a run doubles as a trace.
"""

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Literal, TextIO

from arcee.browser.client import BrowserClient, BrowserError
from arcee.browser.host import EXTENSION_ID
from arcee.browser.setup import EXTENSION_DIR, install
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
from arcee.core.routing import start_tier
from arcee.eval.cases import load as load_cases
from arcee.eval.runner import CaseResult, run_cases, summary
from arcee.eval.tasks import TaskResult, load_tasks, run_task, task_summary
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
    core: str = DEFAULT_CORE,
) -> int:
    terminal = Terminal(verbose=verbose)
    presence = terminal.presence = ShellPresence()
    runner = TaskRunner(
        core=core, host=host, limits=limits, plan=plan, examples=examples, presence=presence
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
    tasks_path: Path, *, host: str, only: str, plan: str, examples: bool, core: str = "local"
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
                start_tier=start_tier if core == "groq" else lambda request: 0,
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
    return 0


def browser_setup() -> int:
    launcher, manifest = install(data_dir())
    print(f"Registered the Arcee browser host with Chrome:\n  {manifest}\n  {launcher}\n")
    print("Now load the extension once:")
    print("  1. Open chrome://extensions and turn on Developer mode (top right).")
    print(f"  2. Click 'Load unpacked' and choose {EXTENSION_DIR}")
    print(f"  3. Check that its ID is {EXTENSION_ID}, then run: arcee-cli browser-check")
    return 0


STOP_SHORTCUT: Final = "Super+Shift+Escape"


def shell_extension() -> int:
    """Link the Arcee GNOME Shell extension into the user's extensions folder."""
    source = EXTENSION_DIR.parent / "extension" / SHELL_EXTENSION
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    target = Path(data) / "gnome-shell" / "extensions" / SHELL_EXTENSION
    target.parent.mkdir(parents=True, exist_ok=True)
    # The stop shortcut lives in the extension's settings schema, compiled in place.
    subprocess.run(["glib-compile-schemas", "--strict", str(source / "schemas")], check=True)
    if target.is_symlink() or target.exists():
        print(f"already installed: {target}")
    else:
        target.symlink_to(source, target_is_directory=True)
        print(f"linked {target} -> {source}")
    print("\nOn Wayland GNOME loads new extensions at login. Log out and back in, then run:")
    print(f"  gnome-extensions enable {SHELL_EXTENSION}")
    print("  arcee-cli shell-check")
    return 0


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
    """Write the systemd user unit and the D-Bus activation file for arceed.

    The panel calls org.arcee.Agent; D-Bus starts arceed through systemd on the first
    call, and its log goes to the journal (journalctl --user -u arceed).
    """
    arceed = Path(sys.executable).parent / "arceed"
    if not arceed.exists():
        print(f"{arceed} is missing; run `uv sync` first")
        return 1
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    unit = config / "systemd" / "user" / "arceed.service"
    activation = data / "dbus-1" / "services" / "org.arcee.Agent.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    activation.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\n"
        "Description=Arcee agent\n"
        "PartOf=graphical-session.target\n\n"
        "[Service]\n"
        "Type=dbus\n"
        "BusName=org.arcee.Agent\n"
        f"ExecStart={arceed}\n"
        f"WorkingDirectory={Path(__file__).resolve().parents[1]}\n"
        "TimeoutStopSec=2\n"
        "Restart=on-failure\n"
    )
    activation.write_text(
        f"[D-BUS Service]\nName=org.arcee.Agent\nExec={arceed}\nSystemdService=arceed.service\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"wrote {unit}\nwrote {activation}")
    print("arceed starts on the panel's first request; its log: journalctl --user -u arceed")
    return 0


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
    run_cmd.add_argument("--core", default=DEFAULT_CORE, help=CORE_HELP)
    run_cmd.add_argument(
        "--plan",
        choices=("auto", "always", "never"),
        default="auto",
        help="plan the task first: auto plans multi-step requests only",
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

    tasks_cmd = sub.add_parser("eval-tasks", help="run whole tasks and check their outcomes")
    tasks_cmd.add_argument("--tasks", type=Path, default=Path("tests/eval/tasks.toml"))
    tasks_cmd.add_argument("--only", default="", help="task id substring")
    tasks_cmd.add_argument("--plan", choices=("auto", "always", "never"), default="auto")
    tasks_cmd.add_argument("--examples", action="store_true", help=EXAMPLES_HELP)
    tasks_cmd.add_argument("--core", default=DEFAULT_CORE, help=CORE_HELP)
    tasks_cmd.add_argument("--host", default=host)

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
        if args.command == "eval-tasks":
            return asyncio.run(
                evaluate_tasks(
                    args.tasks,
                    host=_host(args.host),
                    only=args.only,
                    plan=args.plan,
                    examples=args.examples,
                    core=args.core,
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
SHELL_EXTENSION = "arcee@arcee.dev"
EXAMPLES_HELP = "add the closest worked examples to prompts (off by default; decision 0006)"


def _host(value: str) -> str:
    # OLLAMA_HOST is often written without a scheme, e.g. 127.0.0.1:11434.
    return value if "://" in value else f"http://{value}"


if __name__ == "__main__":
    sys.exit(main())
