"""Task history (spec section 8, phase 1 subset): SQLite plus the JSONL action log.

`history.db` holds `tasks`, `steps` and `observations` (the full tool data, so
nothing the model saw is lost); `logs/actions.jsonl` gets one line per call for
the safety audit. Secrets are redacted before anything is written. All disk
work runs in a worker thread, serialised by a lock, so the event loop never
blocks on SQLite (spec section 12).
"""

import asyncio
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from arcee.context.redact import redact, redact_value
from arcee.core.executor import ActionRecord
from arcee.core.protocol import PlannerOutput

SCHEMA_VERSION: Final = 1
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    request     TEXT NOT NULL,
    status      TEXT NOT NULL,
    reply       TEXT,
    plan        TEXT,
    steps       INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    started     REAL NOT NULL,
    finished    REAL
);
CREATE TABLE IF NOT EXISTS steps (
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    n           INTEGER NOT NULL,
    ts          REAL NOT NULL,
    tool        TEXT NOT NULL,
    args        TEXT NOT NULL,
    tier        TEXT NOT NULL,
    approved_by TEXT,
    status      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    PRIMARY KEY (task_id, n)
);
CREATE TABLE IF NOT EXISTS observations (
    task_id TEXT NOT NULL,
    n       INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (task_id, n),
    FOREIGN KEY (task_id, n) REFERENCES steps(task_id, n)
);
CREATE INDEX IF NOT EXISTS tasks_started ON tasks(started);
"""


def data_dir() -> Path:
    """`$ARCEE_DATA_DIR`, else `$XDG_DATA_HOME/arcee`, else `~/.local/share/arcee`."""
    if explicit := os.environ.get("ARCEE_DATA_DIR"):
        return Path(explicit)
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "arcee"


def new_task_id(now: float | None = None) -> str:
    """Time-ordered and unique enough for one user: milliseconds plus 24 random bits."""
    millis = int((time.time() if now is None else now) * 1000)
    return f"t_{millis:x}{secrets.token_hex(3)}"


@dataclass(frozen=True, slots=True)
class TaskRow:
    id: str
    request: str
    status: str
    reply: str | None
    steps: int
    model_calls: int
    started: float
    finished: float | None


@dataclass(frozen=True, slots=True)
class StepRow:
    n: int
    tool: str
    args: dict[str, Any]
    tier: str
    approved_by: str | None
    status: str
    summary: str
    duration_ms: int


class Store:
    def __init__(self, root: Path) -> None:
        """Open (and create if needed) the store under `root`."""
        self.root = root
        (root / "logs").mkdir(parents=True, exist_ok=True)
        self._log_path = root / "logs" / "actions.jsonl"
        self._db = sqlite3.connect(root / "history.db", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._db.close()

    async def start_task(self, request: str, *, task_id: str | None = None) -> str:
        task_id = task_id or new_task_id()

        def write() -> None:
            with self._db:
                self._db.execute(
                    "INSERT INTO tasks (id, request, status, started) VALUES (?, ?, 'running', ?)",
                    (task_id, redact(request), time.time()),
                )

        await self._run(write)
        return task_id

    async def record_action(self, record: ActionRecord) -> None:
        """The executor's hook: one `steps` row, its observation, one log line."""
        result = record.result
        args = redact_value(record.args)
        observation = redact_value(
            {"data": result.data, "output": result.output, "error": result.error}
        )
        line = {
            "ts": round(record.ts, 3),
            "task_id": record.task_id,
            "n": result.n,
            "tool": result.tool,
            "args": args,
            "tier": str(record.tier),
            "approved_by": record.approved_by,
            "status": result.status,
            "duration_ms": result.duration_ms,
        }

        def write() -> None:
            with self._db:
                self._db.execute(
                    "INSERT OR REPLACE INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.task_id,
                        result.n,
                        record.ts,
                        result.tool,
                        json.dumps(args),
                        str(record.tier),
                        record.approved_by,
                        result.status,
                        redact(result.summary),
                        result.duration_ms,
                    ),
                )
                self._db.execute(
                    "INSERT OR REPLACE INTO observations VALUES (?, ?, ?)",
                    (record.task_id, result.n, json.dumps(observation)),
                )
            with self._log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(line, ensure_ascii=False) + "\n")

        await self._run(write)

    async def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        reply: str,
        steps: int,
        model_calls: int,
        plan: PlannerOutput | None = None,
    ) -> None:
        plan_json = plan.model_dump_json() if plan is not None else None

        def write() -> None:
            with self._db:
                self._db.execute(
                    "UPDATE tasks SET status = ?, reply = ?, plan = ?, steps = ?, "
                    "model_calls = ?, finished = ? WHERE id = ?",
                    (status, redact(reply), plan_json, steps, model_calls, time.time(), task_id),
                )

        await self._run(write)

    async def recent(self, limit: int = 10) -> list[TaskRow]:
        def read() -> list[TaskRow]:
            rows = self._db.execute(
                "SELECT id, request, status, reply, steps, model_calls, started, finished "
                "FROM tasks ORDER BY started DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [TaskRow(*row) for row in rows]

        return await self._run(read)

    async def task(self, task_id: str) -> tuple[TaskRow, list[StepRow]] | None:
        def read() -> tuple[TaskRow, list[StepRow]] | None:
            row = self._db.execute(
                "SELECT id, request, status, reply, steps, model_calls, started, finished "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            steps = self._db.execute(
                "SELECT n, tool, args, tier, approved_by, status, summary, duration_ms "
                "FROM steps WHERE task_id = ? ORDER BY n",
                (task_id,),
            ).fetchall()
            return TaskRow(*row), [
                StepRow(n, tool, json.loads(args), tier, by, status, summary, ms)
                for n, tool, args, tier, by, status, summary, ms in steps
            ]

        return await self._run(read)

    async def observation(self, task_id: str, n: int) -> dict[str, Any] | None:
        def read() -> dict[str, Any] | None:
            row = self._db.execute(
                "SELECT content FROM observations WHERE task_id = ? AND n = ?", (task_id, n)
            ).fetchone()
            return None if row is None else json.loads(row[0])

        return await self._run(read)

    async def _run[T](self, work: Callable[[], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(work)
