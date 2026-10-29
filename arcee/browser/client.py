"""Arcee's side of the browser bridge: requests over the host's Unix socket."""

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any, Final

from arcee.browser.host import socket_path

REQUEST_TIMEOUT_S: Final = 30.0
NOT_CONNECTED: Final = (
    "the Arcee Chrome extension is not connected; open Chrome with the extension loaded "
    "(see `arcee-cli browser-setup`)"
)


class BrowserError(Exception):
    """The extension reported a failure, or the bridge is not available."""


class BrowserClient:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or socket_path()
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        wait_s: float = REQUEST_TIMEOUT_S,
    ) -> Any:
        """One request to the extension; raises BrowserError on failure."""
        async with self._lock:
            reader, writer = await self._connect()
            request_id = next(self._ids)
            line = json.dumps({"id": request_id, "method": method, "params": params or {}})
            try:
                writer.write(line.encode() + b"\n")
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), wait_s)
            except (ConnectionError, TimeoutError) as exc:
                await self.aclose()
                raise BrowserError(f"no answer from the browser ({type(exc).__name__})") from exc
            if not raw:
                await self.aclose()
                raise BrowserError(NOT_CONNECTED)
            reply = json.loads(raw)
            if not reply.get("ok"):
                raise BrowserError(str(reply.get("error", "the browser reported an error")))
            return reply.get("result")

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    str(self._path), limit=16 * 1024 * 1024
                )
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                raise BrowserError(NOT_CONNECTED) from exc
        return self._reader, self._writer

    async def aclose(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None
