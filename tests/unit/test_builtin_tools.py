import os
import shutil
from pathlib import Path

import pytest

from arcee.core.policy import Policy, Tier
from arcee.tools.base import ToolContext
from arcee.tools.builtin import builtin_registry
from arcee.tools.control import Done, DoneArgs
from arcee.tools.files import (
    FsList,
    FsRead,
    FsSearch,
    FsTrash,
    ListArgs,
    ReadArgs,
    SearchArgs,
    TrashArgs,
)
from arcee.tools.shell import RunArgs, ShellRun


@pytest.fixture
def home(tmp_path: Path) -> Path:
    docs = tmp_path / "Documents"
    docs.mkdir()
    for name in ("report.pdf", "cv.PDF", "thesis.pdf", "photo.jpg"):
        (docs / name).write_text(f"content of {name}")
    (docs / "notes.txt").write_text("standup moved to Thursday 10:30")
    (docs / "projects").mkdir()
    (docs / "projects" / "resume-2026.docx").write_text("x")
    (docs / ".hidden").write_text("secret")
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2binary")
    return tmp_path


@pytest.fixture
def ctx(home: Path) -> ToolContext:
    return ToolContext(task_id="t_1", home=home)


class TestList:
    async def test_lists_folders_first_with_counts(self, ctx: ToolContext) -> None:
        result = await FsList().run(ListArgs(path="~/Documents"), ctx)

        assert result.status == "ok"
        assert result.data is not None
        names = [e["name"] for e in result.data["entries"]]
        assert names == ["projects", "cv.PDF", "notes.txt", "photo.jpg", "report.pdf", "thesis.pdf"]
        assert result.data["count"] == 6
        assert result.data["entries"][0]["type"] == "folder"
        assert result.data["entries"][1]["size"].endswith(" B")

    async def test_glob_counts_matches_case_insensitively(self, ctx: ToolContext) -> None:
        result = await FsList().run(ListArgs(path="Documents", glob="*.pdf"), ctx)

        assert result.data is not None
        assert result.data["count"] == 3
        assert result.summary == "Listed ~/Documents: 3 entries matching *.pdf"

    async def test_largest_first_with_limit(self, ctx: ToolContext, home: Path) -> None:
        (home / "Documents" / "big.iso").write_bytes(b"x" * 5000)
        (home / "Documents" / "mid.zip").write_bytes(b"x" * 2000)

        result = await FsList().run(ListArgs(path="~/Documents", sort="largest", limit=2), ctx)

        assert result.data is not None
        assert [e["name"] for e in result.data["entries"]] == ["big.iso", "mid.zip"]
        assert result.data["entries"][0]["size"] == "4.9 KB"
        assert (result.data["count"], result.data["shown"]) == (8, 2)
        assert result.summary == "Listed ~/Documents: 8 entries, largest first: big.iso"

    async def test_newest_first(self, ctx: ToolContext, home: Path) -> None:
        os.utime(home / "Documents" / "notes.txt", (0, 4_000_000_000))

        result = await FsList().run(ListArgs(path="~/Documents", sort="newest", limit=1), ctx)

        assert result.data is not None
        assert result.data["entries"][0]["name"] == "notes.txt"
        assert "_mtime" not in result.data["entries"][0]

    async def test_hidden_entries_on_request(self, ctx: ToolContext) -> None:
        result = await FsList().run(ListArgs(path="~/Documents", hidden=True), ctx)

        assert result.data is not None
        assert ".hidden" in [e["name"] for e in result.data["entries"]]

    async def test_missing_folder_fails(self, ctx: ToolContext) -> None:
        result = await FsList().run(ListArgs(path="~/Nope"), ctx)

        assert (result.status, result.error) == ("error", "~/Nope is not a folder")


class TestRead:
    async def test_reads_text(self, ctx: ToolContext) -> None:
        result = await FsRead().run(ReadArgs(path="~/Documents/notes.txt"), ctx)

        assert result.data == {
            "path": "~/Documents/notes.txt",
            "content": "standup moved to Thursday 10:30",
        }

    async def test_truncates_long_files(self, ctx: ToolContext) -> None:
        result = await FsRead().run(ReadArgs(path="~/Documents/notes.txt", max_bytes=7), ctx)

        assert result.data is not None
        assert result.data["content"] == "standup"
        assert result.data["truncated"] == "first 7 of 31 bytes"

    async def test_refuses_binary_files(self, ctx: ToolContext) -> None:
        result = await FsRead().run(ReadArgs(path="~/blob.bin"), ctx)

        assert result.error == "~/blob.bin is a binary file"


class TestSearch:
    async def test_by_name_substring_skips_hidden(self, ctx: ToolContext) -> None:
        result = await FsSearch().run(SearchArgs(query="Resume"), ctx)

        assert result.data is not None
        assert result.data["matches"] == ["~/Documents/projects/resume-2026.docx"]
        assert result.data["count"] == 1

    async def test_counts_matches_per_folder(self, ctx: ToolContext, home: Path) -> None:
        (home / "Documents" / "projects" / "cv-old.pdf").write_text("x")

        result = await FsSearch().run(SearchArgs(query="*.pdf"), ctx)

        assert result.data is not None
        assert result.data["by_folder"] == {"~/Documents": 3, "~/Documents/projects": 1}

    async def test_by_name_pattern(self, ctx: ToolContext) -> None:
        result = await FsSearch().run(SearchArgs(query="*.pdf", root="~/Documents"), ctx)

        assert result.data is not None
        assert sorted(result.data["matches"]) == [
            "~/Documents/cv.PDF",
            "~/Documents/report.pdf",
            "~/Documents/thesis.pdf",
        ]

    async def test_by_content_ignores_binary(self, ctx: ToolContext) -> None:
        result = await FsSearch().run(SearchArgs(query="thursday", by="content"), ctx)

        assert result.data is not None
        assert result.data["matches"] == ["~/Documents/notes.txt"]

    async def test_no_matches_is_still_ok(self, ctx: ToolContext) -> None:
        result = await FsSearch().run(SearchArgs(query="tax"), ctx)

        assert (result.status, result.data and result.data["count"]) == ("ok", 0)


def trash(home: Path) -> FsTrash:
    """gio refuses /tmp (a system mount), so tests move files into a stand-in trash."""
    bin_ = home / ".trash-for-tests"
    bin_.mkdir(exist_ok=True)
    return FsTrash(command=("mv", "-t", str(bin_), "--"))


class TestTrash:
    async def test_trashes_only_existing_files_inside_home(
        self, ctx: ToolContext, home: Path
    ) -> None:
        args = TrashArgs(paths=["~/Documents/photo.jpg", "~/Documents/missing.txt", "/etc/hosts"])

        result = await trash(home).run(args, ctx)

        assert result.status == "ok"
        assert result.data is not None
        assert result.data["trashed"] == ["~/Documents/photo.jpg"]
        assert result.data["not_trashed"] == {
            "~/Documents/missing.txt": "does not exist",
            "/etc/hosts": "only files inside your home folder can be trashed",
        }
        assert not (home / "Documents" / "photo.jpg").exists()
        assert (home / ".trash-for-tests" / "photo.jpg").exists()

    async def test_refuses_home_itself_and_escapes(self, ctx: ToolContext, home: Path) -> None:
        result = await trash(home).run(TrashArgs(paths=["~", "~/Documents/../../etc"]), ctx)

        assert result.status == "error"
        assert result.data is not None
        assert result.data["count"] == 0

    def test_needs_confirmation_with_targets(self) -> None:
        args = TrashArgs(paths=["~/a.deb", "~/b.deb"])

        assert FsTrash().tier_for(args, Policy()) is Tier.CONFIRM
        assert FsTrash().sentence(args) == "Move 2 item(s) to the trash?"
        assert FsTrash().preview(args) == ("~/a.deb", "~/b.deb")


class TestShell:
    async def test_runs_in_home_with_output(self, ctx: ToolContext, home: Path) -> None:
        result = await ShellRun(limits=False).run(RunArgs(cmd="ls Documents | wc -l"), ctx)

        assert result.status == "ok"
        assert result.data == {"exit_code": 0, "stdout": "6\n", "stderr": ""}
        assert result.summary == "`ls Documents | wc -l` exited 0: 6"

    async def test_no_match_is_an_answer_not_an_error(self, ctx: ToolContext) -> None:
        result = await ShellRun(limits=False).run(RunArgs(cmd="grep -r zzz-none Documents"), ctx)

        assert (result.status, result.summary) == (
            "ok",
            "`grep -r zzz-none Documents` found nothing",
        )
        assert result.data is not None
        assert result.data["note"] == "no output and exit code 1: nothing matched"

    async def test_failure_reports_stderr(self, ctx: ToolContext) -> None:
        result = await ShellRun(limits=False).run(RunArgs(cmd="ls nope"), ctx)

        assert result.status == "error"
        assert result.error is not None
        assert "No such file" in result.error

    async def test_hides_secret_environment(
        self, ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "leak")
        monkeypatch.setenv("ARCEE_VISIBLE", "fine")

        result = await ShellRun(limits=False).run(
            RunArgs(cmd="echo ${GEMINI_API_KEY:-none} $ARCEE_VISIBLE $HOME"), ctx
        )

        assert result.data is not None
        assert result.data["stdout"] == f"none fine {ctx.home}\n"

    @pytest.mark.skipif(
        shutil.which("systemd-run") is None or "DBUS_SESSION_BUS_ADDRESS" not in os.environ,
        reason="needs a systemd user session",
    )
    async def test_runs_under_resource_limits(self, ctx: ToolContext) -> None:
        result = await ShellRun().run(RunArgs(cmd="echo limited"), ctx)

        assert result.data is not None
        assert result.data["stdout"] == "limited\n"

    @pytest.mark.parametrize(
        ("cmd", "tier"),
        [("du -sh ~", Tier.AUTO), ("mv a b", Tier.CONFIRM), ("sudo ls", Tier.NEVER)],
    )
    def test_tier_follows_the_policy(self, cmd: str, tier: Tier) -> None:
        assert ShellRun().tier_for(RunArgs(cmd=cmd), Policy()) is tier

    def test_refusal_names_the_rule(self) -> None:
        reason = ShellRun().refusal(RunArgs(cmd="rm -rf ~"), Policy())

        assert reason == (
            "recursive delete; use fs.trash with paths ['~'] instead; this command will never run"
        )


async def test_control_tools_are_not_run(ctx: ToolContext) -> None:
    with pytest.raises(RuntimeError, match="handled by the orchestrator"):
        await Done().run(DoneArgs(reply="hi"), ctx)


def test_builtin_registry_has_cards_for_every_tool() -> None:
    tools = builtin_registry(shell_limits=False)

    assert [tool.name for tool in tools] == [
        "fs.list",
        "fs.read",
        "fs.search",
        "fs.trash",
        "shell.run",
        "control.done",
        "control.ask_user",
        "control.replan",
    ]
    assert all(tool.usage.when for tool in tools)
