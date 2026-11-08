"""The built-in tool set for phase 1 (spec section 11, the text-mode subset)."""

from collections.abc import Callable

from arcee.tools.apps import AppList, AppOpen
from arcee.tools.browser import BrowserSession, browser_tools
from arcee.tools.calendar import calendar_tools
from arcee.tools.capture import capture_tools
from arcee.tools.clock import clock_tools
from arcee.tools.control import CONTROL_TOOLS, UTILITY_CONTROL_TOOLS
from arcee.tools.desktop import desktop_tools
from arcee.tools.desktop_settings import settings_tools
from arcee.tools.documents import document_tools
from arcee.tools.extra import extra_tools
from arcee.tools.files import (
    FsCopy,
    FsList,
    FsMove,
    FsOpen,
    FsRead,
    FsSearch,
    FsTrash,
    FsUsage,
    FsWrite,
)
from arcee.tools.later import later_tools
from arcee.tools.mail import mail_tools
from arcee.tools.markets import market_tools
from arcee.tools.media import MediaControl, MediaNowPlaying
from arcee.tools.memory import Memory, memory_tools
from arcee.tools.registry import Registry
from arcee.tools.reminders import Reminders, reminder_tools
from arcee.tools.screen import screen_tools
from arcee.tools.shell import ShellRun, ShellScript
from arcee.tools.system import NetworkStatus, SystemStatus
from arcee.tools.travel import travel_tools
from arcee.tools.voice import Say, VoiceSay
from arcee.tools.web import WebFetch, WebNews, WebSearch, WebWeather
from arcee.tools.windows import window_tools


def builtin_registry(
    *,
    shell_limits: bool = True,
    browser: BrowserSession | None = None,
    memory: Memory | None = None,
    reminders: Reminders | None = None,
    say: Callable[[], Say | None] = lambda: None,
) -> Registry:
    """Every built-in tool: files, system facts, shell, then the control tools; and the
    memory and reminder tools when there is a store to back them."""
    return Registry(
        [
            FsList(),
            FsUsage(),
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
            WebNews(),
            WebWeather(),
            MediaControl(),
            MediaNowPlaying(),
            *settings_tools(),
            *capture_tools(),
            *document_tools(),
            *clock_tools(),
            *market_tools(),
            *travel_tools(),
            *extra_tools(memory),  # pyright: ignore[reportArgumentType]
            *calendar_tools(),
            *mail_tools(),
            WebFetch(),
            *browser_tools(browser),
            *desktop_tools(),
            *screen_tools(),
            *window_tools(),
            ShellRun(limits=shell_limits),
            ShellScript(limits=shell_limits),
            *later_tools(reminders),
            *(tool() for tool in UTILITY_CONTROL_TOOLS),
            *(tool() for tool in CONTROL_TOOLS),
            *(memory_tools(memory) if memory is not None else ()),
            *(reminder_tools(reminders) if reminders is not None else ()),
            VoiceSay(say),
        ]
    )
