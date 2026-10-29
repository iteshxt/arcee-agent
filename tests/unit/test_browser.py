import asyncio
import json
import stat
import struct
from pathlib import Path
from typing import Any

import pytest

from arcee.browser.client import BrowserClient, BrowserError
from arcee.browser.host import EXTENSION_ID, HOST_NAME, encode, read_message, serve
from arcee.browser.setup import install
from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import BROWSER, CONTROL, LOCAL, WEB, filters_for
from arcee.models.base import Message, WireCall
from arcee.tools.base import NoArgs, ToolContext
from arcee.tools.browser import (
    BrowserBack,
    BrowserClick,
    BrowserElements,
    BrowserFill,
    BrowserNavigate,
    BrowserSession,
    BrowserTabs,
    ClickArgs,
    ElementsArgs,
    FillArgs,
    NavigateArgs,
    TabsArgs,
)

CTX = ToolContext(task_id="t_1", home=Path("/home/u"))


class FakeExtension:
    """Stands in for Chrome: answers framed requests written to the host's stdout."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.to_host = asyncio.StreamReader()  # the host's stdin
        self.seen: list[dict[str, Any]] = []
        self._buffer = b""

    # The host writes to this object as if it were Chrome's stdin.
    def write(self, data: bytes) -> None:
        self._buffer += data
        while len(self._buffer) >= 4:
            (length,) = struct.unpack("<I", self._buffer[:4])
            if len(self._buffer) < 4 + length:
                return
            message = json.loads(self._buffer[4 : 4 + length])
            self._buffer = self._buffer[4 + length :]
            self.seen.append(message)
            answer = self.answers.get(message["method"])
            if isinstance(answer, Exception):
                reply = {"id": message["id"], "ok": False, "error": str(answer)}
            else:
                reply = {"id": message["id"], "ok": True, "result": answer}
            self.to_host.feed_data(encode(reply))

    async def drain(self) -> None:
        return None


@pytest.fixture
async def bridge(tmp_path: Path) -> Any:
    socket = tmp_path / "run" / "browser.sock"
    extension = FakeExtension(
        {"ping": {"version": "0.2.0"}, "tabs": [{"id": 1}], "click": RuntimeError("el_9 is gone")}
    )
    task = asyncio.create_task(serve(extension.to_host, extension, socket))
    for _ in range(100):
        if socket.exists():
            break
        await asyncio.sleep(0.01)
    yield socket, extension
    extension.to_host.feed_eof()
    await task


async def test_requests_round_trip_through_host(bridge: Any) -> None:
    socket, extension = bridge
    client = BrowserClient(socket)

    assert await client.request("ping") == {"version": "0.2.0"}
    assert await client.request("tabs") == [{"id": 1}]
    with pytest.raises(BrowserError, match="el_9 is gone"):
        await client.request("click", {"target": "el_9"})
    await client.aclose()

    assert [m["method"] for m in extension.seen] == ["ping", "tabs", "click"]
    assert extension.seen[2]["params"] == {"target": "el_9"}
    assert stat.S_IMODE(socket.stat().st_mode) == 0o600


async def test_missing_bridge_says_how_to_connect(tmp_path: Path) -> None:
    with pytest.raises(BrowserError, match="extension is not connected"):
        await BrowserClient(tmp_path / "none.sock").request("ping")


async def test_framing_reads_one_message_and_stops_at_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(encode({"id": 1, "ok": True}))
    reader.feed_eof()

    assert await read_message(reader) == {"id": 1, "ok": True}
    assert await read_message(reader) is None


def test_setup_registers_the_host_for_the_extension(tmp_path: Path) -> None:
    launcher, manifest = install(tmp_path / "data", tmp_path / "hosts")

    content = json.loads(manifest.read_text())
    assert manifest.name == f"{HOST_NAME}.json"
    assert content["path"] == str(launcher)
    assert content["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID}/"]
    assert launcher.stat().st_mode & stat.S_IXUSR
    assert "-m arcee.browser.host" in launcher.read_text()


class FakeClient:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params or {}))
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer


PAGE = {
    "url": "https://mail.example/inbox",
    "title": "Inbox",
    "count": 3,
    "text_excerpt": "Inbox",
    "elements": [
        {"id": "el_1", "role": "button", "name": "Compose"},
        {"id": "el_2", "role": "button", "name": "Send"},
        {"id": "el_3", "role": "input", "name": "Password", "type": "password"},
        {"id": "el_4", "role": "textbox", "name": "Message Body", "editable": True},
    ],
}


def session(answers: dict[str, Any]) -> tuple[BrowserSession, FakeClient]:
    client = FakeClient(answers)
    return BrowserSession(client=client), client  # pyright: ignore[reportArgumentType]


class TestTools:
    async def test_elements_are_remembered_for_the_gate(self) -> None:
        shared, _ = session({"elements": PAGE})
        result = await BrowserElements(shared).run(ElementsArgs(), CTX)
        click, fill = BrowserClick(shared), BrowserFill(shared)

        assert result.summary == "Inbox: 4 elements"
        assert click.tier_for(ClickArgs(target="el_1"), Policy()) is Tier.AUTO
        assert click.tier_for(ClickArgs(target="el_2"), Policy()) is Tier.CONFIRM
        assert click.sentence(ClickArgs(target="el_2")) == "Click “Send” in Chrome?"
        secret = FillArgs(target="el_3", text="hunter2")
        assert fill.tier_for(secret, Policy()) is Tier.CONFIRM
        assert fill.preview(secret) == ("(hidden)",)
        assert fill.tier_for(FillArgs(target="el_4", text="Hi"), Policy()) is Tier.AUTO

    async def test_actions_call_the_extension_and_forget_stale_numbers(self) -> None:
        shared, client = session({"elements": PAGE, "click": {"clicked": "el_1"}})
        await BrowserElements(shared).run(ElementsArgs(), CTX)

        result = await BrowserClick(shared).run(ClickArgs(target="el_1"), CTX)

        assert result.summary == "Clicked Compose"
        assert client.calls[-1] == ("click", {"target": "el_1"})
        assert shared.elements == {}

    async def test_navigation_and_tabs(self) -> None:
        shared, client = session(
            {
                "tabs": [{"id": 7, "url": "https://b.example/x", "active": True}],
                "new_tab": {"id": 9},
                "navigate": {"id": 9, "url": "https://a.example", "title": "A"},
                "back": {},
            }
        )

        opened = await BrowserNavigate(shared).run(NavigateArgs(url="https://a.example"), CTX)
        bad = await BrowserNavigate(shared).run(NavigateArgs(url="a.example"), CTX)
        back = await BrowserBack(shared).run(NoArgs(), CTX)

        assert opened.summary == "Opened A"
        assert bad.error == "give a full http or https address"
        assert back.summary == "Went back"
        # A new site opens in a new tab, which every later call then targets.
        assert ("new_tab", {"url": "https://a.example"}) in client.calls
        assert client.calls[-1] == ("back", {"tab": 9})
        assert shared.tab == 9
        assert (
            BrowserTabs(shared).tier_for(TabsArgs(action="close", tab=7), Policy()) is Tier.CONFIRM
        )

    async def test_reuses_a_tab_already_on_the_site(self) -> None:
        shared, client = session(
            {
                "tabs": [{"id": 4, "url": "https://mail.example/inbox", "active": False}],
                "switch_tab": {"id": 4},
                "navigate": {"id": 4, "url": "https://mail.example/#inbox", "title": "Inbox"},
            }
        )

        await BrowserNavigate(shared).run(NavigateArgs(url="https://mail.example/#inbox"), CTX)

        assert [c[0] for c in client.calls] == ["tabs", "switch_tab", "navigate"]
        assert client.calls[-1][1]["tab"] == 4
        assert "new_tab" not in [c[0] for c in client.calls]

    async def test_bridge_failures_become_tool_errors(self) -> None:
        shared, _ = session({"elements": BrowserError("not connected")})

        result = await BrowserElements(shared).run(ElementsArgs(), CTX)

        assert result.error == "not connected"


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("How much free disk space do I have?", LOCAL + CONTROL),
        ("Rename the photo in Pictures", LOCAL + CONTROL),
        ("Check my Gmail inbox for anything from the bank", CONTROL + BROWSER),
        ("Look at my inbox in Chrome", CONTROL + BROWSER),
        ("Open https://example.org and summarise it", CONTROL + WEB),
        ("Search the web for the Ubuntu 26.04 release date", CONTROL + WEB),
        ("Save the newest email from Gmail to a file in Documents", LOCAL + CONTROL + BROWSER),
        ("Open example.com in Chrome", CONTROL + BROWSER + WEB),
    ],
)
def test_tool_groups_follow_the_request(request_text: str, expected: tuple[str, ...]) -> None:
    assert filters_for([Message("user", request_text)]) == expected


def test_groups_stay_once_used() -> None:
    messages = [
        Message("user", "what is in the first one?"),
        Message("assistant", "", tool_calls=(WireCall("browser_get_elements", {}),)),
        Message("assistant", "", tool_calls=(WireCall("fs_write", {}),)),
    ]

    assert filters_for(messages) == LOCAL + CONTROL + BROWSER
