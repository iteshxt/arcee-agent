"""Whole-task evals (roadmap 1.18b): run the full agent and check the end state.

Each task gets a throwaway home folder with its own files, then runs exactly as
`arcee-cli run` would: Planner when the request calls for it, escalation
ladder, guards, gate. Confirmations and questions are answered from the task.
What is checked is the outcome, not the route: which files exist, are gone or
contain what, and what the reply says. Opening apps and files is stubbed so
nothing appears on screen, and trashing moves into a stand-in trash folder,
because gio refuses to trash on /tmp.
"""

import re
import shutil
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcee.context.examples import ExampleLibrary
from arcee.core.agent import Agent, Event, Guarded, Limits
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.planner import Planner, needs_plan
from arcee.core.policy import Policy
from arcee.core.protocol import ConfirmRequest
from arcee.models.base import ModelAdapter
from arcee.tools.apps import AppList, AppOpen
from arcee.tools.control import CONTROL_TOOLS, UTILITY_CONTROL_TOOLS
from arcee.tools.files import (
    FsCopy,
    FsList,
    FsMove,
    FsOpen,
    FsRead,
    FsSearch,
    FsTrash,
    FsWrite,
)
from arcee.tools.registry import Registry
from arcee.tools.shell import ShellRun, ShellScript
from arcee.tools.system import NetworkStatus, SystemStatus

TRASH_DIR = ".eval-trash"
# Tighter than a real task's, so a looping run fails in minutes, not ten.
TASK_LIMITS = Limits(max_steps=15, max_seconds=300)


@dataclass(frozen=True, slots=True)
class Checks:
    reply_has: tuple[str, ...] = ()
    reply_lacks: tuple[str, ...] = ()
    exists: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    contains: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, Any])
    asked: bool | None = None


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    request: str
    files: Mapping[str, str | int]
    checks: Checks
    answers: tuple[str, ...] = ()
    approve: bool = True


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
                checks=Checks(
                    reply_has=tuple(check.get("reply_has", [])),
                    reply_lacks=tuple(check.get("reply_lacks", [])),
                    exists=tuple(check.get("exists", [])),
                    absent=tuple(check.get("absent", [])),
                    contains={k: tuple(v) for k, v in check.get("contains", {}).items()},
                    asked=check.get("asked"),
                ),
            )
        )
    return tasks


def task_registry(home: Path, *, shell_limits: bool = True) -> Registry:
    """The built-in tools, with opening stubbed and trashing into the task's home."""
    trash = home / TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    return Registry(
        [
            FsList(),
            FsRead(),
            FsSearch(),
            FsTrash(command=("mv", "--backup=numbered", "-t", str(trash), "--")),
            FsWrite(),
            FsMove(),
            FsCopy(),
            FsOpen(command=("true",)),
            AppList(),
            AppOpen(command=("true",)),
            SystemStatus(),
            NetworkStatus(),
            ShellRun(limits=shell_limits),
            ShellScript(limits=shell_limits),
            *(tool() for tool in UTILITY_CONTROL_TOOLS),
            *(tool() for tool in CONTROL_TOOLS),
        ]
    )


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
    try:
        home = make_home(task, root)
        registry = task_registry(home)
        user = _ScriptedUser(task)
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
            Executor(Gate(Policy(home=home), user)),
            user,
            home=home,
            limits=limits,
            planner=planner,
            examples=examples,
            plan_if=needs_plan if plan == "auto" else lambda request: True,
            start_tier=start_tier,
            on_event=record,
        )
        started = time.monotonic()
        outcome = await agent.run(task.request, task_id=f"eval_{task.id}")
        seconds = time.monotonic() - started
        problems = check_outcome(task, home, outcome.reply, bool(user.questions))
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
        )
    finally:
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
