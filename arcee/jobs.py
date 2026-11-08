"""Background jobs (decision 0020): long commands that run without the model waiting.

`shell.start` starts one: a build, a test run, downloads. arceed keeps the process and
its output (a log file), and the task goes on, or ends. `control.wait_for` can wait for
it inside the task, and `job.check` looks at it. When a job ends and the model never
saw it end (the task finished first), arceed says so: a notification, or, when the
model asked for one (`then`), a follow-up request with the result, so the model is
called again only once there is something to read.

Jobs live as long as arceed: they are not kept across a restart (a log stays on disk).
"""

import asyncio
import contextlib
import itertools
import logging
import os
import shutil
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

MAX_RUNNING: Final = 8
MAX_KEPT: Final = 30  # finished jobs remembered for job.check
LIMITS: Final = ("-p", "MemoryMax=4G", "-p", "CPUQuota=300%", "-p", "RuntimeMaxSec=14400")
TAIL_CHARS: Final = 3000


@dataclass(slots=True)
class Job:
    id: str
    cmd: str
    cwd: Path
    log_path: Path
    task_id: str
    request: str  # the request that started it, for the follow-up
    then: str  # what to do with the result once it ends; "" for a notification
    started: float = field(default_factory=time.time)
    ended: float | None = None
    exit_code: int | None = None
    stopped: bool = False  # job.stop ended it
    seen: bool = False  # the model saw it end (wait_for, job.check)
    _process: asyncio.subprocess.Process | None = None

    @property
    def done(self) -> bool:
        return self.ended is not None

    def took(self) -> str:
        """How long it has run: "40 s", "3.5 min"."""
        seconds = (self.ended or time.time()) - self.started
        return f"{seconds:.0f} s" if seconds < 60 else f"{seconds / 60:.1f} min".replace(".0 ", " ")

    def state(self) -> str:
        if not self.done:
            return f"running for {self.took()}"
        if self.stopped:
            return "stopped"
        return f"finished with exit code {self.exit_code} after {self.took()}"

    def tail(self, chars: int = TAIL_CHARS) -> str:
        """The end of its output (stdout and stderr together)."""
        try:
            with self.log_path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - chars))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return ("…" + text) if size > chars else text

    def report(self) -> dict[str, object]:
        out: dict[str, object] = {"job": self.id, "cmd": self.cmd, "state": self.state()}
        if self.done and not self.stopped:
            out["exit_code"] = self.exit_code
        out["output_tail"] = self.tail()
        out["log"] = str(self.log_path)
        return out


Finished = Callable[[Job], Awaitable[None]]


class Jobs:
    """The background jobs arceed runs."""

    def __init__(self, log_dir: Path | None = None, *, limits: bool = True) -> None:
        self._log_dir = log_dir
        self._limits = limits and shutil.which("systemd-run") is not None
        self._jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._watchers: set[asyncio.Task[None]] = set()
        # Set by arceed: a job ended that the model has not seen end.
        self.on_finished: Finished | None = None

    def log_dir(self) -> Path:
        if self._log_dir is None:
            from arcee.context.store import data_dir

            self._log_dir = data_dir() / "jobs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        return self._log_dir

    def running(self) -> list[Job]:
        return [j for j in self._jobs.values() if not j.done]

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id.strip())

    async def start(
        self,
        cmd: str,
        cwd: Path,
        env: Mapping[str, str],
        *,
        task_id: str,
        request: str = "",
        then: str = "",
    ) -> Job:
        if len(self.running()) >= MAX_RUNNING:
            raise RuntimeError(f"{MAX_RUNNING} jobs are running already; wait for one or stop one")
        job_id = f"job{next(self._ids)}-{int(time.time()) % 100000}"
        log_path = self.log_dir() / f"{job_id}.log"
        argv = ["bash", "-c", cmd]
        if self._limits:
            argv = ["systemd-run", "--user", "--scope", "--quiet", *LIMITS, *argv]
        with log_path.open("wb") as out:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=dict(env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # its own process group, to stop it whole
            )
        job = Job(job_id, cmd, cwd, log_path, task_id, request, then, _process=process)
        self._jobs[job_id] = job
        watcher = asyncio.ensure_future(self._watch(job, process))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        self._forget_old()
        return job

    async def _watch(self, job: Job, process: asyncio.subprocess.Process) -> None:
        job.exit_code = await process.wait()
        job.ended = time.time()
        job._process = None  # pyright: ignore[reportPrivateUsage]
        log.info("job %s %s: %s", job.id, job.state(), job.cmd)
        if self.on_finished is not None and not job.stopped:
            try:
                await self.on_finished(job)
            except Exception:
                log.exception("could not report the end of job %s", job.id)

    async def stop(self, job: Job) -> bool:
        """End a running job (its whole process group); False when it had already ended."""
        process = job._process  # pyright: ignore[reportPrivateUsage]
        if job.done or process is None:
            return False
        job.stopped = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        for _ in range(20):
            if job.done:
                return True
            await asyncio.sleep(0.25)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        return True

    def _forget_old(self) -> None:
        finished = [j for j in self._jobs.values() if j.done]
        for job in finished[: max(0, len(finished) - MAX_KEPT)]:
            del self._jobs[job.id]


JOBS: Final = Jobs()  # arceed's; tools and the daemon share it
