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
from arcee.core.toolsets import (
    BROWSER,
    CONTROL,
    FILES,
    LOCAL,
    NOTE,
    SYSTEM,
    WAIT,
    WEB,
    filters_for,
)
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
    browser_tools,
    element_line,
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
        ("How much free disk space do I have?", SYSTEM + CONTROL),
        ("Rename the photo in Pictures", FILES + CONTROL),
        ("Hi Arcee!", CONTROL),
        ("Do the thing we discussed", LOCAL + CONTROL + WAIT),
        ("Remember that my work folder is ~/code", FILES + CONTROL + NOTE),
        ("Check my Gmail inbox for anything from the bank", CONTROL + WAIT + BROWSER),
        ("Look at my inbox in Chrome", CONTROL + WAIT + BROWSER),
        ("Open https://example.org and summarise it", CONTROL + WEB),
        ("Search the web for the Ubuntu 26.04 release date", CONTROL + WEB),
        (
            "Save the newest email from Gmail to a file in Documents",
            FILES + CONTROL + WAIT + BROWSER,
        ),
        ("Open example.com in Chrome", CONTROL + WAIT + BROWSER + WEB),
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

    assert filters_for(messages) == FILES + CONTROL + WAIT + BROWSER


async def test_new_tab_for_an_open_site_switches_instead() -> None:
    shared, client = session(
        {
            "tabs": [{"id": 3, "title": "Inbox", "url": "https://mail.example/u/0/"}],
            "switch_tab": {"id": 3},
            "navigate": {"id": 3, "url": "https://mail.example/u/0/#inbox"},
        }
    )

    result = await BrowserTabs(shared).run(
        TabsArgs(action="new", url="https://mail.example/u/0/#inbox"), CTX
    )

    assert result.summary == "Switched to the open tab for https://mail.example/u/0/#inbox"
    assert "new_tab" not in [c[0] for c in client.calls]
    assert shared.tab == 3


async def test_html_entities_are_decoded() -> None:
    page = {**PAGE, "elements": [{"id": "el_1", "role": "row", "name": "A &amp; B"}]}
    shared, _ = session({"elements": page})

    result = await BrowserElements(shared).run(ElementsArgs(), CTX)

    assert result.data is not None
    assert result.data["elements"] == ["el_1 row: A & B"]


def test_elements_are_one_line_each_with_only_the_flags_that_are_set() -> None:
    assert element_line({"id": "el_1", "role": "button", "name": "Compose"}) == (
        "el_1 button: Compose"
    )
    field = {"id": "el_36", "role": "combobox", "name": "Search mail", "value": "is:unread"}
    assert element_line(field) == 'el_36 combobox: Search mail value="is:unread"'
    box = {"id": "el_3", "role": "input", "name": "Pin", "type": "password", "checked": True}
    assert element_line(box) == "el_3 input: Pin type=password (checked)"


async def test_lines_fit_twice_as_many_elements_in_a_tool_result() -> None:
    buttons = [{"id": f"el_{i}", "role": "button", "name": f"Action {i}"} for i in range(60)]
    rows = [{"id": f"el_{60 + i}", "role": "row", "name": "x" * 120} for i in range(40)]
    page = {**PAGE, "elements": buttons + rows}
    as_objects = len(json.dumps(page))
    shared, client = session({"elements": page})

    result = await BrowserElements(shared).run(ElementsArgs(limit=100), CTX)

    as_lines = len(json.dumps(result.data))
    assert as_lines < as_objects * 0.75
    assert client.calls[0][1]["text_chars"] == 800
    assert shared.elements["el_3"]["name"] == "Action 3"  # labels stay for confirmations


def test_only_looking_at_the_page_is_read_only() -> None:
    session = BrowserSession()
    reads = {"browser.get_elements", "browser.read_text"}
    for tool in browser_tools(session):
        assert tool.read_only == (tool.name in reads), tool.name
    tabs = BrowserTabs(session)
    assert not tabs.changes_things(TabsArgs(action="list"))
    assert tabs.changes_things(TabsArgs(action="switch", tab=1))


async def test_opening_and_navigating_bring_chrome_forward() -> None:
    shown: list[str] = []

    async def show(title: str) -> None:
        shown.append(title)

    client = FakeClient(
        {
            "tabs": [],
            "new_tab": {"id": 9},
            "navigate": {"id": 9, "url": "https://youtube.example/", "title": "YouTube"},
        }
    )
    shared = BrowserSession(client=client, show=show)  # pyright: ignore[reportArgumentType]

    await BrowserTabs(shared).run(TabsArgs(action="new"), CTX)
    await BrowserNavigate(shared).run(NavigateArgs(url="https://youtube.example/"), CTX)

    assert shown == ["", "YouTube"]
