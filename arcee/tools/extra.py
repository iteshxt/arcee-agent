"""Installing apps, a command in a terminal the user sees, undoing a task, and waiting for
something to happen (APP-012, DEV-008, CHAT-007, AGENT-001).

- `app.install`: from Flathub into the user's own Flatpak installation (no password, as
  GNOME Software does for a user install), in the background, with a notification when
  it is ready; or GNOME Software open on the app, for the user to press Install.
- `terminal.run`: a command left running in a terminal tab the user can watch and stop (a
  dev server), where shell.run is for short commands whose output Arcee reads.
- `task.undo`: what a finished request did to files, reversed: moves moved back, copies
  and new files to the trash, trashed files restored. Asked first, after a look.
- `control.wait_for`: waits until a file appears or stops growing, a window opens or
  closes, or a program exits, and returns as soon as it does (or at the timeout, saying
  so), instead of a guessed control.wait.
"""

import asyncio
import os
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, Protocol, cast

from pydantic import Field

from arcee.core.policy import Policy, Tier
from arcee.jobs import JOBS, Jobs
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.commands import Commands, SystemCommands
from arcee.tools.files import display, resolve

# --- app.install ------------------------------------------------------------------------------


def flathub_matches(text: str) -> list[tuple[str, str, str]]:
    """flatpak search's tab-separated (application, name, description) lines."""
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and "." in parts[0]:
            rows.append(
                (parts[0].strip(), parts[1].strip(), parts[2].strip() if len(parts) > 2 else "")
            )
    return rows


def best_app(rows: list[tuple[str, str, str]], wanted: str) -> tuple[str, str, str] | None:
    key = wanted.lower().strip()
    for row in rows:  # the app called exactly that, then one whose name starts so
        if row[1].lower() == key:
            return row
    for row in rows:
        if row[1].lower().startswith(key) or row[0].lower().endswith("." + key):
            return row
    return rows[0] if rows else None


class InstallArgs(ToolArgs):
    name: str = Field(min_length=2, max_length=60, description="The app: VLC, Discord, GIMP")
    how: Literal["flatpak", "software"] = Field(
        default="flatpak",
        description="flatpak: install it now from Flathub; software: open GNOME Software on it",
    )


class AppInstall(Tool[InstallArgs]):
    name = "app.install"
    description = "Install an app from Flathub for this user (no password), or open Software on it."
    usage = Usage(
        when='"Install VLC", "get me Discord".',
        avoid="apt or sudo in shell.run: Arcee never uses sudo.",
        mistakes="Installing something the user did not name: confirm the app's exact name.",
        example='app.install(name="VLC")',
    )
    args_model = InstallArgs
    timeout_s = 60

    def __init__(self, commands: Commands | None = None) -> None:
        self._commands = commands or SystemCommands()

    def tier_for(self, args: InstallArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM if args.how == "flatpak" else Tier.AUTO

    def sentence(self, args: InstallArgs) -> str:
        return f"Install {args.name} from Flathub?"

    async def run(self, args: InstallArgs, ctx: ToolContext) -> ToolResult:
        run = self._commands.run
        if args.how == "software" or not self._commands.has("flatpak"):
            await run(
                ["systemd-run", "--user", "--collect", "gnome-software", f"--search={args.name}"]
            )
            return ToolResult.ok(
                f"Opened Software on “{args.name}”: press Install there",
                data={"opened": "GNOME Software"},
            )
        found = await run(
            ["flatpak", "search", "--columns=application,name,description", args.name], limit_s=40
        )
        app = best_app(flathub_matches(found.out), args.name)
        if app is None:
            return ToolResult.fail(f"Flathub has no app called “{args.name}”; try how=software")
        app_id, name, about = app
        if (await run(["flatpak", "info", "--user", app_id])).ok:
            return ToolResult.ok(f"{name} is already installed", data={"app": app_id})
        script = (
            f"flatpak install --user --noninteractive -y flathub {shlex.quote(app_id)} && "
            f"notify-send -a Arcee -i {shlex.quote(app_id)} {shlex.quote(f'{name} is installed')} "
            "'Find it in the app grid.' || notify-send -a Arcee "
            f"{shlex.quote(f'{name} did not install')} 'Try it in Software.'"
        )
        unit = "arcee-install-" + re.sub(r"[^\w-]", "-", app_id)
        done = await run(
            ["systemd-run", "--user", "--collect", f"--unit={unit}", "sh", "-c", script]
        )
        if not done.ok:
            return ToolResult.fail(f"could not start the install: {done.why()}")
        return ToolResult.ok(
            f"Installing {name} from Flathub in the background; a notification says when "
            "it is ready",
            data={"app": app_id, "name": name, "about": about},
        )


# --- terminal.run -----------------------------------------------------------------------------


class TerminalArgs(ToolArgs):
    command: str = Field(min_length=1, max_length=2000, description="The command line to run")
    folder: str = Field(default="", description="Where to run it; default the home folder")


class TerminalRun(Tool[TerminalArgs]):
    name = "terminal.run"
    description = (
        "Run a command in a new terminal window the user sees and can stop (Ctrl+C): "
        "servers, watchers, long builds."
    )
    usage = Usage(
        when='"Start the dev server for my website", "run the tests where I can watch".',
        avoid="Short commands whose output you need: shell.run.",
        example='terminal.run(command="npm run dev", folder="~/code/site")',
    )
    args_model = TerminalArgs
    tier = Tier.CONFIRM
    risk = "medium"
    timeout_s = 15

    def __init__(self, commands: Commands | None = None) -> None:
        self._commands = commands or SystemCommands()

    def sentence(self, args: TerminalArgs) -> str:
        where = f" in {args.folder}" if args.folder else ""
        return f"Run `{args.command}`{where}, in a terminal?"

    async def run(self, args: TerminalArgs, ctx: ToolContext) -> ToolResult:
        folder = resolve(args.folder, ctx) if args.folder else ctx.home
        if not folder.is_dir():
            return ToolResult.fail(f"{args.folder} is not a folder")
        # The shell stays when the command ends, so its last words can be read.
        keep = f"{args.command}; echo; echo '[finished: exit '$?']'; exec bash"
        if self._commands.has("ptyxis"):
            argv = ["ptyxis", "--new-window", "-d", str(folder), "--", "bash", "-c", keep]
        elif self._commands.has("gnome-terminal"):
            argv = ["gnome-terminal", f"--working-directory={folder}", "--", "bash", "-c", keep]
        else:
            argv = ["xdg-terminal-exec", "bash", "-c", f"cd {shlex.quote(str(folder))}; {keep}"]
        done = await self._commands.run(["systemd-run", "--user", "--collect", *argv])
        if not done.ok:
            return ToolResult.fail(f"could not open a terminal: {done.why()}")
        return ToolResult.ok(
            f"Running `{args.command}` in a terminal in {display(folder, ctx)}",
            data={"command": args.command, "folder": display(folder, ctx)},
        )


# --- task.undo --------------------------------------------------------------------------------


class History(Protocol):
    async def recent(self, limit: int = 10) -> list[Any]: ...
    async def task(self, task_id: str) -> tuple[Any, list[Any]] | None: ...
    async def observation(self, task_id: str, n: int) -> dict[str, Any] | None: ...


Step = tuple[str, Callable[[], Awaitable[str]]]  # what it does, and doing it


class UndoArgs(ToolArgs):
    task_id: str = Field(
        default="", description="The request to undo (t_...); empty for the last one"
    )
    do_it: bool = Field(
        default=False, description="false: say what would be undone; true: undo it (asks)"
    )


class TaskUndo(Tool[UndoArgs]):
    name = "task.undo"
    description = (
        "Undo what an earlier request did to files: moves and renames put back, copies and "
        "new files to the trash, trashed files restored. First look (do_it=false), then do."
    )
    usage = Usage(
        when='"Undo that", "put the files back", "undo the last thing you did".',
        mistakes="Undoing without looking first: call with do_it=false, tell the user, then "
        "do_it=true.",
        example="task.undo()",
    )
    args_model = UndoArgs
    timeout_s = 120

    def __init__(self, history: History, commands: Commands | None = None) -> None:
        self._history = history
        self._commands = commands or SystemCommands()

    def tier_for(self, args: UndoArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM if args.do_it else Tier.AUTO

    def sentence(self, args: UndoArgs) -> str:
        return "Undo what that request did to your files (as just listed)?"

    async def _target(self, args: UndoArgs, ctx: ToolContext) -> str | None:
        if args.task_id:
            return args.task_id
        for row in await self._history.recent(15):
            if row.id == ctx.task_id:
                continue
            found = await self._history.task(row.id)
            if found and any(s.tool in _UNDOABLE and s.status == "ok" for s in found[1]):
                return str(row.id)
        return None

    async def run(self, args: UndoArgs, ctx: ToolContext) -> ToolResult:
        task_id = await self._target(args, ctx)
        if task_id is None:
            return ToolResult.fail("no recent request changed files in a way that can be undone")
        found = await self._history.task(task_id)
        if found is None:
            return ToolResult.fail(f"no request {task_id} in History")
        task, steps = found
        plan: list[Step] = []
        cannot: list[str] = []
        for step in reversed(steps):  # the last change first
            if step.status != "ok":
                continue
            seen = await self._history.observation(task_id, step.n)
            data: dict[str, Any] = (seen or {}).get("data") or {}
            plan_for = _UNDOABLE.get(step.tool)
            if plan_for is None:
                if step.tool in _CHANGES:
                    cannot.append(f"{step.tool}: {step.summary}")
                continue
            made = plan_for(self, step.args, data, ctx)
            if isinstance(made, str):
                cannot.append(made)
            else:
                plan.extend(made)
        info: dict[str, Any] = {
            "request": task.request,
            "task_id": task_id,
            "would_undo" if not args.do_it else "undone": [what for what, _ in plan],
        }
        if cannot:
            info["cannot_undo"] = cannot
        if not plan:
            return ToolResult.fail(f"nothing in “{task.request}” can be undone: {cannot}")
        if not args.do_it:
            return ToolResult.ok(
                f"{len(plan)} changes of “{task.request}” can be undone", data=info
            )
        problems: list[str] = []
        for what, do in plan:
            try:
                if why := await do():
                    problems.append(f"{what}: {why}")
            except OSError as exc:
                problems.append(f"{what}: {exc}")
        if problems:
            info["problems"] = problems
        return ToolResult.ok(f"Undid {len(plan) - len(problems)} of {len(plan)} changes", data=info)

    # Each: the steps that reverse one tool call, or why it cannot be.

    def _move(
        self, args: dict[str, Any], data: dict[str, Any], ctx: ToolContext
    ) -> list[Step] | str:
        if "from" not in data or "to" not in data:
            return f"a move of {args.get('src')} (not recorded fully)"
        src, dst = resolve(str(data["to"]), ctx), resolve(str(data["from"]), ctx)

        async def back() -> str:
            if not src.exists():
                return f"{display(src, ctx)} is no longer there"
            if dst.exists():
                return f"{display(dst, ctx)} exists again"
            dst.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(os.replace, src, dst)
            return ""

        return [(f"move {data['to']} back to {data['from']}", back)]

    def _made(self, paths: list[str], ctx: ToolContext) -> list[Step]:
        steps: list[Step] = []
        for shown in paths:
            path = resolve(shown, ctx)

            async def trash(path: Path = path) -> str:
                if not await asyncio.to_thread(path.exists):
                    return "already gone"
                done = await self._commands.run(["gio", "trash", "--", str(path)])
                return "" if done.ok else done.why()

            steps.append((f"move {shown} (made by it) to the trash", trash))
        return steps

    def _copy(
        self, args: dict[str, Any], data: dict[str, Any], ctx: ToolContext
    ) -> list[Step] | str:
        return self._made([str(data["to"])], ctx) if "to" in data else "a copy (not recorded)"

    def _write(
        self, args: dict[str, Any], data: dict[str, Any], ctx: ToolContext
    ) -> list[Step] | str:
        if data.get("mode") != "create":
            what = data.get("path", args.get("path"))
            return f"{data.get('mode', 'a change')} of {what} (no copy kept)"
        return self._made([str(data["path"])], ctx)

    def _created(
        self, args: dict[str, Any], data: dict[str, Any], ctx: ToolContext
    ) -> list[Step] | str:
        made = data.get("made") or ([data["path"]] if "path" in data else [])
        return self._made([str(p) for p in made], ctx) if made else "files made (not recorded)"

    def _trash(
        self, args: dict[str, Any], data: dict[str, Any], ctx: ToolContext
    ) -> list[Step] | str:
        steps: list[Step] = []
        trashed = cast(list[str], data.get("trashed") or [])
        for shown in trashed:
            original = resolve(shown, ctx)

            async def restore(original: Path = original) -> str:
                listing = await self._commands.run(["gio", "trash", "--list"])
                uri = next(
                    (
                        line.split("\t", 1)[0]
                        for line in listing.out.splitlines()
                        if line.endswith("\t" + str(original))
                    ),
                    None,
                )
                if uri is None:
                    return "no longer in the trash"
                done = await self._commands.run(["gio", "trash", "--restore", uri])
                return "" if done.ok else done.why()

            steps.append((f"restore {shown} from the trash", restore))
        return steps or "a trash (not recorded)"


_UNDOABLE: Final[dict[str, Callable[..., list[Step] | str]]] = {
    "fs.move": TaskUndo._move,  # pyright: ignore[reportPrivateUsage]
    "fs.copy": TaskUndo._copy,  # pyright: ignore[reportPrivateUsage]
    "fs.write": TaskUndo._write,  # pyright: ignore[reportPrivateUsage]
    "fs.trash": TaskUndo._trash,  # pyright: ignore[reportPrivateUsage]
    "doc.create": TaskUndo._created,  # pyright: ignore[reportPrivateUsage]
    "image.edit": TaskUndo._created,  # pyright: ignore[reportPrivateUsage]
}
# Changes it cannot reverse, said so.
_CHANGES: Final = frozenset(
    {
        "mail.send",
        "mail.update",
        "calendar.add",
        "calendar.update",
        "calendar.delete",
        "shell.run",
        "shell.script",
        "settings.default_app",
    }
)


# --- control.wait_for -------------------------------------------------------------------------


class WaitForArgs(ToolArgs):
    event: Literal[
        "file_exists", "file_done", "window_opens", "window_closes", "program_exits", "job_done"
    ]
    target: str = Field(
        min_length=1,
        description="The file's path, the window's app or title, the program's name, or the "
        "job id from shell.start",
    )
    timeout_s: int = Field(default=60, ge=1, le=600, description="Give up after this long")


class WaitFor(Tool[WaitForArgs]):
    name = "control.wait_for"
    description = (
        "Wait until something happens, and go on the moment it does: a file appears or stops "
        "growing (a download), a window opens or closes, a program exits, a background job "
        "(shell.start) ends."
    )
    usage = Usage(
        when="After starting something that takes a while: an app opening, a download, an "
        "export, a build.",
        avoid="Fixed pauses (control.wait) when there is something to wait for.",
        example='control.wait_for(event="file_done", target="~/Downloads/report.pdf")',
    )
    args_model = WaitForArgs
    read_only: ClassVar[bool] = False
    timeout_s = 620
    POLL_S: ClassVar[float] = 0.5
    STEADY_S: ClassVar[float] = 3.0  # a file "done" when its size has not changed this long

    def __init__(
        self,
        windows: Callable[[], list[dict[str, Any]]] | None = None,
        commands: Commands | None = None,
        jobs: Jobs | None = None,
    ) -> None:
        self._windows = windows
        self._commands = commands or SystemCommands()
        self._jobs = jobs or JOBS

    def _window_list(self) -> list[dict[str, Any]]:
        if self._windows is not None:
            return self._windows()
        from arcee.platform.shell import ShellBridge

        return ShellBridge().windows()

    async def _check(
        self, args: WaitForArgs, ctx: ToolContext, state: dict[str, Any]
    ) -> str | None:
        """What happened, or None while waiting."""
        if args.event == "job_done":
            job = self._jobs.get(args.target)
            if job is None:
                raise LookupError(f"there is no job {args.target!r} (job.check lists them)")
            if not job.done:
                return None
            job.seen = True
            return f"{job.id} {job.state()}; job.check(job={job.id!r}) shows its output"
        if args.event in ("file_exists", "file_done"):
            path = resolve(args.target, ctx)
            if not path.exists():
                return None
            if args.event == "file_exists":
                return f"{display(path, ctx)} is there"
            if path.name.endswith((".crdownload", ".part", ".download")):
                return None
            size = path.stat().st_size
            if size != state.get("size"):
                state["size"], state["since"] = size, time.monotonic()
                return None
            if time.monotonic() - state["since"] >= self.STEADY_S:
                return f"{display(path, ctx)} is complete ({size:,} bytes)"
            return None
        if args.event in ("window_opens", "window_closes"):
            from arcee.tools.windows import find_window

            open_now = find_window(args.target, await asyncio.to_thread(self._window_list))
            if args.event == "window_opens" and open_now is not None:
                return f"the window “{open_now.get('title')}” is open"
            if args.event == "window_closes" and open_now is None:
                return f"no window matches “{args.target}” any more"
            return None
        found = await self._commands.run(["pgrep", "-x", args.target[:15]])
        if not found.ok:
            return f"{args.target} is not running"
        return None

    async def run(self, args: WaitForArgs, ctx: ToolContext) -> ToolResult:
        started = time.monotonic()
        state: dict[str, Any] = {}
        while True:
            try:
                happened = await self._check(args, ctx, state)
            except Exception as exc:  # the shell gone, a file unreadable: say it, do not hang
                return ToolResult.fail(f"cannot watch for that: {exc}")
            waited = round(time.monotonic() - started, 1)
            if happened:
                return ToolResult.ok(
                    f"After {waited:g} s: {happened}", data={"waited_s": waited, "happened": True}
                )
            if waited >= args.timeout_s:
                return ToolResult.ok(
                    f"Not yet after {args.timeout_s} s: {args.event} {args.target}",
                    data={"waited_s": waited, "happened": False},
                )
            await asyncio.sleep(self.POLL_S)


def extra_tools(history: History | None = None) -> list[Tool[Any]]:
    tools: list[Tool[Any]] = [AppInstall(), TerminalRun(), WaitFor()]
    if history is not None:
        tools.append(TaskUndo(history))
    return tools
