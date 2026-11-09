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


def test_stubs_look_real_and_run_nothing(tmp_path: Path) -> None:
    from arcee.core.policy import Policy, Tier
    from arcee.eval.tasks import task_registry

    registry = task_registry(tmp_path, stubs={"travel.search": {"summary": "2 trains"}})
    whatsapp = registry.get("whatsapp.send")
    travel = registry.get("travel.search")
    files = registry.get("fs.list")

    assert whatsapp is not None
    assert travel is not None
    assert files is not None
    assert len(list(registry)) > 100  # every built-in tool is offered
    args = whatsapp.args_model(to="Mom", text="hi")
    assert whatsapp.tier_for(args, Policy()) is Tier.CONFIRM  # its own tier
    assert type(files).__module__ == "arcee.tools.files"  # real


async def test_stubbed_tools_answer_from_the_task(tmp_path: Path) -> None:
    from arcee.eval.tasks import task_registry
    from arcee.tools.base import ToolContext

    registry = task_registry(
        tmp_path,
        stubs={"travel.search": {"summary": "2 trains", "data": {"trains": ["12014"]}}},
    )
    ctx = ToolContext("t", tmp_path)
    travel, volume = registry.get("travel.search"), registry.get("settings.volume")
    assert travel is not None
    assert volume is not None

    canned = await travel.run(travel.args_model(origin="LDH", destination="NDLS"), ctx)
    done = await volume.run(volume.args_model(action="set", percent=30), ctx)

    assert (canned.summary, canned.data) == ("2 trains", {"trains": ["12014"]})
    assert done.summary == "Done: settings.volume"
    assert done.data is not None
    assert done.data["percent"] == 30


def test_calls_are_checked() -> None:
    from arcee.eval.tasks import Call, check_calls

    task = Task(
        "t",
        "r",
        {},
        Checks(
            used=("travel.search",),
            used_any=("mail.search", "mail.list"),
            not_used=("web.search",),
            args={"travel.search": {"destination": "delhi"}},
        ),
    )
    calls = [
        Call("web.search", {"query": "trains"}, "ok"),
        Call("travel.search", {"origin": "Ludhiana", "destination": "Shimla"}, "ok"),
    ]

    assert check_calls(task, calls) == [
        "called none of mail.search, mail.list",
        "called web.search",
        "travel.search destination=['Shimla'], wanted 'delhi'",
    ]


async def test_a_task_through_a_stubbed_tool() -> None:
    task = Task(
        "trains",
        "Find trains from Ludhiana to Delhi tomorrow",
        {},
        Checks(reply_has=("12014",), used=("travel.search",), not_used=("web.search",)),
        stubs={"travel.search": {"summary": "1 train", "data": {"trains": ["12014 Shatabdi"]}}},
    )
    model = FakeAdapter(
        [
            call("travel_search", origin="Ludhiana", destination="Delhi"),
            Completion("The 12014 Shatabdi leaves at 07:02."),
        ],
        PROFILE,
    )

    result = await run_task(task, [model], planner_model=None)

    assert result.passed, result.problems
    assert [c.tool for c in result.calls] == ["travel.search"]
