import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from arcee.context.redact import REDACTED, redact, redact_value
from arcee.context.store import Store, data_dir, new_task_id
from arcee.core.executor import ActionRecord, Executor
from arcee.core.gate import Gate
from arcee.core.policy import Policy, Tier
from arcee.core.protocol import ConfirmRequest, ExecutorResult, PlannerOutput
from arcee.tools.base import ToolContext
from arcee.tools.builtin import builtin_registry

KEY = "AIzaSyD" + "x" * 32


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path / "arcee")
    yield store
    store.close()


def action(task_id: str, n: int, *, args: dict[str, object], status: str = "ok") -> ActionRecord:
    result = ExecutorResult(
        n=n,
        tool="shell.run",
        status=status,  # pyright: ignore[reportArgumentType]
        summary=f"step {n}",
        data={"stdout": f"token={KEY}"},
        duration_ms=7,
    )
    return ActionRecord(
        ts=1000.0 + n,
        task_id=task_id,
        args=args,
        tier=Tier.AUTO,
        approved_by="policy",
        result=result,
    )


class TestRedact:
    @pytest.mark.parametrize(
        "secret",
        [
            KEY,
            "sk-proj-" + "a" * 40,
            "ghp_" + "b" * 36,
            "nvapi-" + "c" * 60,
            "gsk_" + "d" * 40,
            "Bearer " + "e" * 30,
        ],
    )
    def test_known_key_formats(self, secret: str) -> None:
        assert redact(f"use {secret} now") == f"use {REDACTED} now"

    def test_assigned_secrets(self) -> None:
        assert redact("DB_PASSWORD=hunter2 user=me") == f"DB_PASSWORD={REDACTED} user=me"
        assert redact("api_key: abc123") == f"api_key: {REDACTED}"

    def test_nested_values(self) -> None:
        assert redact_value({"a": [f"k {KEY}", 3], "b": None}) == {
            "a": [f"k {REDACTED}", 3],
            "b": None,
        }

    def test_leaves_ordinary_text(self) -> None:
        assert redact("du -sh ~/Downloads") == "du -sh ~/Downloads"


class TestStore:
    async def test_records_a_task_end_to_end(self, store: Store) -> None:
        task_id = await store.start_task("free space?")
        await store.record_action(action(task_id, 1, args={"cmd": "df -h"}))
        plan = PlannerOutput.model_validate(
            {"goal": "g", "interpretation": "", "steps": [{"id": 1, "do": "d", "success": "s"}]}
        )
        await store.finish_task(
            task_id, status="done", reply="44G free", steps=2, model_calls=3, plan=plan
        )

        found = await store.task(task_id)

        assert found is not None
        task, steps = found
        assert (task.request, task.status, task.reply, task.steps, task.model_calls) == (
            "free space?",
            "done",
            "44G free",
            2,
            3,
        )
        assert task.finished is not None
        [step] = steps
        assert (step.n, step.tool, step.args, step.tier, step.approved_by) == (
            1,
            "shell.run",
            {"cmd": "df -h"},
            "auto",
            "policy",
        )
        assert await store.observation(task_id, 1) == {
            "data": {"stdout": f"token={REDACTED}"},
            "output": None,
            "error": None,
        }

    async def test_secrets_never_reach_disk(self, store: Store) -> None:
        task_id = await store.start_task(f"use key {KEY}")
        await store.record_action(action(task_id, 1, args={"cmd": f"curl -H 'k: {KEY}'"}))
        await store.finish_task(task_id, status="done", reply=KEY, steps=1, model_calls=1)
        store.close()

        on_disk = (store.root / "history.db").read_bytes() + (
            store.root / "logs" / "actions.jsonl"
        ).read_bytes()

        assert KEY.encode() not in on_disk
        reopened = Store(store.root)
        assert (await reopened.recent())[0].request == f"use key {REDACTED}"
        reopened.close()

    async def test_action_log_has_one_line_per_call(self, store: Store) -> None:
        task_id = await store.start_task("x")
        await store.record_action(action(task_id, 1, args={"cmd": "ls"}))
        await store.record_action(action(task_id, 2, args={"cmd": "sudo ls"}, status="refused"))

        lines = (store.root / "logs" / "actions.jsonl").read_text().splitlines()

        assert [json.loads(line) for line in lines] == [
            {
                "ts": 1001.0,
                "task_id": task_id,
                "n": 1,
                "tool": "shell.run",
                "args": {"cmd": "ls"},
                "tier": "auto",
                "approved_by": "policy",
                "status": "ok",
                "duration_ms": 7,
            },
            {
                "ts": 1002.0,
                "task_id": task_id,
                "n": 2,
                "tool": "shell.run",
                "args": {"cmd": "sudo ls"},
                "tier": "auto",
                "approved_by": "policy",
                "status": "refused",
                "duration_ms": 7,
            },
        ]

    async def test_recent_is_newest_first(self, store: Store) -> None:
        first = await store.start_task("one", task_id=new_task_id(1.0))
        second = await store.start_task("two", task_id=new_task_id(2.0))

        assert [t.id for t in await store.recent()] == [second, first]
        assert len(await store.recent(limit=1)) == 1

    async def test_unknown_task(self, store: Store) -> None:
        assert await store.task("t_nope") is None
        assert await store.observation("t_nope", 1) is None


def test_task_ids_sort_by_time() -> None:
    assert new_task_id(1.0) < new_task_id(2.0)
    assert new_task_id().startswith("t_")


def test_data_dir_follows_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ARCEE_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert data_dir() == tmp_path / "arcee"
    monkeypatch.setenv("ARCEE_DATA_DIR", "/elsewhere")
    assert data_dir() == Path("/elsewhere")


class Deny:
    async def confirm(self, request: ConfirmRequest) -> bool:
        return False


async def test_executor_records_every_call_including_refusals(tmp_path: Path) -> None:
    records: list[ActionRecord] = []

    async def keep(record: ActionRecord) -> None:
        records.append(record)

    tools = builtin_registry(shell_limits=False)
    executor = Executor(Gate(Policy(), Deny()), record=keep)
    ctx = ToolContext(task_id="t_1", home=tmp_path)

    await executor.execute(tools.validate("fs.list", {"path": "~"}), 1, ctx)
    await executor.execute(tools.validate("fs.trash", {"paths": ["~/x"]}), 2, ctx)

    assert [(r.result.tool, r.tier, r.approved_by, r.result.status) for r in records] == [
        ("fs.list", Tier.AUTO, "policy", "ok"),
        ("fs.trash", Tier.CONFIRM, None, "refused"),
    ]
    assert records[1].args == {"paths": ["~/x"]}
