"""Arcee's own follow-ups (decision 0020): `control.set_timer` and the end of a job.

A follow-up is a reminder of kind "followup" in History, fired by arceed's scheduler
like the user's timers. Its text is the request to run then; its `detail` (JSON)
holds the request it continues and, optionally, a watch: something code checks
without the model (a page changed or says something, a file appeared, a read-only
command succeeds), every so often, until it happens or the watch gives up. Only then
is the model called, with what was seen.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

log = logging.getLogger(__name__)

WatchKind = Literal[
    "page_changes", "page_contains", "page_lacks", "file_exists", "file_gone", "command_succeeds"
]
WATCH_KINDS: Final[tuple[str, ...]] = (
    "page_changes",
    "page_contains",
    "page_lacks",
    "file_exists",
    "file_gone",
    "command_succeeds",
)
MAX_PENDING: Final = 10  # follow-ups waiting at once
COMMAND_LIMIT_S: Final = 30.0


@dataclass(frozen=True, slots=True)
class Watch:
    kind: str
    target: str  # the page's address, the file's path or the command
    text: str = ""  # page_contains / page_lacks
    every_s: float = 600.0
    until: float = 0.0  # epoch seconds: give up after this
    baseline: str = ""  # page_changes: the page's hash when the watch was set


@dataclass(frozen=True, slots=True)
class Detail:
    request: str  # the request this follows up
    set_at: float
    watch: Watch | None = None

    def dumps(self) -> str:
        data: dict[str, Any] = {"request": self.request, "set_at": self.set_at}
        if self.watch is not None:
            w = self.watch
            data["watch"] = {
                "kind": w.kind,
                "target": w.target,
                "text": w.text,
                "every_s": w.every_s,
                "until": w.until,
                "baseline": w.baseline,
            }
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def loads(cls, raw: str) -> Detail:
        try:
            loaded: Any = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            loaded = {}
        data = cast(dict[str, Any], loaded if isinstance(loaded, dict) else {})
        watch = cast(dict[str, Any] | None, data.get("watch"))
        return cls(
            request=str(data.get("request", "")),
            set_at=float(data.get("set_at", 0.0)),
            watch=Watch(
                kind=str(watch["kind"]),
                target=str(watch["target"]),
                text=str(watch.get("text", "")),
                every_s=float(watch.get("every_s", 600.0)),
                until=float(watch.get("until", 0.0)),
                baseline=str(watch.get("baseline", "")),
            )
            if isinstance(watch, dict)
            else None,
        )


def request_for(check: str, detail: Detail, seen: str = "") -> str:
    """The request a follow-up runs: what to do, with where it came from and what was
    seen, so a fresh task (no memory of the first) can act on it."""
    when = datetime.fromtimestamp(detail.set_at).strftime("%H:%M") if detail.set_at else ""
    parts = [check.strip()]
    if seen:
        parts.append(f"What Arcee saw: {seen}")
    if detail.request:
        origin = f"(A follow-up Arcee set for itself at {when} while doing: “{detail.request}”.)"
        parts.append(origin)
    return "\n\n".join(parts)


# --- checks that need no model ------------------------------------------------------------

PageText = Callable[[str], Awaitable[str]]
RunCommand = Callable[[str], Awaitable[bool]]


def digest(text: str) -> str:
    """A page's fingerprint: its words, so whitespace and markup shifts do not count."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:16]


async def page_text(url: str) -> str:
    """A page's readable text (web.fetch's own reading of it)."""
    from arcee.tools.base import ToolContext
    from arcee.tools.web import FetchArgs, WebFetch

    ctx = ToolContext(task_id=f"watch-{time.monotonic_ns()}", home=Path.home())
    result = await WebFetch().run(FetchArgs(url=url, max_chars=20_000), ctx)
    if result.status != "ok" or result.data is None:
        raise RuntimeError(result.error or "the page could not be read")
    return str(result.data.get("text", ""))


async def command_succeeds(cmd: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        cmd,
        cwd=Path.home(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), COMMAND_LIMIT_S) == 0
    except TimeoutError:
        process.kill()
        await process.wait()
        return False


async def check(
    watch: Watch,
    *,
    read_page: PageText = page_text,
    run: RunCommand = command_succeeds,
) -> str | None:
    """What happened, or None while it has not."""
    kind = watch.kind
    if kind in ("file_exists", "file_gone"):
        there = await asyncio.to_thread(os.path.exists, watch.target)  # absolute: set_timer
        if kind == "file_exists" and there:
            return f"{watch.target} is there now"
        if kind == "file_gone" and not there:
            return f"{watch.target} is gone"
        return None
    if kind == "command_succeeds":
        return f"`{watch.target}` succeeded" if await run(watch.target) else None
    text = await read_page(watch.target)
    if kind == "page_changes":
        return f"the page {watch.target} changed" if digest(text) != watch.baseline else None
    found = watch.text.lower() in text.lower()
    if kind == "page_contains" and found:
        return f"the page {watch.target} now says “{watch.text}”"
    if kind == "page_lacks" and not found:
        return f"the page {watch.target} no longer says “{watch.text}”"
    return None


# --- firing -------------------------------------------------------------------------------

RunRequest = Callable[[str, float], Awaitable[bool]]
Notify = Callable[[str, str], Awaitable[None]]


class Rearm(Protocol):
    """The store, as a follow-up firing sees it."""

    async def set_reminder(
        self, reminder_id: str, *, status: str, due: float | None = None
    ) -> bool: ...


async def fire(
    reminder_id: str,
    text: str,
    raw_detail: str,
    late: float,
    *,
    store: Rearm,
    run_request: RunRequest,
    notify: Notify,
    clock: Callable[[], float] = time.time,
    read_page: PageText = page_text,
    run: RunCommand = command_succeeds,
) -> None:
    """A follow-up came due: run its request, or check its watch first and look again
    later while it has not happened."""
    detail = Detail.loads(raw_detail)
    watch = detail.watch
    if watch is None:
        await run_request(request_for(text, detail), late)
        return
    try:
        seen = await check(watch, read_page=read_page, run=run)
    except Exception as exc:  # the page down for a while: look again next time
        log.info("follow-up %s could not check: %s", reminder_id, exc)
        seen = None
    now = clock()
    if seen is not None:
        await run_request(request_for(text, detail, seen), late)
        return
    if now + watch.every_s <= watch.until:
        await store.set_reminder(reminder_id, status="pending", due=now + watch.every_s)
        return
    hours = max(1, round((watch.until - detail.set_at) / 3600))
    await notify(
        "Arcee stopped watching",
        f"{_watched(watch)}: it did not happen in {hours} h. ({text.strip()[:120]})",
    )


def _watched(watch: Watch) -> str:
    return {
        "page_changes": f"{watch.target} did not change",
        "page_contains": f"{watch.target} never said “{watch.text}”",
        "page_lacks": f"{watch.target} kept saying “{watch.text}”",
        "file_exists": f"{watch.target} never appeared",
        "file_gone": f"{watch.target} is still there",
        "command_succeeds": f"`{watch.target}` never succeeded",
    }.get(watch.kind, watch.target)
