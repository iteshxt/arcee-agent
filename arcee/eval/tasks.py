"""Whole-task evals (roadmap 1.18b, 6): run the full agent and check the end state.

Each task gets a throwaway home folder with its own files and its own History,
then runs exactly as `arcee-cli run` would: every built-in tool, offered by the
same per-request groups; Planner when the request calls for it; escalation
ladder, guards, gate. Confirmations and questions are answered from the task.

Tools that work on files, the shell, documents, memory and reminders are real,
inside that home. Every tool that would reach the network, the desktop, Chrome
or the user's accounts is a stub: the same name, description, card and tier, so
the model chooses among exactly what it sees in real use, but its run returns
what the task gives it (`stub`), or a plain "done". So a run is offline,
repeatable and changes nothing on the computer. Trashing moves into a stand-in
trash folder, because gio refuses to trash on /tmp.

What is checked is the outcome: which files exist, are gone or contain what,
what the reply says, and which tools were called with what.
"""

import copy
import json
import re
import shutil
import tempfile
import time
import tomllib
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcee.context.examples import ExampleLibrary
from arcee.context.store import Store
from arcee.core.agent import Agent, Event, Guarded, Limits
from arcee.core.executor import ActionRecord, Executor
from arcee.core.gate import Gate
from arcee.core.planner import Planner, needs_plan
from arcee.core.policy import Policy
from arcee.core.protocol import ConfirmRequest
from arcee.models.base import ModelAdapter
from arcee.tools.apps import AppOpen
from arcee.tools.base import ToolContext, ToolResult
from arcee.tools.builtin import builtin_registry
from arcee.tools.files import FsOpen, FsTrash
from arcee.tools.registry import AnyTool, Registry

TRASH_DIR = ".eval-trash"
# Tighter than a real task's, so a looping run fails in minutes, not ten.
TASK_LIMITS = Limits(max_steps=15, max_seconds=300)
# Real in an eval: they work inside the task's home and History. Every other tool is
# a stub (see the module's note).
REAL_TOOLS = frozenset(
    {
        "fs.list",
        "fs.usage",
        "fs.read",
        "fs.search",
        "fs.trash",
        "fs.write",
        "fs.move",
        "fs.copy",
        "app.list",
        "system.status",
        "network.status",
        "doc.create",
        "shell.run",
        "shell.script",
        "task.undo",
        "memory.recall",
        "memory.forget",
        "memory.history",
        "reminder.add",
        "reminder.list",
        "reminder.cancel",
        "routine.add",
        "control.set_timer",
        "control.wait_for",
        "job.check",
        "job.stop",
    }
)


@dataclass(frozen=True, slots=True)
class Checks:
    reply_has: tuple[str, ...] = ()
    reply_lacks: tuple[str, ...] = ()
    exists: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    contains: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, Any])
    asked: bool | None = None
    used: tuple[str, ...] = ()  # every one of these tools was called
    used_any: tuple[str, ...] = ()  # at least one of these
    not_used: tuple[str, ...] = ()
    # Per tool, words its arguments must contain (any call of it): {"settings.volume":
    # {"percent": "30"}}.
    args: Mapping[str, Mapping[str, str]] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    request: str
    files: Mapping[str, str | int]
    checks: Checks
    answers: tuple[str, ...] = ()
    approve: bool = True
    # What stubbed tools return: {"travel.search": {"summary": "...", "data": {...}}}.
    stubs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict[str, Any])
    category: str = ""


@dataclass(frozen=True, slots=True)
class Call:
    tool: str
    args: Mapping[str, Any]
    status: str


@dataclass(frozen=True, slots=True)
class TaskResult:
    task: Task
    problems: tuple[str, ...]
    reply: str
    steps: int
    model_calls: int
    seconds: float
    planned: bool
    guards: tuple[str, ...]
    calls: tuple[Call, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.problems


def load_tasks(path: Path) -> list[Task]:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    tasks: list[Task] = []
    for entry in raw.get("task", []):
        check = entry.get("check", {})
        tasks.append(
            Task(
                id=entry["id"],
                request=entry["request"],
                files=entry.get("files", {}),
                answers=tuple(entry.get("answers", [])),
                approve=entry.get("approve", True),
                stubs=entry.get("stub", {}),
                category=entry.get("category", "files"),
                checks=Checks(
                    reply_has=tuple(check.get("reply_has", [])),
                    reply_lacks=tuple(check.get("reply_lacks", [])),
                    exists=tuple(check.get("exists", [])),
                    absent=tuple(check.get("absent", [])),
                    contains={k: tuple(v) for k, v in check.get("contains", {}).items()},
                    asked=check.get("asked"),
                    used=tuple(check.get("used", [])),
                    used_any=tuple(check.get("used_any", [])),
                    not_used=tuple(check.get("not_used", [])),
                    args={k: dict(v) for k, v in check.get("args", {}).items()},
                ),
            )
        )
    return tasks


class _NoWindows:
    """No open windows: an eval never brings the user's own windows forward."""

    def windows(self) -> list[dict[str, Any]]:
        return []

    def focus_window(self, window_id: int) -> bool:
        return False


def stub(tool: AnyTool, canned: Mapping[str, Any] | None) -> AnyTool:
    """`tool` as the model sees it (name, card, arguments, tier), running nothing:
    its result is `canned` (summary, data), or a plain "done" with the arguments."""
    fake = copy.copy(tool)

    async def run(self: Any, args: Any, ctx: ToolContext) -> ToolResult:
        if canned is not None:
            if canned.get("error"):
                return ToolResult.fail(str(canned["error"]))
            data = canned.get("data")
            return ToolResult.ok(str(canned.get("summary", "Done")), data=dict(data or {}))
        given = args.model_dump(mode="json")
        if tool.read_only:
            return ToolResult.ok("Nothing found", data={"results": []})
        return ToolResult.ok(f"Done: {tool.name}", data=given)

    fake.run = types.MethodType(run, fake)  # type: ignore[method-assign]
    return fake


def task_registry(
    home: Path,
    *,
    store: Store | None = None,
    stubs: Mapping[str, Mapping[str, Any]] | None = None,
    shell_limits: bool = True,
) -> Registry:
    """Every built-in tool: the REAL_TOOLS working in `home` (trash into a stand-in
    folder) and on `store`, every other one stubbed with `stubs`."""
    trash = home / TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    stubs = stubs or {}
    tools: list[AnyTool] = []
    for tool in builtin_registry(shell_limits=shell_limits, memory=store, reminders=store):
        if tool.name == "fs.trash":
            tools.append(FsTrash(command=("mv", "--backup=numbered", "-t", str(trash), "--")))
        elif tool.name == "fs.open_with_default" and tool.name not in stubs:
            # Real, opening nothing: a file that is not there still fails as it should.
            tools.append(FsOpen(command=("true",)))
        elif tool.name == "app.open" and tool.name not in stubs:
            tools.append(AppOpen(command=("true",), windows=_NoWindows(), window_wait_s=0))
        elif (tool.name in REAL_TOOLS and tool.name not in stubs) or (
            tool.name.startswith("control.")
            and tool.name not in ("control.set_timer", "control.wait_for")
        ):
            tools.append(tool)
        else:
            tools.append(stub(tool, stubs.get(tool.name)))
    return Registry(tools)


class _ScriptedUser:
    def __init__(self, task: Task) -> None:
        self._answers = list(task.answers)
        self._approve = task.approve
        self.questions: list[str] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        return self._approve

    async def ask(self, question: str, options: Sequence[str] = ()) -> str:
        self.questions.append(question)
        return self._answers.pop(0) if self._answers else "I don't know, do what you can."


def make_home(task: Task, root: Path) -> Path:
    home = root / "home"
    home.mkdir()
    for rel, body in task.files.items():
        path = home / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, int):
            path.write_bytes(b"\0" * body)  # a file of that many bytes
        else:
            path.write_text(body)
    return home


def _matches(value: Any, wanted: str) -> bool:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return wanted.lower() in text.lower()


def check_calls(task: Task, calls: Sequence[Call]) -> list[str]:
    checks = task.checks
    called = {call.tool for call in calls}
    problems = [f"never called {t}" for t in checks.used if t not in called]
    if checks.used_any and not called & set(checks.used_any):
        problems.append(f"called none of {', '.join(checks.used_any)}")
    problems += [f"called {t}" for t in checks.not_used if t in called]
    for tool, fields in checks.args.items():
        runs = [c for c in calls if c.tool == tool]
        if not runs:
            continue  # "never called" says it, when it is required
        for name, wanted in fields.items():
            if not any(_matches(c.args.get(name, ""), str(wanted)) for c in runs):
                got = [c.args.get(name) for c in runs]
                problems.append(f"{tool} {name}={got!r}, wanted {wanted!r}")
    return problems


def check_outcome(task: Task, home: Path, reply: str, asked: bool) -> list[str]:
    problems: list[str] = []
    checks = task.checks
    # Numbers are often written with separators: 1,099,511,627,776.
    text = re.sub(r"(?<=\d)[,_ ](?=\d{3}\b)", "", reply).lower()
    problems += [f"reply lacks {w!r}" for w in checks.reply_has if w.lower() not in text]
    problems += [f"reply has {u!r}" for u in checks.reply_lacks if u.lower() in text]
    problems += [f"{p} is missing" for p in checks.exists if not (home / p).exists()]
    problems += [f"{p} still exists" for p in checks.absent if (home / p).exists()]
    for rel, wanted in checks.contains.items():
        path = home / rel
        body = path.read_text(errors="replace").lower() if path.is_file() else ""
        problems += [f"{rel} lacks {w!r}" for w in wanted if w.lower() not in body]
    if checks.asked is not None and asked != checks.asked:
        problems.append("asked the user" if asked else "never asked the user")
    return problems


async def run_task(
    task: Task,
    ladder: Sequence[ModelAdapter],
    *,
    planner_model: ModelAdapter | None,
    plan: str = "auto",
    examples: ExampleLibrary | None = None,
    start_tier: Callable[[str], int] = lambda request: 0,
    limits: Limits = TASK_LIMITS,
    on_event: Callable[[Event], None] = lambda event: None,
) -> TaskResult:
    root = Path(tempfile.mkdtemp(prefix="arcee-task-"))
    store = Store(root / "history")
    try:
        home = make_home(task, root)
        registry = task_registry(home, store=store, stubs=task.stubs)
        user = _ScriptedUser(task)
        calls: list[Call] = []

        async def remember(record: ActionRecord) -> None:
            calls.append(Call(record.result.tool, dict(record.args), record.result.status))

        events: list[Event] = []
        planner = None
        if planner_model is not None and plan != "never":
            planner = Planner(planner_model, registry, home=home)

        def record(event: Event) -> None:
            events.append(event)
            on_event(event)

        agent = Agent(
            ladder,
            registry,
            Executor(Gate(Policy(home=home), user), record=remember),
            user,
            home=home,
            limits=limits,
            planner=planner,
            examples=examples,
            plan_if=needs_plan if plan == "auto" else lambda request: True,
            start_tier=start_tier,
            on_event=record,
            sharing="always",  # the stubs hold no one's mail; no question about sending it
        )
        started = time.monotonic()
        outcome = await agent.run(task.request, task_id=f"eval_{task.id}")
        seconds = time.monotonic() - started
        problems = check_outcome(task, home, outcome.reply, bool(user.questions))
        problems += check_calls(task, calls)
        if outcome.status != "done" and not task.checks.asked:
            problems.insert(0, f"task {outcome.status}: {outcome.reply[:80]}")
        return TaskResult(
            task,
            tuple(problems),
            outcome.reply,
            outcome.steps,
            outcome.model_calls,
            seconds,
            planned=planner is not None and (plan == "always" or needs_plan(task.request)),
            guards=tuple(e.what for e in events if isinstance(e, Guarded)),
            calls=tuple(calls),
        )
    finally:
        store.close()
        shutil.rmtree(root, ignore_errors=True)


def task_summary(results: Sequence[TaskResult]) -> str:
    if not results:
        return "no tasks run"
    passed = sum(r.passed for r in results)
    steps = sorted(r.steps for r in results)
    seconds = sorted(r.seconds for r in results)
    return "\n".join(
        [
            f"tasks passed: {passed}/{len(results)} ({passed / len(results):.0%})",
            f"steps: median {steps[len(steps) // 2]}, max {steps[-1]}",
            f"time: median {seconds[len(seconds) // 2]:.1f}s, max {seconds[-1]:.1f}s",
            f"planned: {sum(r.planned for r in results)}, "
            f"guards fired: {sum(len(r.guards) for r in results)}",
        ]
    )


def report_markdown(results: Sequence[TaskResult], *, title: str, notes: Sequence[str] = ()) -> str:
    """The run as a Markdown report: totals, by category, then every task."""
    lines = [f"# {title}", ""]
    lines += [f"- {note}" for note in notes]
    lines += ["", "```", task_summary(results), "```", "", "## By category", ""]
    lines += ["| Category | Passed |", "| --- | --- |"]
    categories: dict[str, list[TaskResult]] = {}
    for result in results:
        categories.setdefault(result.task.category or "files", []).append(result)
    for name, group in sorted(categories.items()):
        lines.append(f"| {name} | {sum(r.passed for r in group)}/{len(group)} |")
    lines += ["", "## Tasks", "", "| Task | | Steps | Calls | Time | Tools | Problems |"]
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        tools = ", ".join(
            dict.fromkeys(c.tool for c in r.calls if not c.tool.startswith("control."))
        )
        problems = "; ".join(r.problems).replace("|", "/")
        mark = "pass" if r.passed else "**FAIL**"
        lines.append(
            f"| {r.task.id} | {mark} | {r.steps} | {r.model_calls} | {r.seconds:.0f} s | "
            f"{tools or '-'} | {problems[:200]} |"
        )
    return "\n".join(lines) + "\n"
