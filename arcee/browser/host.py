"""Native messaging host `org.arcee.browser`: the bridge between Chrome and Arcee.

Chrome starts this process for the Arcee extension and talks to it over stdin
and stdout (4-byte little-endian length, then JSON). The host listens on a Unix
socket in the user's runtime directory, mode 0600, so only the user's own
processes can reach it, and there is no network listener (spec section 3).
Requests from socket clients are forwarded to the extension under host-assigned
ids, so several clients cannot mix up each other's replies.
"""

import asyncio
import contextlib
import itertools
import json
import os
import struct
import sys
from pathlib import Path
from typing import Any, Final

HOST_NAME: Final = "org.arcee.browser"
EXTENSION_ID: Final = "hgefhfepecjjabmnbjoikomoplpffjae"
MAX_MESSAGE: Final = 64 * 1024 * 1024


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "arcee" / "browser.sock"


def encode(message: dict[str, Any]) -> bytes:
    body = json.dumps(message, ensure_ascii=False).encode()
    return struct.pack("<I", len(body)) + body


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """One framed message from Chrome, or None when Chrome closed the stream."""
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE:
        raise ValueError(f"message of {length} bytes is too large")
    return json.loads(await reader.readexactly(length))


class Bridge:
    def __init__(self, to_chrome: asyncio.StreamWriter | Any) -> None:
        self._to_chrome = to_chrome
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[asyncio.StreamWriter, Any]] = {}

    async def from_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Forward a socket client's newline-delimited requests to the extension."""
        try:
            while line := await reader.readline():
                request = json.loads(line)
                host_id = next(self._ids)
                self._pending[host_id] = (writer, request.get("id"))
                message = {
                    "id": host_id,
                    "method": request["method"],
                    "params": request.get("params", {}),
                }
                self._to_chrome.write(encode(message))
                await self._to_chrome.drain()
        except ConnectionError, json.JSONDecodeError, KeyError:
            pass
        finally:
            self._pending = {k: v for k, v in self._pending.items() if v[0] is not writer}
            writer.close()

    async def to_client(self, reply: dict[str, Any]) -> None:
        """Route an extension reply back to the client that asked."""
        target = self._pending.pop(reply.get("id", -1), None)
        if target is None:
            return
        writer, client_id = target
        with contextlib.suppress(ConnectionError):
            writer.write((json.dumps({**reply, "id": client_id}) + "\n").encode())
            await writer.drain()


def _prepare(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.unlink(missing_ok=True)  # a stale socket from a host that died


async def serve(stdin: asyncio.StreamReader, stdout: Any, path: Path) -> None:
    _prepare(path)
    bridge = Bridge(stdout)
    server = await asyncio.start_unix_server(bridge.from_client, path=str(path))
    os.chmod(path, 0o600)
    try:
        while (message := await read_message(stdin)) is not None:
            await bridge.to_client(message)
    finally:
        server.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


async def main() -> None:
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=MAX_MESSAGE)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    stdout = asyncio.StreamWriter(transport, protocol, None, loop)
    await serve(stdin, stdout, socket_path())


if __name__ == "__main__":
    asyncio.run(main())
