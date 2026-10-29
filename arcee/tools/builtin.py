"""The built-in tool set for phase 1 (spec section 11, the text-mode subset)."""

from arcee.tools.apps import AppList, AppOpen
from arcee.tools.control import CONTROL_TOOLS, UTILITY_CONTROL_TOOLS
from arcee.tools.files import (
    FsCopy,
    FsList,
    FsMove,
    FsOpen,
    FsRead,
    FsSearch,
    FsTrash,
    FsWrite,
)
from arcee.tools.registry import Registry
from arcee.tools.shell import ShellRun, ShellScript
from arcee.tools.system import NetworkStatus, SystemStatus
from arcee.tools.web import WebFetch, WebSearch


def builtin_registry(*, shell_limits: bool = True) -> Registry:
    """Every built-in tool: files, system facts, shell, then the control tools."""
    return Registry(
        [
            FsList(),
            FsRead(),
            FsSearch(),
            FsTrash(),
            FsWrite(),
            FsMove(),
            FsCopy(),
            FsOpen(),
            AppList(),
            AppOpen(),
            SystemStatus(),
            NetworkStatus(),
            WebSearch(),
            WebFetch(),
            ShellRun(limits=shell_limits),
            ShellScript(limits=shell_limits),
            *(tool() for tool in UTILITY_CONTROL_TOOLS),
            *(tool() for tool in CONTROL_TOOLS),
        ]
    )
