"""`arcee-cli`: run one task from the terminal (roadmap 1.17).

Confirmations and questions are answered on stdin. Every step is printed: the
model's call, the result, guards and escalations, so a run doubles as a trace.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TextIO

from arcee.browser.client import BrowserClient, BrowserError
from arcee.browser.host import EXTENSION_ID
from arcee.browser.setup import EXTENSION_DIR, install
from arcee.context.examples import ExampleLibrary
from arcee.context.store import Store, data_dir
from arcee.core.agent import (
    Agent,
    Escalated,
    Event,
    Guarded,
    Limits,
    ModelTurn,
    Planned,
    StepStarted,
    ToolRan,
)
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.planner import Planner, needs_plan
from arcee.core.policy import Policy
from arcee.core.protocol import ConfirmRequest, PlannerOutput
from arcee.eval.cases import load as load_cases
from arcee.eval.runner import CaseResult, run_cases, summary
from arcee.eval.tasks import TaskResult, load_tasks, run_task, task_summary
from arcee.models.base import ModelAdapter
from arcee.models.errors import ModelUnavailableError
from arcee.models.ollama import DEFAULT_HOST
from arcee.models.presets import core_ladder, planner_for, share_budget
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

    def print(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    def on_event(self, event: Event) -> None:
        s = self._style
        match event:
            case ModelTurn(call=call, thinking=thinking, model=model, seconds=seconds):
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
        try:
            return await asyncio.to_thread(input, prompt)
        except EOFError:
            return ""


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
    core: str = "local",
) -> int:
    terminal = Terminal(verbose=verbose)
    ladder = core_ladder(core, host)
    planner_model = planner_for(core, host)
    share_budget(ladder, planner_model)
    store = Store(data_dir())
    try:
        usable = await _usable(ladder, terminal)
        if not usable:
            terminal.print(
                f"Ollama is not running at {host}, or the model is missing. "
                f"Start it, then: ollama pull {ladder[0].profile.model}"
            )
            return 2
        registry = builtin_registry()
        planner = None if plan == "never" else Planner(planner_model, registry, home=Path.home())
        plans: list[PlannerOutput] = []

        def on_event(event: Event) -> None:
            if isinstance(event, Planned):
                plans.append(event.plan)
            terminal.on_event(event)

        agent = Agent(
            usable,
            registry,
            Executor(Gate(Policy(), terminal), record=store.record_action),
            terminal,
            home=Path.home(),
            limits=limits,
            planner=planner,
            notes=store,
            examples=ExampleLibrary.load() if examples else None,
            plan_if=needs_plan if plan == "auto" else lambda request: True,
            on_event=on_event,
        )
        task_id = await store.start_task(request)
        started = time.monotonic()
        try:
            outcome = await agent.run(request, task_id=task_id)
        except ModelUnavailableError as exc:
            await _finish(store, task_id, "failed", f"model unavailable: {exc}", plans)
            terminal.print(f"model unavailable: {exc}")
            return 2
        except asyncio.CancelledError:
            await asyncio.shield(_finish(store, task_id, "cancelled", "", plans))
            raise
        await store.finish_task(
            task_id,
            status=outcome.status,
            reply=outcome.reply,
            steps=outcome.steps,
            model_calls=outcome.model_calls,
            plan=plans[-1] if plans else None,
        )
        style = Style(sys.stdout)
        terminal.print()
        terminal.print(style.bold("Arcee: ") + outcome.reply)
        terminal.print(
            style.dim(
                f"({outcome.status}: {outcome.steps} steps, {outcome.model_calls} model calls, "
                f"{time.monotonic() - started:.1f}s, {task_id})"
            )
        )
        return 0 if outcome.status == "done" else 1
    finally:
        await asyncio.gather(*(model.aclose() for model in [*ladder, planner_model]))
        store.close()


async def _finish(
    store: Store, task_id: str, status: str, reply: str, plans: list[PlannerOutput]
) -> None:
    await store.finish_task(
        task_id,
        status=status,
        reply=reply,
        steps=0,
        model_calls=0,
        plan=plans[-1] if plans else None,
    )


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
    core: str = "local",
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
                task, usable, planner_model=planner_model, plan=plan, examples=library
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
    _load_dotenv(Path(".env"))
    host = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="carry out a request")
    run_cmd.add_argument("request", nargs="+", help="what to do, in plain words")
    run_cmd.add_argument("--max-steps", type=int, default=25)
    run_cmd.add_argument("--max-seconds", type=float, default=600.0)
    run_cmd.add_argument("--host", default=host)
    run_cmd.add_argument("-v", "--verbose", action="store_true", help="show model reasoning")
    run_cmd.add_argument("--examples", action="store_true", help=EXAMPLES_HELP)
    run_cmd.add_argument("--core", default="local", help=CORE_HELP)
    run_cmd.add_argument(
        "--plan",
        choices=("auto", "always", "never"),
        default="auto",
        help="plan the task first: auto plans multi-step requests only",
    )

    history_cmd = sub.add_parser("history", help="list recent tasks, or show one")
    history_cmd.add_argument("task_id", nargs="?", help="a task id from the list")
    history_cmd.add_argument("-n", type=int, default=15, help="how many tasks to list")

    sub.add_parser("browser-setup", help="register the Chrome extension's native host")
    sub.add_parser("browser-check", help="check that the Chrome extension is connected")

    tasks_cmd = sub.add_parser("eval-tasks", help="run whole tasks and check their outcomes")
    tasks_cmd.add_argument("--tasks", type=Path, default=Path("tests/eval/tasks.toml"))
    tasks_cmd.add_argument("--only", default="", help="task id substring")
    tasks_cmd.add_argument("--plan", choices=("auto", "always", "never"), default="auto")
    tasks_cmd.add_argument("--examples", action="store_true", help=EXAMPLES_HELP)
    tasks_cmd.add_argument("--core", default="local", help=CORE_HELP)
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
    eval_cmd.add_argument("--core", default="local", help=CORE_HELP)
    eval_cmd.add_argument("--host", default=host)

    args = parser.parse_args(argv)
    try:
        if args.command == "history":
            return asyncio.run(history(args.task_id, args.n))
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
CORE_HELP = "the Core model: local (the Ollama ladder), groq:<model id> or openrouter:<model id>"
EXAMPLES_HELP = "add the closest worked examples to prompts (off by default; decision 0006)"


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from a local .env (git-ignored) without overriding the environment."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def _host(value: str) -> str:
    # OLLAMA_HOST is often written without a scheme, e.g. 127.0.0.1:11434.
    return value if "://" in value else f"http://{value}"


if __name__ == "__main__":
    sys.exit(main())
