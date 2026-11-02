"""Task history and memory (spec section 8): SQLite plus the JSONL action log.

`history.db` holds `tasks`, `steps` and `observations` (the full tool data, so
nothing the model saw is lost), and memory (decision 0014): `memories`, the facts
Arcee keeps about the user, and `summaries`, one per finished task, each with a
full-text index. `logs/actions.jsonl` gets one line per call for the safety audit.
Secrets are redacted before anything is written. All disk work runs in a worker
thread, serialised by a lock, so the event loop never blocks on SQLite (spec
section 12).
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

SCHEMA_VERSION: Final = 2
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
CREATE TABLE IF NOT EXISTS memories (
    id         TEXT PRIMARY KEY,
    task_id    TEXT,
    text       TEXT NOT NULL,
    importance REAL NOT NULL,
    created    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_started ON tasks(started);
"""
# Version 2: memory (decision 0014). Where a fact came from (`note`: control.note or
# the user; `digest`: learned after a task), when it last changed, the task summaries,
# and full-text indexes that follow both tables through triggers.
_MEMORY_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS summaries (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    text    TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    text, content='memories', content_rowid='rowid', tokenize='porter unicode61'
);
-- A task is found by its request and its summary together: a summary may never repeat
-- the request's words ("Booked Abu Road to Delhi" for "book my usual train home").
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
    task_id UNINDEXED, text, tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS summaries_ai AFTER INSERT ON summaries BEGIN
    INSERT INTO summaries_fts(task_id, text) VALUES (
        new.task_id,
        COALESCE((SELECT request FROM tasks WHERE id = new.task_id), '') || ' ' || new.text
    );
END;
CREATE TRIGGER IF NOT EXISTS summaries_ad AFTER DELETE ON summaries BEGIN
    DELETE FROM summaries_fts WHERE task_id = old.task_id;
END;
CREATE TRIGGER IF NOT EXISTS summaries_au AFTER UPDATE ON summaries BEGIN
    DELETE FROM summaries_fts WHERE task_id = old.task_id;
    INSERT INTO summaries_fts(task_id, text) VALUES (
        new.task_id,
        COALESCE((SELECT request FROM tasks WHERE id = new.task_id), '') || ' ' || new.text
    );
END;
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


@dataclass(frozen=True, slots=True)
class MemoryRow:
    id: str
    task_id: str | None
    text: str
    importance: float
    created: float
    updated: float
    source: str  # "note" (control.note or the user) or "digest" (learned after a task)


@dataclass(frozen=True, slots=True)
class SummaryRow:
    task_id: str
    request: str
    text: str
    created: float


_MEMORY_COLUMNS: Final = (
    "id, task_id, text, importance, created, COALESCE(updated, created), source"
)


def match_query(text: str) -> str:
    """An FTS5 query matching any content word of `text` ("" when there is none)."""
    from arcee.context.examples import tokens

    words = dict.fromkeys(tokens(text))  # once each, in order
    return " OR ".join(f'"{word}"' for word in words)


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
        self._migrate()
        self._lock = asyncio.Lock()

    def _migrate(self) -> None:
        """Bring an older history.db up to SCHEMA_VERSION, keeping everything in it."""
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        with self._db:
            if version < 2:
                columns = {row[1] for row in self._db.execute("PRAGMA table_info(memories)")}
                if "source" not in columns:
                    self._db.execute(
                        "ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT 'note'"
                    )
                if "updated" not in columns:
                    self._db.execute("ALTER TABLE memories ADD COLUMN updated REAL")
                self._db.executescript(_MEMORY_SCHEMA)
                # Index what was saved before the indexes existed.
                self._db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

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

    async def save_note(self, task_id: str, text: str, importance: float) -> str:
        """`control.note`: a durable memory the user can see and delete (spec section 8)."""
        note_id = f"m_{secrets.token_hex(4)}"

        def write() -> None:
            with self._db:
                now = time.time()
                self._db.execute(
                    "INSERT INTO memories (id, task_id, text, importance, created, source, "
                    "updated) VALUES (?, ?, ?, ?, ?, 'note', ?)",
                    (note_id, task_id, redact(text), importance, now, now),
                )

        await self._run(write)
        return note_id

    # --- memory (decision 0014) ------------------------------------------------------

    async def save_memory(
        self,
        text: str,
        importance: float,
        *,
        task_id: str | None = None,
        source: str = "digest",
        replaces: str | None = None,
    ) -> str:
        """A fact to keep; `replaces` updates that fact instead (a newer address)."""
        text, importance = redact(text.strip()), min(1.0, max(0.0, importance))

        def write() -> str:
            now = time.time()
            with self._db:
                if replaces is not None:
                    changed = self._db.execute(
                        "UPDATE memories SET text = ?, importance = MAX(importance, ?), "
                        "task_id = COALESCE(?, task_id), updated = ? WHERE id = ?",
                        (text, importance, task_id, now, replaces),
                    ).rowcount
                    if changed:
                        return replaces
                same = self._db.execute(
                    "SELECT id FROM memories WHERE lower(text) = lower(?)", (text,)
                ).fetchone()
                if same is not None:  # said again: it matters, and it is still true
                    self._db.execute(
                        "UPDATE memories SET importance = MAX(importance, ?), updated = ? "
                        "WHERE id = ?",
                        (importance, now, same[0]),
                    )
                    return str(same[0])
                memory_id = f"m_{secrets.token_hex(4)}"
                self._db.execute(
                    "INSERT INTO memories (id, task_id, text, importance, created, source, "
                    "updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (memory_id, task_id, text, importance, now, source, now),
                )
                return memory_id

        return await self._run(write)

    async def update_memory(
        self, memory_id: str, *, text: str | None = None, importance: float | None = None
    ) -> bool:
        def write() -> bool:
            with self._db:
                return bool(
                    self._db.execute(
                        "UPDATE memories SET text = COALESCE(?, text), "
                        "importance = COALESCE(?, importance), updated = ? WHERE id = ?",
                        (
                            None if text is None else redact(text.strip()),
                            importance,
                            time.time(),
                            memory_id,
                        ),
                    ).rowcount
                )

        return await self._run(write)

    async def forget_memory(self, memory_id: str) -> bool:
        def write() -> bool:
            with self._db:
                return bool(
                    self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount
                )

        return await self._run(write)

    async def forget_task(self, task_id: str) -> int:
        """Forget what one task taught (its facts and summary); the task stays in History."""

        def write() -> int:
            with self._db:
                facts = self._db.execute(
                    "DELETE FROM memories WHERE task_id = ?", (task_id,)
                ).rowcount
                self._db.execute("DELETE FROM summaries WHERE task_id = ?", (task_id,))
                return facts

        return await self._run(write)

    async def all_memories(self) -> list[MemoryRow]:
        def read() -> list[MemoryRow]:
            rows = self._db.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories ORDER BY importance DESC, created DESC"
            ).fetchall()
            return [MemoryRow(*row) for row in rows]

        return await self._run(read)

    async def top_memories(self, min_importance: float, limit: int) -> list[MemoryRow]:
        def read() -> list[MemoryRow]:
            rows = self._db.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE importance >= ? "
                "ORDER BY importance DESC, updated DESC LIMIT ?",
                (min_importance, limit),
            ).fetchall()
            return [MemoryRow(*row) for row in rows]

        return await self._run(read)

    async def search_memories(self, query: str, limit: int) -> list[tuple[MemoryRow, float]]:
        """Facts matching `query`, best first, with their BM25 score (higher is better)."""
        match = match_query(query)
        if not match:
            return []

        def read() -> list[tuple[MemoryRow, float]]:
            rows = self._db.execute(
                "SELECT m.id, m.task_id, m.text, m.importance, m.created, "
                "COALESCE(m.updated, m.created), m.source, -bm25(memories_fts) "
                "FROM memories_fts JOIN memories m ON m.rowid = "
                "memories_fts.rowid WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) "
                "LIMIT ?",
                (match, limit),
            ).fetchall()
            return [(MemoryRow(*row[:7]), float(row[7])) for row in rows]

        return await self._run(read)

    async def save_summary(self, task_id: str, text: str) -> None:
        def write() -> None:
            with self._db:
                self._db.execute(
                    "INSERT INTO summaries (task_id, text, created) VALUES (?, ?, ?) "
                    "ON CONFLICT(task_id) DO UPDATE SET text = excluded.text",
                    (task_id, redact(text.strip()), time.time()),
                )

        await self._run(write)

    async def search_summaries(
        self, query: str, limit: int, *, exclude: str | None = None
    ) -> list[tuple[SummaryRow, float]]:
        """Past tasks' summaries matching `query`, best first, with their BM25 score."""
        match = match_query(query)
        if not match:
            return []

        def read() -> list[tuple[SummaryRow, float]]:
            rows = self._db.execute(
                "SELECT s.task_id, t.request, s.text, s.created, -bm25(summaries_fts) "
                "FROM summaries_fts JOIN summaries s ON s.task_id = summaries_fts.task_id "
                "JOIN tasks t ON t.id = s.task_id WHERE summaries_fts MATCH ? "
                "AND s.task_id != ? ORDER BY bm25(summaries_fts) LIMIT ?",
                (match, exclude or "", limit),
            ).fetchall()
            return [(SummaryRow(*row[:4]), float(row[4])) for row in rows]

        return await self._run(read)

    async def memories(self, limit: int = 50) -> list[tuple[str, str, float]]:
        def read() -> list[tuple[str, str, float]]:
            return self._db.execute(
                "SELECT id, text, importance FROM memories ORDER BY created DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return await self._run(read)

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
