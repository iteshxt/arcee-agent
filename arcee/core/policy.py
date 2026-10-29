"""Permission policy (spec section 12): tier overrides, the shell allowlist and `never` rules.

The shell classifier is deliberately conservative. A command line runs without
asking only when every part of a plain pipeline is an allowlisted read-only
command with no dangerous flag; anything it cannot read with certainty (command
substitution, redirection, chaining) needs confirmation, and the commands that
can destroy data or escalate privileges are refused outright.
"""

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final


class Tier(StrEnum):
    """Permission tier; the gate decides with it, the tool never does."""

    AUTO = "auto"
    CONFIRM = "confirm"
    NEVER = "never"


# Read-only commands that run without asking, by argv prefix, with the flags that
# would make them write or execute (spec section 11, `shell.run`).
DEFAULT_SHELL_ALLOWLIST: Final[Mapping[tuple[str, ...], frozenset[str]]] = {
    ("ls",): frozenset(),
    ("cat",): frozenset(),
    ("grep",): frozenset(),
    ("find",): frozenset(
        {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
    ),
    ("head",): frozenset(),
    ("tail",): frozenset(),
    ("wc",): frozenset(),
    ("df",): frozenset(),
    ("du",): frozenset(),
    ("ps",): frozenset(),
    ("pgrep",): frozenset(),
    ("which",): frozenset(),
    ("date",): frozenset({"-s", "--set"}),
    ("uname",): frozenset(),
    ("free",): frozenset(),
    ("nproc",): frozenset(),
    ("uptime",): frozenset(),
    ("whoami",): frozenset(),
    ("stat",): frozenset(),
    ("file",): frozenset(),
    ("lsblk",): frozenset(),
    ("pwd",): frozenset(),
    ("hostname",): frozenset(),
    ("ip", "addr"): frozenset(),
    ("ip", "a"): frozenset(),
    ("ip", "route"): frozenset(),
    ("git", "status"): frozenset(),
    ("git", "log"): frozenset({"--output"}),
}

_NEVER_COMMANDS: Final = frozenset(
    {
        "sudo", "su", "doas", "pkexec", "dd", "fdisk", "sfdisk", "parted", "wipefs",
        "shred", "shutdown", "reboot", "poweroff", "halt", "init", "passwd", "chpasswd",
        "useradd", "userdel", "usermod", "iptables", "nft", "ufw",
    }
)  # fmt: skip
_SHELLS: Final = frozenset({"sh", "bash", "zsh", "dash", "fish", "python", "python3", "perl"})
_RECURSIVE: Final = frozenset({"-r", "-R", "--recursive"})
_SYSTEM_PATH: Final = re.compile(r">>?\s*/(etc|usr|bin|sbin|boot|lib|lib64|var|opt|sys|proc|dev)\b")
_SEPARATOR_CHARS: Final = frozenset(";|&()<>")
# Anything that makes a command line more than a plain pipeline of simple commands.
_COMPLEX: Final = re.compile(r"\$\(|`|[<>]|&|;|\|\||\n")


@dataclass(frozen=True, slots=True)
class ShellVerdict:
    tier: Tier
    reason: str


@dataclass(frozen=True, slots=True)
class Policy:
    """What the gate enforces. Defaults match the spec; `policy.toml` loading comes later."""

    tier_overrides: Mapping[str, Tier] = field(default_factory=dict[str, Tier])
    shell_allowlist: Mapping[tuple[str, ...], frozenset[str]] = field(
        default_factory=lambda: dict(DEFAULT_SHELL_ALLOWLIST)
    )
    # "Ask me for everything": every `auto` becomes `confirm`.
    confirm_all: bool = False

    def classify_shell(self, command: str) -> ShellVerdict:
        """Decide the tier of one `shell.run` command line."""
        if _SYSTEM_PATH.search(command):
            return ShellVerdict(Tier.NEVER, "writes to a system path")
        try:
            commands = _commands(command)
        except ValueError:
            return ShellVerdict(Tier.CONFIRM, "the command line could not be parsed")
        # Every command is checked, including ones hidden behind `;`, `&&` or `$(`.
        for argv, after in commands:
            if argv and (reason := _never_reason(argv, piped_into=after == "|")):
                return ShellVerdict(Tier.NEVER, reason)
        if _COMPLEX.search(command):
            return ShellVerdict(Tier.CONFIRM, "uses redirection, chaining or substitution")
        if any(not argv for argv, _ in commands):
            return ShellVerdict(Tier.CONFIRM, "empty pipeline segment")
        for argv, _ in commands:
            if reason := self._not_allowlisted(argv):
                return ShellVerdict(Tier.CONFIRM, reason)
        return ShellVerdict(Tier.AUTO, "allowlisted read-only command")

    def _not_allowlisted(self, argv: list[str]) -> str | None:
        for prefix, banned in self.shell_allowlist.items():
            if tuple(argv[: len(prefix)]) == prefix:
                flags = (arg.split("=", 1)[0] for arg in argv[1:])
                if flag := next((f for f in flags if f in banned), None):
                    return f"{argv[0]} with {flag} can modify files"
                return None
        return f"{argv[0]!r} is not on the read-only allowlist"


def _commands(command: str) -> list[tuple[list[str], str]]:
    """Split a command line into argvs, each with the separator that preceded it.

    Separators are runs of `;|&()<>`, so `a; b`, `a && b`, `a | b`, `$(b)` and
    backticks all put `b` in command position. Raises ValueError on unbalanced quotes.
    """
    # A backtick substitution starts a command just like `$(`.
    lexer = shlex.shlex(command.replace("`", " ( "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[tuple[list[str], str]] = []
    argv: list[str] = []
    after = ""
    for token in lexer:
        if token and set(token) <= _SEPARATOR_CHARS:
            commands.append((argv, after))
            argv, after = [], token
        else:
            argv.append(token)
    commands.append((argv, after))
    # `$(` leaves a lone `$` before the `(`, and substitutions leave empty argvs
    # around their parentheses; neither is a missing command.
    return [
        (argv[:-1] if argv[-1:] == ["$"] else argv, sep)
        for argv, sep in commands
        if (argv and argv != ["$"]) or sep not in {"(", ")"}
    ]


def _never_reason(argv: list[str], *, piped_into: bool) -> str | None:
    name = PurePosixPath(argv[0]).name
    if name in _NEVER_COMMANDS or name.startswith("mkfs"):
        return f"{name!r} is never run by Arcee"
    if piped_into and name in _SHELLS:
        return "piping into a shell or interpreter runs unreviewed code"
    if name in {"rm", "unlink", "rmdir"}:
        # Deleting goes through the trash, recoverably; there is no permanent delete.
        targets = [arg for arg in argv[1:] if not arg.startswith("-")]
        hint = f" with paths {targets}" if targets else ""
        kind = "recursive delete" if _has_recursive(argv) else "permanent delete"
        return f"{kind}; use fs.trash{hint} instead"
    if name in {"chmod", "chown"} and _has_recursive(argv):
        return f"recursive {name}"
    if name == "systemctl" and any(a in {"poweroff", "reboot", "halt"} for a in argv):
        return "power actions go through power.action"
    return None


def _has_recursive(argv: list[str]) -> bool:
    # Combined short flags such as -rf count as recursive too.
    return any(
        arg in _RECURSIVE
        or (arg.startswith("-") and not arg.startswith("--") and "r" in arg.lower())
        for arg in argv[1:]
    )
