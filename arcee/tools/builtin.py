"""The built-in tool set for phase 1 (spec section 11, the text-mode subset)."""

from arcee.tools.control import CONTROL_TOOLS
from arcee.tools.files import FsList, FsRead, FsSearch, FsTrash
from arcee.tools.registry import Registry
from arcee.tools.shell import ShellRun


def builtin_registry(*, shell_limits: bool = True) -> Registry:
    """Every built-in tool, file and shell tools first, control tools last."""
    return Registry(
        [
            FsList(),
            FsRead(),
            FsSearch(),
            FsTrash(),
            ShellRun(limits=shell_limits),
            *(tool() for tool in CONTROL_TOOLS),
        ]
    )
