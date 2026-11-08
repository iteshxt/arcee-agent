"""Background jobs and Arcee's own follow-ups (decision 0020)."""

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arcee import followups
from arcee.context.store import SCHEMA_VERSION, Store
from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import filters_for
from arcee.followups import Detail, Watch, digest, request_for
from arcee.jobs import Job, Jobs
from arcee.models.base import Message
from arcee.tools.base import ToolContext
from arcee.tools.extra import WaitFor, WaitForArgs
from arcee.tools.later import (
    JobArgs,
    JobCheck,
    JobStop,
    SetTimer,
    ShellStart,
    StartArgs,
    TimerArgs,
)

NOW = 1_790_000_000.0


@pytest.fixture
def store(tmp_path: Path) -> Any:
    opened = Store(tmp_path / "db")
    yield opened
    opened.close()


@pytest.fixture
def jobs(tmp_path: Path) -> Jobs:
    return Jobs(tmp_path / "jobs", limits=False)


def ctx(tmp_path: Path, task: str = "t_1") -> ToolContext:
    return ToolContext(task, tmp_path, request="Build the app and tell me if the tests pass")


async def until_done(job: Job) -> None:
    for _ in range(200):
        if job.done:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{job.id} did not end")


# --- jobs ---------------------------------------------------------------------------------


async def test_a_job_runs_on_and_reports_its_end(jobs: Jobs, tmp_path: Path) -> None:
    ended: list[Job] = []

    async def finished(job: Job) -> None:
        ended.append(job)

    jobs.on_finished = finished
    job = await jobs.start(
        "echo building; echo oops >&2; exit 3", tmp_path, dict(os.environ), task_id="t_1"
    )
    await until_done(job)
    await asyncio.sleep(0.05)

    assert ended == [job]
    assert job.exit_code == 3
    assert job.tail() == "building\noops\n"
    assert job.state().startswith("finished with exit code 3")


async def test_a_stopped_job_ends_its_whole_group_quietly(jobs: Jobs, tmp_path: Path) -> None:
    ended: list[Job] = []

    async def finished(job: Job) -> None:
        ended.append(job)

    jobs.on_finished = finished
    job = await jobs.start("sleep 30 & sleep 30; wait", tmp_path, {}, task_id="t_1")

    assert await jobs.stop(job)
    await until_done(job)
    assert job.state() == "stopped"
    assert ended == []
    assert not await jobs.stop(job)  # already over


def test_shell_start_is_tiered_like_shell_run() -> None:
    tool, policy = ShellStart(), Policy()

    assert tool.tier_for(StartArgs(cmd="make && make test"), policy) == Tier.CONFIRM
    assert tool.tier_for(StartArgs(cmd="ls ~/Downloads"), policy) == Tier.AUTO
    assert tool.tier_for(StartArgs(cmd="rm -rf ~"), policy) == Tier.NEVER


async def test_start_wait_then_read_the_output(jobs: Jobs, tmp_path: Path) -> None:
    started = await ShellStart(jobs).run(
        StartArgs(cmd="sleep 0.3; echo 12 passed", then="Tell me if the tests passed"),
        ctx(tmp_path),
    )
    assert started.data is not None
    job_id = started.data["job"]
    assert "Arcee will: Tell me if the tests passed" in started.data["after"]

    waited = await WaitFor(jobs=jobs).run(
        WaitForArgs(event="job_done", target=job_id, timeout_s=10), ctx(tmp_path)
    )
    read = await JobCheck(jobs).run(JobArgs(job=job_id), ctx(tmp_path))

    assert "finished with exit code 0" in waited.summary
    assert read.data is not None
    assert read.data["output_tail"] == "12 passed\n"
    job = jobs.get(job_id)
    assert job is not None
    assert job.seen
    assert job.request == "Build the app and tell me if the tests pass"


async def test_unknown_jobs_and_folders_are_said(jobs: Jobs, tmp_path: Path) -> None:
    waited = await WaitFor(jobs=jobs).run(
        WaitForArgs(event="job_done", target="job9"), ctx(tmp_path)
    )
    checked = await JobCheck(jobs).run(JobArgs(job="job9"), ctx(tmp_path))
    stopped = await JobStop(jobs).run(JobArgs(job="job9"), ctx(tmp_path))
    nowhere = await ShellStart(jobs).run(StartArgs(cmd="ls", cwd="~/nope"), ctx(tmp_path))
    listed = await JobCheck(jobs).run(JobArgs(), ctx(tmp_path))

    assert waited.error is not None
    assert "no job 'job9'" in waited.error
    assert checked.status == stopped.status == nowhere.status == "error"
    assert listed.summary == "No background jobs"


async def test_job_stop_tool(jobs: Jobs, tmp_path: Path) -> None:
    started = await ShellStart(jobs).run(StartArgs(cmd="sleep 30"), ctx(tmp_path))
    assert started.data is not None

    stopped = await JobStop(jobs).run(JobArgs(job=started.data["job"]), ctx(tmp_path))
    listed = await JobCheck(jobs).run(JobArgs(), ctx(tmp_path))

    assert stopped.summary.startswith("Stopped job")
    assert listed.data is not None
    assert listed.data["jobs"][0]["state"] == "stopped"


# --- control.set_timer ------------------------------------------------------------------


def page(text: str) -> Any:
    async def read(url: str) -> str:
        return text

    return read


async def test_a_plain_follow_up_is_a_reminder_with_its_origin(store: Any, tmp_path: Path) -> None:
    tool = SetTimer(store, clock=lambda: NOW)

    result = await tool.run(
        TimerArgs(minutes=5, check="Check whether ~/out.mp4 finished rendering"), ctx(tmp_path)
    )
    [row] = await store.pending_reminders()
    detail = Detail.loads(row.detail)

    assert result.summary.startswith("Arcee will check again at ")
    assert (row.kind, row.due, row.text) == (
        "followup",
        NOW + 300,
        "Check whether ~/out.mp4 finished rendering",
    )
    assert detail == Detail("Build the app and tell me if the tests pass", NOW, None)


async def test_a_page_watch_keeps_its_baseline_and_schedule(store: Any, tmp_path: Path) -> None:
    tool = SetTimer(store, read_page=page("iPhone 17: Sold out"), clock=lambda: NOW)

    result = await tool.run(
        TimerArgs(
            minutes=10,
            check="Tell me the iPhone is in stock",
            watch="page_changes",
            target="https://shop.example/iphone",
            every_minutes=30,
            for_hours=12,
        ),
        ctx(tmp_path),
    )
    [row] = await store.pending_reminders()
    watch = Detail.loads(row.detail).watch

    assert result.status == "ok"
    assert watch == Watch(
        kind="page_changes",
        target="https://shop.example/iphone",
        every_s=1800,
        until=NOW + 12 * 3600,
        baseline=digest("iPhone 17: Sold out"),
    )


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"watch": "page_contains", "target": "https://a.example", "text": "Sold"}, "already"),
        ({"watch": "page_contains", "target": "https://a.example"}, "needs the words"),
        ({"watch": "page_changes", "target": "file:///etc/passwd"}, "http or https"),
        ({"watch": "file_exists"}, "needs a target"),
        ({"watch": "command_succeeds", "target": "touch ~/x"}, "only read-only"),
    ],
)
async def test_watches_that_cannot_work_are_refused(
    store: Any, tmp_path: Path, args: dict[str, Any], error: str
) -> None:
    tool = SetTimer(store, read_page=page("Sold out"), clock=lambda: NOW)

    result = await tool.run(TimerArgs(minutes=5, check="Tell me", **args), ctx(tmp_path))

    assert result.error is not None
    assert error in result.error
    assert await store.pending_reminders() == []


async def test_a_file_watch_is_resolved_and_follow_ups_are_capped(
    store: Any, tmp_path: Path
) -> None:
    tool = SetTimer(store, clock=lambda: NOW)
    args = TimerArgs(minutes=1, check="Open it", watch="file_exists", target="~/out.mp4")

    first = await tool.run(args, ctx(tmp_path))
    for _ in range(followups.MAX_PENDING - 1):
        await tool.run(args, ctx(tmp_path))
    over = await tool.run(args, ctx(tmp_path))

    assert first.status == "ok"
    rows = await store.pending_reminders()
    watch = Detail.loads(rows[0].detail).watch
    assert watch is not None
    assert watch.target == str(tmp_path / "out.mp4")
    assert watch.every_s == 60  # one minute at least
    assert over.error is not None
    assert "10 follow-ups are waiting" in over.error


# --- firing --------------------------------------------------------------------------------


class Fired:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.notes: list[tuple[str, str]] = []
        self.rearmed: list[tuple[str, str, float | None]] = []

    async def run_request(self, request: str, late: float) -> bool:
        self.requests.append(request)
        return True

    async def notify(self, title: str, body: str) -> None:
        self.notes.append((title, body))

    async def set_reminder(
        self, reminder_id: str, *, status: str, due: float | None = None
    ) -> bool:
        self.rearmed.append((reminder_id, status, due))
        return True


async def fire(fired: Fired, detail: Detail, *, text: str = "Tell me", now: float = NOW) -> None:
    await followups.fire(
        "r_1",
        text,
        detail.dumps(),
        0.0,
        store=fired,
        run_request=fired.run_request,
        notify=fired.notify,
        clock=lambda: now,
        read_page=page("Price: 999"),
        run=lambda cmd: asyncio.sleep(0, result=cmd == "true"),
    )


async def test_a_plain_follow_up_runs_its_request() -> None:
    fired = Fired()

    await fire(fired, Detail("Render the video", NOW - 600), text="Is the render done?")

    [request] = fired.requests
    assert request.startswith("Is the render done?\n\n(A follow-up Arcee set for itself at ")
    assert request.endswith("while doing: “Render the video”.)")


async def test_a_watch_looks_again_until_it_happens_then_gives_up() -> None:
    waiting = Detail(
        "Watch the price",
        NOW - 600,
        Watch("page_contains", "https://shop.example", "Price: 899", 600, NOW + 3600),
    )
    happened = Detail(
        "Watch the price",
        NOW - 600,
        Watch("page_lacks", "https://shop.example", "Price: 1099", 600, NOW + 3600),
    )
    fired = Fired()

    await fire(fired, waiting)
    await fire(fired, waiting, now=NOW + 3500)  # no room for another look
    await fire(fired, happened)

    assert fired.rearmed == [("r_1", "pending", NOW + 600)]
    assert fired.notes[0][0] == "Arcee stopped watching"
    assert "never said “Price: 899”" in fired.notes[0][1]
    assert fired.requests == [
        request_for(
            "Tell me",
            happened,
            "the page https://shop.example no longer says “Price: 1099”",
        )
    ]


async def test_command_and_file_watches(tmp_path: Path) -> None:
    there = tmp_path / "out.mp4"
    there.write_text("x")

    assert await followups.check(Watch("file_exists", str(there))) is not None
    assert await followups.check(Watch("file_gone", str(there))) is None
    assert await followups.check(Watch("command_succeeds", "true")) == "`true` succeeded"
    assert await followups.check(Watch("command_succeeds", "false")) is None


# --- storage and offering ---------------------------------------------------------------


def test_history_from_version_6_gains_the_detail_column(tmp_path: Path) -> None:
    root = tmp_path / "old"
    Store(root).close()
    db = sqlite3.connect(root / "history.db")
    db.execute("ALTER TABLE reminders DROP COLUMN detail")
    db.execute("PRAGMA user_version=6")
    db.commit()
    db.close()

    Store(root).close()

    db = sqlite3.connect(root / "history.db")
    columns = {row[1] for row in db.execute("PRAGMA table_info(reminders)")}
    assert "detail" in columns
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
    db.close()


@pytest.mark.parametrize(
    "request_text",
    [
        "Build the project in the background and tell me when it's done",
        "Check the render again in 5 minutes",
        "Watch this page and tell me when the price drops: https://shop.example/x",
        "Download these five files and let me know when they're done",
    ],
)
def test_requests_about_later_get_the_later_tools(request_text: str) -> None:
    filters = filters_for([Message("user", request_text)])

    assert "control.set_timer" in filters
    assert "job.*" in filters


def test_other_requests_do_not() -> None:
    filters = filters_for([Message("user", "What are the biggest files in my Downloads?")])

    assert "control.set_timer" not in filters


def test_the_detail_json_survives_junk() -> None:
    assert Detail.loads("") == Detail("", 0.0)
    assert Detail.loads("{not json") == Detail("", 0.0)
    assert json.loads(Detail("x", 1.0).dumps()) == {"request": "x", "set_at": 1.0}


# --- a job that ends after its task -------------------------------------------------------


class Runner:
    def __init__(self) -> None:
        self.current_task: str | None = "t_1"
        self.presence = None
        self.stopped = False


async def test_arceed_tells_of_a_job_its_task_never_saw_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcee import daemon

    runner = Runner()
    service = daemon.AgentService(runner)  # pyright: ignore[reportArgumentType]
    ran: list[str] = []
    notes: list[str] = []

    async def run_routine(request: str, late: float) -> bool:
        ran.append(request)
        return True

    async def notify(title: str, body: str) -> None:
        notes.append(f"{title} | {body}")

    monkeypatch.setattr(service, "run_routine", run_routine)
    monkeypatch.setattr(daemon, "notify", notify)
    monkeypatch.setattr(daemon, "JOB_LOOK_S", 0.01)

    def job(then: str = "", *, seen: bool = False) -> Job:
        made = Job("job1-1", "make test", tmp_path, tmp_path / "j.log", "t_1", "Build it", then)
        made.ended, made.exit_code, made.seen = made.started + 60, 0, seen
        return made

    waiting = asyncio.ensure_future(service.job_finished(job("Tell me if it passed")))
    await asyncio.sleep(0.05)
    assert not waiting.done()  # its task is still running: it may yet look
    runner.current_task = None
    await waiting
    await service.job_finished(job())
    await service.job_finished(job("Tell me", seen=True))

    [request] = ran
    assert request.startswith("Tell me if it passed\n\nWhat Arcee saw: The background command")
    assert "job.check(job='job1-1') shows its output" in request
    assert notes == ["Done: make test | It finished with exit code 0 after 1 min."]
