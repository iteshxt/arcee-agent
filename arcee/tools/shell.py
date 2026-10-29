"""`shell.run` (spec sections 11 and 12): one command line, tiered by the policy.

Allowlisted read-only pipelines run without asking; anything else is confirmed
or refused by the gate before this tool sees it. Commands run under
`systemd-run` resource limits when available, with secret-looking environment
variables removed, and return exit code and trimmed output as data.
"""

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from arcee.core.policy import Policy, Tier
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.files import resolve

MAX_OUTPUT_CHARS: Final = 4000
LIMITS: Final = ("-p", "MemoryMax=2G", "-p", "CPUQuota=200%", "-p", "RuntimeMaxSec=120")
_SECRET_ENV: Final = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)


class RunArgs(ToolArgs):
    cmd: str = Field(min_length=1, description="One Linux command line, e.g. du -sh ~/Downloads")
    cwd: str = Field(default="~", description="Folder to run in")


class ShellRun(Tool[RunArgs]):
    name = "shell.run"
    description = (
        "Run one Linux command line and return its exit code and output. Read-only commands "
        "(ls, du, df, ip addr, find, grep, wc, ps, cat, head, uname, free) run at once; "
        "anything else asks the user first."
    )
    usage = Usage(
        when="A standard command answers the step directly: du -sh for folder size, "
        "df -h for free disk space, ip addr for IP addresses, ps for running programs.",
        avoid="A file tool does it (fs.list, fs.read, fs.search), or you would delete files "
        "(use fs.trash).",
        mistakes="macOS commands (ipconfig getifaddr); sudo, which is never allowed; "
        "listing a folder when the question is its size.",
        example='shell.run(cmd="du -sh ~/Downloads")',
    )
    args_model = RunArgs
    observation_kind = "shell_output"
    untrusted = True
    timeout_s = 120

    def __init__(self, *, limits: bool = True) -> None:
        self._limits = limits and shutil.which("systemd-run") is not None

    def tier_for(self, args: RunArgs, policy: Policy) -> Tier:
        return policy.classify_shell(args.cmd).tier

    def refusal(self, args: RunArgs, policy: Policy) -> str:
        return f"{policy.classify_shell(args.cmd).reason}; this command will never run"

    def sentence(self, args: RunArgs) -> str:
        return f"Run this command? {args.cmd}"

    def preview(self, args: RunArgs) -> tuple[str, ...]:
        return (args.cmd,)

    async def run(self, args: RunArgs, ctx: ToolContext) -> ToolResult:
        cwd = resolve(args.cwd, ctx)
        if not cwd.is_dir():
            return ToolResult.fail(f"{args.cwd} is not a folder")
        argv = ["bash", "-c", args.cmd]
        if self._limits:
            argv = ["systemd-run", "--user", "--scope", "--quiet", *LIMITS, *argv]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_clean_env(os.environ, ctx),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        code = proc.returncode or 0
        stdout, stderr = _trim(out), _trim(err)
        data: dict[str, object] = {"exit_code": code, "stdout": stdout, "stderr": stderr}
        if code == 1 and not stdout.strip() and not stderr.strip():
            # grep, pgrep and friends exit 1 to say "no match"; that is an answer.
            data["note"] = "no output and exit code 1: nothing matched"
            return ToolResult.ok(f"`{args.cmd}` found nothing", data=data)
        first = stdout.strip().splitlines()[:1]
        summary = f"`{args.cmd}` exited {code}" + (f": {first[0][:80]}" if first else "")
        if code != 0:
            return ToolResult("error", summary, data=data, error=stderr.strip()[:200] or summary)
        return ToolResult.ok(summary, data=data)


def _clean_env(env: Mapping[str, str], ctx: ToolContext) -> dict[str, str]:
    clean = {name: value for name, value in env.items() if not _SECRET_ENV.search(name)}
    clean["HOME"] = str(ctx.home)
    return clean


def _trim(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


# --- shell.script ---------------------------------------------------------------


class ScriptArgs(ToolArgs):
    language: Literal["bash", "python"]
    code: str = Field(min_length=1, max_length=20_000)
    network: bool = Field(default=False, description="Allow internet access")


class ShellScript(Tool[ScriptArgs]):
    name = "shell.script"
    description = (
        "Run a short bash or Python script in a sandbox: the home folder is read-only, "
        "files can only be written in a temporary folder, and there is no internet unless "
        "network is true. The user always confirms."
    )
    usage = Usage(
        when="A calculation or text processing needs a few lines of code that no single "
        "command or tool does.",
        avoid="One command does it (shell.run), or the task changes files in the home "
        "folder (the sandbox cannot; use the file tools).",
        mistakes="Writing results into the home folder: print them instead.",
        example='shell.script(language="python", code="print(sum(range(101)))")',
    )
    args_model = ScriptArgs
    tier = Tier.CONFIRM
    risk = "medium"
    observation_kind = "shell_output"
    timeout_s = 120

    def __init__(self, *, limits: bool = True, sandbox: bool | None = None) -> None:
        self._limits = limits and shutil.which("systemd-run") is not None
        # None: probe once on first use.
        self._sandbox = sandbox

    def sentence(self, args: ScriptArgs) -> str:
        net = " with internet access" if args.network else ""
        warning = (
            "" if self._sandbox is not False else " Isolation is off: it can change your files."
        )
        return f"Run this {args.language} script{net}?{warning}"

    def preview(self, args: ScriptArgs) -> tuple[str, ...]:
        lines = args.code.strip().splitlines()
        return (*lines[:8], *(["..."] if len(lines) > 8 else []))

    async def run(self, args: ScriptArgs, ctx: ToolContext) -> ToolResult:
        if self._sandbox is None:
            self._sandbox = await bwrap_works()
        interpreter = (
            ["bash", "-c", args.code]
            if args.language == "bash"
            else ["python3", "-I", "-c", args.code]
        )
        with tempfile.TemporaryDirectory(prefix="arcee-script-") as scratch:
            argv = interpreter
            if self._sandbox:
                argv = [*_bwrap(ctx.home, Path(scratch), network=args.network), *argv]
            if self._limits:
                argv = ["systemd-run", "--user", "--scope", "--quiet", *LIMITS, *argv]
            env = _clean_env(os.environ, ctx) | {"HOME": scratch}
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=scratch,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
        code = proc.returncode or 0
        data: dict[str, object] = {
            "exit_code": code,
            "stdout": _trim(out),
            "stderr": _trim(err),
            "sandboxed": self._sandbox,
        }
        summary = f"{args.language} script exited {code}"
        if code != 0:
            return ToolResult(
                "error", summary, data=data, error=_trim(err).strip()[:200] or summary
            )
        return ToolResult.ok(summary, data=data)


def _bwrap(home: Path, scratch: Path, *, network: bool) -> list[str]:
    """Read-only system and home, a writable scratch folder, no session bus or portals."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    argv = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", runtime,
        "--ro-bind", str(home), str(home),
        "--bind", str(scratch), str(scratch),
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
    ]  # fmt: skip
    if not network:
        argv.append("--unshare-net")
    return [*argv, "--"]


async def bwrap_works() -> bool:
    """Whether bubblewrap can create namespaces here (AppArmor may forbid it)."""
    if shutil.which("bwrap") is None:
        return False
    proc = await asyncio.create_subprocess_exec(
        "bwrap", "--ro-bind", "/", "/", "--unshare-net", "--", "true",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    return await proc.wait() == 0
