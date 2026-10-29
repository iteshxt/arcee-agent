from pathlib import Path
from typing import Any

from arcee.eval.tasks import (
    Checks,
    Task,
    check_outcome,
    load_tasks,
    make_home,
    run_task,
    task_summary,
)
from arcee.models.base import Completion, ModelProfile, WireCall
from arcee.models.fake import FakeAdapter

TASKS = Path(__file__).parents[1] / "eval" / "tasks.toml"
PROFILE = ModelProfile(model="m", context_tokens=8192, cost_tier="local", call_mode="native")


def call(name: str, **args: Any) -> Completion:
    return Completion("", tool_calls=(WireCall(name, args),))


def test_repo_tasks_load() -> None:
    tasks = load_tasks(TASKS)

    assert len(tasks) >= 12
    assert len({t.id for t in tasks}) == len(tasks)
    assert all(t.checks != Checks() for t in tasks)


def test_make_home_builds_text_sized_files_and_folders(tmp_path: Path) -> None:
    task = Task("t", "r", {"a/b.txt": "hi", "c.bin": 5, "empty/": ""}, Checks())

    home = make_home(task, tmp_path)

    assert (home / "a" / "b.txt").read_text() == "hi"
    assert (home / "c.bin").stat().st_size == 5
    assert (home / "empty").is_dir()


def test_check_outcome(tmp_path: Path) -> None:
    home = tmp_path
    (home / "kept.txt").write_text("Milk and EGGS")
    checks = Checks(
        reply_has=("1099511627776",),
        exists=("kept.txt",),
        absent=("gone.txt",),
        contains={"kept.txt": ("milk", "bread")},
        asked=False,
    )
    task = Task("t", "r", {}, checks)

    problems = check_outcome(task, home, "It is 1,099,511,627,776.", asked=True)

    assert problems == ["kept.txt lacks 'bread'", "asked the user"]


async def test_run_task_checks_the_end_state() -> None:
    task = Task(
        "trash",
        "Delete the zips in ~/Temp",
        {"Temp/a.zip": "x", "Temp/b.txt": "x"},
        Checks(absent=("Temp/a.zip",), exists=("Temp/b.txt", ".eval-trash/a.zip")),
    )
    model = FakeAdapter(
        [call("fs_trash", paths=["~/Temp/a.zip"]), Completion("Trashed a.zip.")], PROFILE
    )

    result = await run_task(task, [model], planner_model=None)

    assert result.passed, result.problems
    assert (result.reply, result.steps, result.planned) == ("Trashed a.zip.", 2, False)
    assert "tasks passed: 1/1 (100%)" in task_summary([result])


async def test_declined_confirmations_leave_files() -> None:
    task = Task("keep", "Delete ~/x.txt", {"x.txt": "x"}, Checks(exists=("x.txt",)), approve=False)
    model = FakeAdapter([call("fs_trash", paths=["~/x.txt"]), Completion("Left it.")], PROFILE)

    result = await run_task(task, [model], planner_model=None)

    assert result.passed, result.problems
