import shutil
from pathlib import Path
from typing import Any

import pytest

from arcee.context.store import Store
from arcee.core.policy import Policy, Tier
from arcee.tools.apps import AppList, AppOpen, ListArgs, OpenAppArgs, installed_apps
from arcee.tools.base import ToolContext
from arcee.tools.control import Note, NoteArgs, Speak, SpeakArgs, Wait, WaitArgs
from arcee.tools.files import FsCopy, FsMove, FsOpen, FsWrite, MoveArgs, OpenArgs, WriteArgs
from arcee.tools.shell import ScriptArgs, ShellScript, bwrap_works


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "a.txt").write_text("alpha")
    (home / "Documents" / "sub").mkdir()
    (home / "Documents" / "sub" / "b.txt").write_text("beta")
    return ToolContext(task_id="t_1", home=home)


class TestWrite:
    async def test_create_append_overwrite(self, ctx: ToolContext) -> None:
        write = FsWrite()
        target = ctx.home / "Arcee" / "notes.md"

        created = await write.run(WriteArgs(path="~/Arcee/notes.md", content="one\n"), ctx)
        again = await write.run(WriteArgs(path="~/Arcee/notes.md", content="x"), ctx)
        await write.run(WriteArgs(path="~/Arcee/notes.md", content="two\n", mode="append"), ctx)

        assert created.data == {"path": "~/Arcee/notes.md", "mode": "create", "chars": 4}
        assert again.error == "~/Arcee/notes.md already exists; use overwrite or append"
        assert target.read_text() == "one\ntwo\n"
        await write.run(WriteArgs(path="~/Arcee/notes.md", content="new", mode="overwrite"), ctx)
        assert target.read_text() == "new"

    @pytest.mark.parametrize(
        ("path", "error"),
        [
            ("/etc/arcee.conf", "only files inside your home folder can be written"),
            ("~/../outside.txt", "only files inside your home folder can be written"),
            ("~/Documents", "~/Documents is a folder"),
        ],
    )
    async def test_refuses_outside_home_and_folders(
        self, ctx: ToolContext, path: str, error: str
    ) -> None:
        result = await FsWrite().run(WriteArgs(path=path, content="x", mode="overwrite"), ctx)

        assert result.error == error

    def test_workspace_writes_run_without_asking(self, ctx: ToolContext) -> None:
        policy = Policy(home=ctx.home)
        write = FsWrite()

        assert write.tier_for(WriteArgs(path="~/Arcee/x.md", content="x"), policy) is Tier.AUTO
        assert write.tier_for(WriteArgs(path="~/notes.md", content="x"), policy) is Tier.CONFIRM
        escape = WriteArgs(path="~/Arcee/../notes.md", content="x")
        assert write.tier_for(escape, policy) is Tier.CONFIRM

    def test_confirmation_shows_mode_size_and_first_line(self) -> None:
        args = WriteArgs(path="~/todo.md", content="# Todo\n- milk", mode="append")

        assert FsWrite().sentence(args) == "Append to ~/todo.md (13 characters)?"
        assert FsWrite().preview(args) == ("~/todo.md", "starts: # Todo")


class TestMoveAndCopy:
    async def test_rename_and_move_into_folder(self, ctx: ToolContext) -> None:
        moved = await FsMove().run(MoveArgs(src="~/Documents/a.txt", dst="~/Documents/sub"), ctx)
        renamed = await FsMove().run(
            MoveArgs(src="~/Documents/sub/a.txt", dst="~/Documents/renamed.txt"), ctx
        )

        assert moved.data == {"from": "~/Documents/a.txt", "to": "~/Documents/sub/a.txt"}
        assert renamed.status == "ok"
        assert (ctx.home / "Documents" / "renamed.txt").read_text() == "alpha"

    @pytest.mark.parametrize(
        ("src", "dst", "error"),
        [
            ("~/Documents/a.txt", "~/Documents/sub/b.txt", "~/Documents/sub/b.txt already exists"),
            ("~/Documents/nope.txt", "~/x", "~/Documents/nope.txt does not exist"),
            ("~/Documents/a.txt", "/tmp/a.txt", "both paths must be inside your home folder"),
            ("~/Documents", "~/Documents/sub/inner", "cannot put a folder inside itself"),
        ],
    )
    async def test_refuses_overwrites_escapes_and_loops(
        self, ctx: ToolContext, src: str, dst: str, error: str
    ) -> None:
        result = await FsMove().run(MoveArgs(src=src, dst=dst), ctx)

        assert result.error is not None
        assert result.error.startswith(error)
        assert (ctx.home / "Documents" / "a.txt").exists()

    async def test_copies_files_and_folders(self, ctx: ToolContext) -> None:
        await FsCopy().run(MoveArgs(src="~/Documents/a.txt", dst="~/a-copy.txt"), ctx)
        await FsCopy().run(MoveArgs(src="~/Documents/sub", dst="~/sub-copy"), ctx)

        assert (ctx.home / "a-copy.txt").read_text() == "alpha"
        assert (ctx.home / "sub-copy" / "b.txt").read_text() == "beta"
        assert (ctx.home / "Documents" / "a.txt").exists()

    def test_both_ask_first(self) -> None:
        args = MoveArgs(src="~/a", dst="~/b")

        assert FsMove().tier_for(args, Policy()) is Tier.CONFIRM
        assert FsCopy().sentence(args) == "Copy ~/a to ~/b?"


class TestOpen:
    async def test_opens_with_the_default_app(self, ctx: ToolContext) -> None:
        result = await FsOpen(command=("true",)).run(OpenArgs(path="~/Documents/a.txt"), ctx)

        assert result.summary == "Opened ~/Documents/a.txt"

    async def test_reports_failures(self, ctx: ToolContext) -> None:
        missing = await FsOpen(command=("true",)).run(OpenArgs(path="~/none.pdf"), ctx)
        failing = await FsOpen(command=("false",)).run(OpenArgs(path="~/Documents/a.txt"), ctx)

        assert missing.error == "~/none.pdf does not exist"
        assert failing.status == "error"


def desktop(folder: Path, name: str, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(f"[Desktop Entry]\nType=Application\n{body}\n")


@pytest.fixture
def app_dirs(tmp_path: Path) -> list[Path]:
    user, system = tmp_path / "user", tmp_path / "system"
    desktop(system, "org.gnome.Calculator.desktop", "Name=Calculator\nKeywords=math;sum;")
    desktop(system, "firefox_firefox.desktop", "Name=Firefox\nComment=Browse the web")
    desktop(system, "hidden.desktop", "Name=Helper\nNoDisplay=true")
    desktop(system, "org.gnome.TextEditor.desktop", "Name=Text Editor")
    desktop(user, "org.gnome.TextEditor.desktop", "Name=My Editor")  # the user's copy wins
    return [user, system]


class TestApps:
    def test_reads_visible_apps_with_user_overrides(self, app_dirs: list[Path]) -> None:
        names = [app.name for app in installed_apps(app_dirs)]

        assert names == ["Calculator", "Firefox", "My Editor"]

    async def test_list_matches_name_keywords_and_purpose(
        self, app_dirs: list[Path], ctx: ToolContext
    ) -> None:
        tool = AppList(dirs=app_dirs)

        math = await tool.run(ListArgs(query="math"), ctx)
        web = await tool.run(ListArgs(query="web"), ctx)

        assert math.data is not None
        assert web.data is not None
        assert [a["name"] for a in math.data["apps"]] == ["Calculator"]
        assert [a["id"] for a in web.data["apps"]] == ["firefox_firefox.desktop"]

    async def test_open_launches_the_best_match(
        self, app_dirs: list[Path], ctx: ToolContext
    ) -> None:
        tool = AppOpen(dirs=app_dirs, command=("true",), windows=Windows([]))

        opened = await tool.run(OpenAppArgs(name="firefox"), ctx)
        unknown = await tool.run(OpenAppArgs(name="photoshop"), ctx)

        assert opened.data == {"opened": "Firefox", "id": "firefox_firefox.desktop"}
        assert unknown.error == "no installed app matches 'photoshop'; try app.list"

    async def test_an_open_app_is_brought_forward_not_started_again(
        self, app_dirs: list[Path], ctx: ToolContext
    ) -> None:
        windows = Windows([{"id": 7, "app_id": "firefox_firefox", "title": "Firefox"}])
        tool = AppOpen(dirs=app_dirs, command=("false",), windows=windows)

        opened = await tool.run(OpenAppArgs(name="firefox"), ctx)
        with_file = await tool.run(OpenAppArgs(name="firefox", args=["a.html"]), ctx)

        assert opened.summary == "Switched to the open Firefox window"
        assert windows.focused == [7]
        assert with_file.status == "error"  # files still go through a launch ("false" fails)


class Windows:
    def __init__(self, open_windows: list[dict[str, Any]]) -> None:
        self.open_windows = open_windows
        self.focused: list[int] = []

    def windows(self) -> list[dict[str, Any]]:
        return self.open_windows

    def focus_window(self, window_id: int) -> bool:
        self.focused.append(window_id)
        return True


class TestScript:
    async def test_runs_code_and_reports_output(self, ctx: ToolContext) -> None:
        tool = ShellScript(limits=False, sandbox=False)

        ok = await tool.run(ScriptArgs(language="python", code="print(6 * 7)"), ctx)
        bad = await tool.run(ScriptArgs(language="bash", code="echo oops >&2; exit 3"), ctx)

        assert ok.data is not None
        assert (ok.data["stdout"], ok.data["sandboxed"]) == ("42\n", False)
        assert (bad.status, bad.error) == ("error", "oops")

    def test_confirmation_warns_about_network_and_missing_isolation(self) -> None:
        args = ScriptArgs(language="bash", code="curl x", network=True)

        assert ShellScript(sandbox=True).sentence(args) == (
            "Run this bash script with internet access?"
        )
        assert "Isolation is off" in ShellScript(sandbox=False).sentence(args)
        assert ShellScript().tier_for(args, Policy()) is Tier.CONFIRM

    @pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs bubblewrap")
    async def test_sandbox_keeps_home_read_only_and_offline(self, ctx: ToolContext) -> None:
        if not await bwrap_works():
            pytest.skip("bubblewrap cannot create namespaces here")
        target = ctx.home / "escape.txt"
        code = (
            f"import socket\ntry:\n    open({str(target)!r}, 'w')\n    print('wrote')\n"
            "except OSError:\n    print('read-only')\n"
            "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=2)\n    print('online')\n"
            "except OSError:\n    print('offline')\n"
        )

        result = await ShellScript(limits=False).run(ScriptArgs(language="python", code=code), ctx)

        assert result.data is not None
        assert result.data["stdout"].split() == ["read-only", "offline"]
        assert not target.exists()


class TestControl:
    async def test_wait_and_speak(self, ctx: ToolContext) -> None:
        waited = await Wait().run(WaitArgs(seconds=0.01), ctx)
        said = await Speak().run(SpeakArgs(text="Halfway there"), ctx)

        assert (waited.summary, said.summary) == ("Waited 0.01s", "Said: Halfway there")

    async def test_note_saves_to_the_store(self, tmp_path: Path, ctx: ToolContext) -> None:
        store = Store(tmp_path / "data")
        with_notes = ToolContext(task_id="t_1", home=ctx.home, notes=store)

        saved = await Note().run(NoteArgs(text="Work folder is ~/code", importance=0.8), with_notes)
        missing = await Note().run(NoteArgs(text="x"), ctx)

        [(note_id, text, importance)] = await store.memories()
        assert (saved.summary, text, importance) == (
            "Remembered: Work folder is ~/code",
            "Work folder is ~/code",
            0.8,
        )
        assert saved.data == {"id": note_id}
        assert missing.error == "memory is not available in this session"
        store.close()
