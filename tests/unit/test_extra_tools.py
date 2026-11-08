"""Installing apps, a visible terminal, undoing a request, waiting for something."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from arcee.context.store import Store
from arcee.core.executor import ActionRecord
from arcee.core.policy import Policy, Tier
from arcee.core.protocol import ExecutorResult
from arcee.core.toolsets import filters_for
from arcee.models.base import Message
from arcee.tools.base import ToolContext
from arcee.tools.commands import Done
from arcee.tools.extra import (
    AppInstall,
    InstallArgs,
    TaskUndo,
    TerminalArgs,
    TerminalRun,
    UndoArgs,
    WaitFor,
    WaitForArgs,
    best_app,
    flathub_matches,
)


class Commands:
    def __init__(self, replies: dict[str, Done] | None = None, has: Sequence[str] = ()) -> None:
        self.replies = replies or {}
        self.ran: list[list[str]] = []
        self.programs = set(has)

    async def run(
        self,
        argv: Sequence[str],
        *,
        limit_s: float = 15,
        stdin: bytes | None = None,
        quiet: bool = False,
    ) -> Done:
        self.ran.append(list(argv))
        line = " ".join(argv)
        best = max((k for k in self.replies if line.startswith(k)), key=len, default=None)
        return self.replies[best] if best else Done(0, "", "")

    def has(self, program: str) -> bool:
        return program in self.programs


SEARCH = (
    "org.videolan.VLC\tVLC\tVLC media player, the open-source multimedia framework\n"
    "com.example.VlcRemote\tVLC Remote\tControl VLC\n"
)


def test_the_app_named_is_picked_from_flathub() -> None:
    rows = flathub_matches(SEARCH)
    assert best_app(rows, "vlc") == rows[0]
    assert best_app(rows, "vlc remote") == rows[1]


async def test_an_app_installs_from_flathub_in_the_background(tmp_path: Path) -> None:
    commands = Commands(
        {"flatpak search": Done(0, SEARCH, ""), "flatpak info": Done(1, "", "not installed")},
        has=["flatpak"],
    )
    tool = AppInstall(commands)
    assert tool.tier_for(InstallArgs(name="VLC"), Policy()) is Tier.CONFIRM
    result = await tool.run(InstallArgs(name="VLC"), ToolContext("t", tmp_path))
    script = commands.ran[-1][-1]
    assert commands.ran[-1][:4] == [
        "systemd-run",
        "--user",
        "--collect",
        "--unit=arcee-install-org-videolan-VLC",
    ]
    assert "flatpak install --user --noninteractive -y flathub org.videolan.VLC" in script
    assert "sudo" not in script
    assert result.summary.startswith("Installing VLC from Flathub in the background")


async def test_software_is_opened_when_asked(tmp_path: Path) -> None:
    commands = Commands(has=["flatpak"])
    tool = AppInstall(commands)
    args = InstallArgs(name="Discord", how="software")
    assert tool.tier_for(args, Policy()) is Tier.AUTO
    result = await tool.run(args, ToolContext("t", tmp_path))
    assert commands.ran[-1][-1] == "--search=Discord"
    assert result.summary == "Opened Software on “Discord”: press Install there"


async def test_a_command_runs_in_a_terminal_the_user_sees(tmp_path: Path) -> None:
    (tmp_path / "site").mkdir()
    commands = Commands(has=["ptyxis"])
    tool = TerminalRun(commands)
    args = TerminalArgs(command="npm run dev", folder="~/site")
    assert tool.tier_for(args, Policy()) is Tier.CONFIRM
    assert tool.sentence(args) == "Run `npm run dev` in ~/site, in a terminal?"
    await tool.run(args, ToolContext("t", tmp_path))
    argv = commands.ran[-1]
    assert argv[:7] == [
        "systemd-run",
        "--user",
        "--collect",
        "ptyxis",
        "--new-window",
        "-d",
        str(tmp_path / "site"),
    ]
    assert argv[-1].startswith("npm run dev; echo;")


async def record(
    store: Store, task_id: str, n: int, tool: str, args: dict[str, Any], data: dict[str, Any]
) -> None:
    result = ExecutorResult(
        n=n, tool=tool, status="ok", summary=f"{tool} done", data=data, duration_ms=1
    )
    await store.record_action(
        ActionRecord(
            ts=1.0, task_id=task_id, args=args, tier=Tier.AUTO, approved_by="user", result=result
        )
    )


async def test_a_request_is_looked_at_then_undone(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "cv.pdf").write_text("cv")  # where fs.move put it
    (home / "notes-copy.md").write_text("n")  # what fs.copy made
    task_id = await store.start_task("tidy my files")
    await record(
        store,
        task_id,
        1,
        "fs.move",
        {"src": "~/cv.pdf", "dst": "~/Documents"},
        {"from": "~/cv.pdf", "to": "~/Documents/cv.pdf"},
    )
    await record(store, task_id, 2, "fs.copy", {}, {"from": "~/notes.md", "to": "~/notes-copy.md"})
    await record(store, task_id, 3, "fs.trash", {}, {"trashed": ["~/old.txt"], "count": 1})
    await record(store, task_id, 4, "mail.send", {}, {})
    await store.finish_task(task_id, status="done", reply="Tidied", steps=4, model_calls=2)
    commands = Commands({"gio trash --list": Done(0, f"trash:///old.txt\t{home}/old.txt\n", "")})
    tool = TaskUndo(store, commands)  # pyright: ignore[reportArgumentType]
    ctx = ToolContext("t_now", home)

    look = await tool.run(UndoArgs(), ctx)
    assert tool.tier_for(UndoArgs(), Policy()) is Tier.AUTO
    assert tool.tier_for(UndoArgs(do_it=True), Policy()) is Tier.CONFIRM
    assert look.data is not None
    assert look.data["would_undo"] == [
        "restore ~/old.txt from the trash",
        "move ~/notes-copy.md (made by it) to the trash",
        "move ~/Documents/cv.pdf back to ~/cv.pdf",
    ]
    assert look.data["cannot_undo"] == ["mail.send: mail.send done"]
    assert (home / "Documents" / "cv.pdf").exists()  # nothing done yet

    done = await tool.run(UndoArgs(do_it=True), ctx)
    assert done.summary == "Undid 3 of 3 changes"
    assert (home / "cv.pdf").read_text() == "cv"
    assert ["gio", "trash", "--", str(home / "notes-copy.md")] in commands.ran
    assert ["gio", "trash", "--restore", "trash:///old.txt"] in commands.ran
    store.close()


async def test_nothing_to_undo_says_so(tmp_path: Path) -> None:
    store = Store(tmp_path)
    task_id = await store.start_task("what time is it")
    await store.finish_task(task_id, status="done", reply="3 pm", steps=0, model_calls=1)
    result = await TaskUndo(store, Commands()).run(UndoArgs(), ToolContext("t_now", tmp_path))  # pyright: ignore[reportArgumentType]
    assert result.error == "no recent request changed files in a way that can be undone"
    store.close()


class Quick(WaitFor):
    POLL_S = 0.02
    STEADY_S = 0.1


async def test_waiting_ends_when_a_download_stops_growing(tmp_path: Path) -> None:
    target = tmp_path / "report.pdf"

    async def download() -> None:
        await asyncio.sleep(0.05)
        target.write_bytes(b"x" * 10)
        await asyncio.sleep(0.05)
        target.write_bytes(b"x" * 20)

    writing = asyncio.ensure_future(download())
    result = await Quick().run(
        WaitForArgs(event="file_done", target="report.pdf", timeout_s=5), ToolContext("t", tmp_path)
    )
    await writing
    assert result.data is not None
    assert result.data["happened"] is True
    assert result.summary.endswith("~/report.pdf is complete (20 bytes)")


async def test_waiting_for_a_window_and_giving_up_in_time(tmp_path: Path) -> None:
    shown: list[dict[str, Any]] = []
    tool = Quick(windows=lambda: shown)
    asyncio.get_running_loop().call_later(
        0.05,
        lambda: shown.append({"id": 1, "app_id": "org.gnome.Calculator", "title": "Calculator"}),
    )
    opened = await tool.run(
        WaitForArgs(event="window_opens", target="Calculator", timeout_s=5),
        ToolContext("t", tmp_path),
    )
    never = await tool.run(
        WaitForArgs(event="window_opens", target="Zoom", timeout_s=1), ToolContext("t", tmp_path)
    )
    assert opened.summary.endswith("the window “Calculator” is open")
    assert never.data is not None
    assert never.data["happened"] is False


@pytest.mark.parametrize(
    ("said", "wanted"),
    [
        ("Install VLC", "app.*"),
        ("Start the dev server for my website in a terminal", "terminal.*"),
        ("Undo that", "task.*"),
        ("Put the files back", "task.*"),
    ],
)
def test_these_requests_offer_their_tools(said: str, wanted: str) -> None:
    assert wanted in filters_for([Message("user", said)])
