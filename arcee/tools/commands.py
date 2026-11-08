"""Running the desktop's own commands for a tool: wpctl, gsettings, nmcli, bluetoothctl.

Tools that change a setting run a fixed command with arguments they build themselves
(never a shell line, never words of the model's pasted into one). `Commands` is the
seam tests replace, so no test touches the real desktop. `later` runs a command after a
delay as a transient systemd user timer: it fires even if arceed has quit since.
"""

import asyncio
import contextlib
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

TIMEOUT_S: Final = 15.0
OUTPUT_LIMIT: Final = 8_000_000  # pw-dump on a desktop: about 1 MB


@dataclass(frozen=True, slots=True)
class Done:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def why(self) -> str:
        """The command's own complaint, in one line."""
        text = (self.err or self.out).strip().splitlines()
        return text[-1] if text else f"exit code {self.code}"


class Commands(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        limit_s: float = TIMEOUT_S,
        stdin: bytes | None = None,
        quiet: bool = False,
    ) -> Done: ...

    def has(self, program: str) -> bool: ...


class SystemCommands:
    """The real ones."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        limit_s: float = TIMEOUT_S,
        stdin: bytes | None = None,
        quiet: bool = False,
    ) -> Done:
        """`quiet`: not reading its output, for a program that leaves a child behind
        holding it (wl-copy serves the paste after it returns)."""
        output = asyncio.subprocess.DEVNULL if quiet else asyncio.subprocess.PIPE
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=output,
                stderr=output,
            )
        except FileNotFoundError:
            return Done(127, "", f"{argv[0]} is not installed")
        try:
            out, err = await asyncio.wait_for(process.communicate(stdin), limit_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return Done(124, "", f"{argv[0]} did not finish in {limit_s:.0f} s")
        return Done(
            process.returncode if process.returncode is not None else 1,
            (out or b"")[:OUTPUT_LIMIT].decode(errors="replace"),
            (err or b"")[:OUTPUT_LIMIT].decode(errors="replace"),
        )

    def has(self, program: str) -> bool:
        return shutil.which(program) is not None


async def later(commands: Commands, unit: str, minutes: float, argv: Sequence[str]) -> Done:
    """Run `argv` in `minutes` as the transient user timer `unit` (one at a time: an
    earlier one of the same name is replaced)."""
    await cancel_later(commands, unit)
    seconds = max(1, round(minutes * 60))
    return await commands.run(
        [
            "systemd-run",
            "--user",
            f"--unit={unit}",
            f"--on-active={seconds}s",
            "--timer-property=AccuracySec=1s",
            "--collect",
            *argv,
        ]
    )


async def cancel_later(commands: Commands, unit: str) -> bool:
    """Stop the timer `unit` if it is waiting; whether there was one."""
    done = await commands.run(["systemctl", "--user", "stop", f"{unit}.timer"])
    return done.ok
