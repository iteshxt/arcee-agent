"""Work that goes on after the step that started it (decision 0020).

- `shell.start`: a long command (a build, tests, downloads) run in the background by
  arceed; the task goes on. `control.wait_for(event="job_done")` waits for it, and
  `job.check` reads its output. One that ends after the task is said by a
  notification, or, with `then`, by a follow-up request that reads the result.
- `job.check` and `job.stop`.
- `control.set_timer`: Arcee's own timer. After a while it runs a request (what to
  check), or watches something without the model (a page changes or says a thing, a
  file appears, a read-only command succeeds) and runs the request once it happens.
"""

import os
import time
from datetime import datetime
from typing import Any, ClassVar, Final, Literal
from urllib.parse import urlparse

from pydantic import Field

from arcee.core.policy import Policy, Tier
from arcee.followups import MAX_PENDING, Detail, PageText, Watch, digest, page_text
from arcee.jobs import JOBS, Jobs
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.files import display, resolve
from arcee.tools.reminders import Reminders
from arcee.tools.shell import clean_env

LONGEST_WATCH_H: Final = 72.0

# --- shell.start ------------------------------------------------------------------------------


class StartArgs(ToolArgs):
    cmd: str = Field(min_length=1, description="One Linux command line, e.g. make && make test")
    cwd: str = Field(default="~", description="Folder to run in")
    then: str = Field(
        default="",
        max_length=500,
        description="Leave empty when you will wait for it in this task. Only for a job "
        "that outlives the task: what to do with its result then, as a request ('Tell me "
        "whether the tests passed'); empty gives a notification",
    )


class ShellStart(Tool[StartArgs]):
    name = "shell.start"
    description = (
        "Start a long command in the background (a build, a test suite, downloads with curl "
        "or wget) and go on at once; it returns a job id. Up to 4 hours."
    )
    usage = Usage(
        when="A command takes more than a minute or two, or several should run at once. "
        "Then control.wait_for(event='job_done', target=<job>) to wait, job.check to read "
        "its output.",
        avoid="Quick commands (shell.run returns their output directly); something the "
        "user wants to watch (terminal.run).",
        mistakes="Redirecting its output to a file: Arcee keeps it, and job.check reads it. "
        "Polling job.check over and over: wait_for returns the moment it ends.",
        example='shell.start(cmd="make && make test", cwd="~/code/app", then="Tell me if '
        'the tests passed")',
    )
    args_model = StartArgs
    timeout_s = 15

    def __init__(self, jobs: Jobs | None = None) -> None:
        self._jobs = jobs or JOBS

    def tier_for(self, args: StartArgs, policy: Policy) -> Tier:
        return policy.classify_shell(args.cmd).tier

    def refusal(self, args: StartArgs, policy: Policy) -> str:
        return f"{policy.classify_shell(args.cmd).reason}; this command will never run"

    def sentence(self, args: StartArgs) -> str:
        return f"Run this in the background? {args.cmd}"

    def preview(self, args: StartArgs) -> tuple[str, ...]:
        return (args.cmd,)

    async def run(self, args: StartArgs, ctx: ToolContext) -> ToolResult:
        cwd = resolve(args.cwd, ctx)
        if not cwd.is_dir():
            return ToolResult.fail(f"{args.cwd} is not a folder")
        try:
            job = await self._jobs.start(
                args.cmd,
                cwd,
                clean_env(os.environ, ctx),
                task_id=ctx.task_id,
                request=ctx.request,
                then=args.then.strip(),
            )
        except (OSError, RuntimeError) as exc:
            return ToolResult.fail(f"could not start it: {exc}")
        after = (
            f"if this task has ended by then, Arcee will: {job.then}"
            if job.then
            else "a notification says when it ends"
        )
        return ToolResult.ok(
            f"Started {job.id} in the background: `{args.cmd}`",
            data={"job": job.id, "cmd": args.cmd, "cwd": display(cwd, ctx), "after": after},
        )


# --- job.check / job.stop ---------------------------------------------------------------------


class JobArgs(ToolArgs):
    job: str = Field(default="", description="The job id from shell.start; empty: all jobs")


class JobCheck(Tool[JobArgs]):
    name = "job.check"
    description = "How background jobs are doing: running or finished, exit code, last output."
    usage = Usage(
        when="To read a job's output, or see what is running.",
        avoid="Waiting for one to end: control.wait_for(event='job_done') returns when it does.",
        example='job.check(job="job1-12345")',
    )
    args_model = JobArgs
    untrusted = True  # command output

    def __init__(self, jobs: Jobs | None = None) -> None:
        self._jobs = jobs or JOBS

    async def run(self, args: JobArgs, ctx: ToolContext) -> ToolResult:
        if args.job:
            job = self._jobs.get(args.job)
            if job is None:
                return ToolResult.fail(f"no job {args.job!r}; job.check() lists them")
            if job.done:
                job.seen = True
            lines = job.tail(400).strip().splitlines()
            last = f"; last line: {lines[-1][:100]}" if lines else "; no output"
            return ToolResult.ok(f"{job.id} {job.state()}{last}", data=job.report())
        jobs = self._jobs.all()
        if not jobs:
            return ToolResult.ok("No background jobs", data={"jobs": []})
        for job in jobs:
            if job.done and job.task_id == ctx.task_id:
                job.seen = True
        listed = [{"job": j.id, "cmd": j.cmd, "state": j.state()} for j in jobs]
        running = sum(not j.done for j in jobs)
        return ToolResult.ok(f"{len(jobs)} jobs, {running} running", data={"jobs": listed})


class JobStop(Tool[JobArgs]):
    name = "job.stop"
    description = "Stop a background job started with shell.start."
    usage = Usage(
        when="The user asks to stop it, or it is clearly stuck.",
        example='job.stop(job="job1-12345")',
    )
    args_model = JobArgs

    def __init__(self, jobs: Jobs | None = None) -> None:
        self._jobs = jobs or JOBS

    def sentence(self, args: JobArgs) -> str:
        return f"Stop the background job {args.job}?"

    async def run(self, args: JobArgs, ctx: ToolContext) -> ToolResult:
        job = self._jobs.get(args.job) if args.job else None
        if job is None:
            return ToolResult.fail(f"no job {args.job!r}; job.check() lists them")
        if not await self._jobs.stop(job):
            return ToolResult.ok(f"{job.id} had already {job.state()}", data=job.report())
        job.seen = True
        return ToolResult.ok(f"Stopped {job.id}: `{job.cmd}`", data={"job": job.id})


# --- control.set_timer ------------------------------------------------------------------------


class TimerArgs(ToolArgs):
    minutes: float = Field(ge=0.5, le=1440, description="When to check (the first time)")
    check: str = Field(
        min_length=3,
        max_length=500,
        description="What to do then, as a request that stands alone: 'Check whether the "
        "render in ~/Videos/out.mp4 finished and tell me'",
    )
    watch: (
        Literal[
            "page_changes",
            "page_contains",
            "page_lacks",
            "file_exists",
            "file_gone",
            "command_succeeds",
        ]
        | None
    ) = Field(
        default=None,
        description="Optional: check this without the model every `every_minutes`, and run "
        "`check` only once it happens",
    )
    target: str = Field(default="", description="The watched page's address, file or command")
    text: str = Field(default="", description="page_contains / page_lacks: the words")
    every_minutes: float = Field(default=0, ge=0, le=1440, description="0: same as minutes")
    for_hours: float = Field(default=24, gt=0, le=LONGEST_WATCH_H, description="Give up after")


class SetTimer(Tool[TimerArgs]):
    name = "control.set_timer"
    description = (
        "Arcee's own timer: check something again later, without waiting now. After "
        "`minutes` it runs `check` as a new request; with `watch`, it first checks a page, "
        "file or read-only command every so often, without the model, and runs `check` "
        "once it happens."
    )
    usage = Usage(
        when='"Check the render again in 5 minutes", "watch this page and tell me when it '
        'says In stock", "tell me when the download appears". Listed and cancelled with '
        "reminder.list and reminder.cancel.",
        avoid="A reminder for the user (reminder.add); something that ends within a few "
        "minutes (control.wait_for); a schedule that repeats (routine.add).",
        mistakes="A `check` that only makes sense inside this task: it runs as a new request.",
        example='control.set_timer(minutes=30, check="Tell me the iPhone page says In stock '
        'now", watch="page_contains", target="https://example.com/iphone", text="In stock")',
    )
    args_model = TimerArgs
    timeout_s = 30
    _policy: ClassVar[Policy] = Policy()

    def __init__(
        self,
        reminders: Reminders,
        *,
        read_page: PageText = page_text,
        clock: Any = time.time,
    ) -> None:
        self._reminders = reminders
        self._read_page = read_page
        self._clock = clock

    async def run(self, args: TimerArgs, ctx: ToolContext) -> ToolResult:
        pending = await self._reminders.pending_reminders()
        if sum(r.kind == "followup" for r in pending) >= MAX_PENDING:
            return ToolResult.fail(
                f"{MAX_PENDING} follow-ups are waiting already; cancel one (reminder.list)"
            )
        now = self._clock()
        watch: Watch | None = None
        if args.watch is not None:
            made = await self._watch(args, ctx, now)
            if isinstance(made, str):
                return ToolResult.fail(made)
            watch = made
        due = now + args.minutes * 60
        detail = Detail(request=ctx.request, set_at=now, watch=watch)
        timer_id = await self._reminders.add_reminder(
            args.check, due, kind="followup", task_id=ctx.task_id, detail=detail.dumps()
        )
        at = datetime.fromtimestamp(due).strftime("%H:%M")
        if watch is None:
            summary = f"Arcee will check again at {at}: {args.check}"
        else:
            every = round(watch.every_s / 60, 1)
            until = datetime.fromtimestamp(watch.until).strftime("%a %H:%M")
            summary = (
                f"Watching from {at}, every {every:g} min until {until} "
                f"({args.watch} {watch.target}); then: {args.check}"
            )
        return ToolResult.ok(summary, data={"reminder_id": timer_id, "first": at})

    async def _watch(self, args: TimerArgs, ctx: ToolContext, now: float) -> Watch | str:
        """The watch the arguments describe, or why it cannot be made."""
        target = args.target.strip()
        if not target:
            return f"watch={args.watch} needs a target"
        kind = args.watch or ""
        baseline = ""
        if kind.startswith("page_"):
            parsed = urlparse(target)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return "a watched page needs an http or https address"
            if kind != "page_changes" and not args.text.strip():
                return f"watch={kind} needs the words in `text`"
            try:
                page = await self._read_page(target)
            except Exception as exc:
                return f"cannot read {target} now: {exc}"
            if kind == "page_changes":
                baseline = digest(page)
            found = args.text.lower() in page.lower()
            if (kind == "page_contains" and found) or (kind == "page_lacks" and not found):
                state = "says" if found else "does not say"
                return f"it has already happened: {target} {state} “{args.text}”"
        elif kind in ("file_exists", "file_gone"):
            target = str(resolve(target, ctx))
        elif self._policy.classify_shell(target).tier != Tier.AUTO:
            return "only read-only commands (ls, grep, pgrep, …) can be watched"
        every = (args.every_minutes or args.minutes) * 60
        return Watch(
            kind=kind,
            target=target,
            text=args.text.strip(),
            every_s=max(60.0, every),
            until=now + args.for_hours * 3600,
            baseline=baseline,
        )


def later_tools(reminders: Reminders | None = None) -> list[Tool[Any]]:
    tools: list[Tool[Any]] = [ShellStart(), JobCheck(), JobStop()]
    if reminders is not None:
        tools.append(SetTimer(reminders))
    return tools
