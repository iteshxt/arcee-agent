"""media.play (Spotify, app or web) and whatsapp.send (WhatsApp Web), on a scripted Chrome."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from arcee.browser.client import BrowserClient
from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import filters_for
from arcee.models.base import Message
from arcee.tools.base import ToolContext
from arcee.tools.browser import BrowserSession
from arcee.tools.media import Player
from arcee.tools.music import LIKED_URI, MediaPlay, PlayArgs
from arcee.tools.whatsapp import SendArgs, WhatsAppSend, phone_number

CTX = ToolContext("t_1", Path("/home/user"), request="")
Answer = Callable[[dict[str, Any]], Any]


class Chrome:
    """The extension's side: each method answers from `answers` (by method, or by
    method and selector), and every request is kept."""

    def __init__(self, answers: dict[str, Answer]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        params = params or {}
        self.calls.append((method, params))
        key = f"{method} {params.get('selector', '')}".strip()
        for name in (key, method):
            if name in self.answers:
                return self.answers[name](params)
        return {}

    def made(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]


def session(chrome: Chrome) -> BrowserSession:
    return BrowserSession(client=cast(BrowserClient, chrome))


class FakePlayers:
    def __init__(self, *, running: bool = True, plays: bool = True) -> None:
        self.running = running
        self.plays = plays
        self.status = "Stopped"
        self.opened: list[str] = []
        self.commands: list[str] = []

    async def list(self) -> list[Player]:
        players = [
            Player("org.mpris.MediaPlayer2.chromium.instance1", "Chrome", "Paused", "", "", "")
        ]
        if self.running:
            title = "Kesariya" if self.status == "Playing" else ""
            artist = "Arijit Singh" if title else ""
            players.append(
                Player("org.mpris.MediaPlayer2.spotify", "spotify", self.status, title, artist, "")
            )
        return players

    async def open_uri(self, bus_name: str, uri: str) -> None:
        self.opened.append(uri)
        if self.plays:
            self.status = "Playing"

    async def command(self, player: Player, method: str) -> None:
        self.commands.append(method)


SEARCH_RESULTS = [
    {"href": "https://open.spotify.com/track/6VBhH7CyP56BXjp8VsDFPZ", "text": "Kesariya"},
    {"href": "https://open.spotify.com/track/other", "text": "Kesariya (Remix)"},
]


def spotify_chrome() -> Chrome:
    return Chrome(
        {
            "new_tab": lambda p: {"id": 7},
            "select a[href*='/track/']": lambda p: SEARCH_RESULTS,
            "select a[href*='/artist/']": lambda p: [],
            "navigate": lambda p: {"id": 7, "title": "Kesariya | Spotify", "url": p["url"]},
            "select": lambda p: {"done": True},
        }
    )


async def test_a_song_is_found_in_chrome_and_played_in_the_app() -> None:
    chrome, players = spotify_chrome(), FakePlayers()
    tool = MediaPlay(session(chrome), players=players, app=lambda: "spotify.desktop", poll_s=0.01)

    result = await tool.run(PlayArgs(query="Kesariya Arijit Singh"), CTX)

    assert result.summary == "Playing Kesariya by Arijit Singh in the Spotify app"
    assert players.opened == ["spotify:track:6VBhH7CyP56BXjp8VsDFPZ"]
    assert chrome.made("new_tab")[0]["url"] == (
        "https://open.spotify.com/search/Kesariya%20Arijit%20Singh/tracks"
    )
    assert chrome.made("close_tab") == [{"tab": 7}]  # the search tab is not left open


async def test_the_app_is_started_when_it_is_not_running() -> None:
    chrome, players = spotify_chrome(), FakePlayers(running=False)
    started: list[str] = []

    async def start(app: str) -> None:
        started.append(app)
        players.running = True

    tool = MediaPlay(
        session(chrome),
        players=players,
        app=lambda: "spotify.desktop",
        start_app=start,
        poll_s=0.01,
    )

    result = await tool.run(PlayArgs(query="liked songs"), CTX)

    assert started == ["spotify.desktop"]
    assert players.opened == [LIKED_URI]
    assert chrome.calls == []  # liked songs need no search
    assert result.status == "ok"


async def test_liked_songs_that_do_not_start_are_left_to_the_user() -> None:
    players = FakePlayers(plays=False)
    tool = MediaPlay(
        session(spotify_chrome()),
        players=players,
        app=lambda: "s.desktop",
        poll_s=0.01,
        playing_s=0.05,
    )

    result = await tool.run(PlayArgs(query="my liked songs"), CTX)

    assert result.summary.startswith("Opened your Liked Songs in the Spotify app")
    assert result.data is not None
    assert result.data["playing"] is False


async def test_without_the_app_chrome_plays_it() -> None:
    chrome = spotify_chrome()
    players = FakePlayers(running=False)

    async def chrome_plays() -> list[Player]:
        return [
            Player(
                "org.mpris.MediaPlayer2.chromium.instance1", "Chrome", "Playing", "Kesariya", "", ""
            )
        ]

    players.list = chrome_plays  # type: ignore[method-assign]
    tool = MediaPlay(session(chrome), players=players, app=lambda: None, poll_s=0.01)

    result = await tool.run(PlayArgs(query="Kesariya"), CTX)

    assert result.summary == "Playing Kesariya on open.spotify.com in Chrome"
    assert chrome.made("navigate")[0] == {
        "url": "https://open.spotify.com/track/6VBhH7CyP56BXjp8VsDFPZ",
        "tab": 7,
    }
    clicks = [p for p in chrome.made("select") if p.get("action") == "click"]
    assert "play-button" in clicks[0]["selector"]


async def test_nothing_found_and_app_missing_are_said() -> None:
    tool = MediaPlay(
        session(spotify_chrome()), players=FakePlayers(), app=lambda: None, poll_s=0.01
    )

    nothing = await tool.run(PlayArgs(query="zzzz", kind="artist"), CTX)
    no_app = await tool.run(PlayArgs(query="x", where="app"), CTX)

    assert nothing.error == "Spotify found no artist for “zzzz”"
    assert no_app.error is not None
    assert "not installed" in no_app.error


# --- WhatsApp -------------------------------------------------------------------------


def whatsapp_chrome(*, chat: str = "Mom", signed_in: bool = True) -> Chrome:
    composed: list[str] = []

    def compose(p: dict[str, Any]) -> Any:
        if p.get("action") == "type":
            composed.append(p["text"])
            return {"done": True}
        return [{"text": ""}]  # empty again once sent

    def side(p: dict[str, Any]) -> Any:
        return [{"text": ""}] if signed_in else []

    from arcee.tools import whatsapp as w

    return Chrome(
        {
            "tabs": lambda p: [{"id": 3, "url": "https://web.whatsapp.com/"}],
            f"select {w.SIGNED_IN}": side,
            f"select {w.SEARCH_BOX}": lambda p: {"done": True},
            f"select {w.CHAT_NAME}": lambda p: {"done": True, "text": chat},
            f"select {w.COMPOSE}": compose,
            f"select {w.OPEN_CHAT}": lambda p: [{"text": chat}],
            f"select {w.SEND}": lambda p: {"done": True},
        }
    )


async def test_a_message_is_sent_to_the_chat_of_that_name() -> None:
    chrome = whatsapp_chrome()
    tool = WhatsAppSend(session(chrome))

    result = await tool.run(SendArgs(to="Mom", text="I'll be home by 8"), CTX)

    assert result.summary == "Sent to Mom on WhatsApp"
    typed = [p["text"] for p in chrome.made("select") if p.get("action") == "type"]
    assert typed == ["Mom", "I'll be home by 8"]
    assert any(
        p.get("action") == "click" and "Send" in p["selector"] for p in chrome.made("select")
    )


async def test_a_near_match_is_left_as_a_draft() -> None:
    chrome = whatsapp_chrome(chat="Mom Office")
    tool = WhatsAppSend(session(chrome))

    result = await tool.run(SendArgs(to="Mom", text="hi"), CTX)

    assert result.summary == (
        "Typed the message in the chat with Mom Office, not sent: the chat that opened is "
        "“Mom Office”, not “Mom”"
    )
    assert not any("Send" in p.get("selector", "") for p in chrome.made("select"))


async def test_not_signed_in_says_how_to_link() -> None:
    tool = WhatsAppSend(session(whatsapp_chrome(signed_in=False)))

    result = await tool.run(SendArgs(to="Mom", text="hi"), CTX)

    assert result.error is not None
    assert "Linked devices" in result.error


def test_numbers_and_asking_first() -> None:
    tool = WhatsAppSend(session(Chrome({})))

    assert phone_number("98765 43210") == "919876543210"
    assert phone_number("+44 7700 900123") == "447700900123"
    assert phone_number("Mom") is None
    assert tool.tier_for(SendArgs(to="Mom", text="hi"), Policy()) is Tier.CONFIRM
    assert tool.tier_for(SendArgs(to="Mom", text="hi", send=False), Policy()) is Tier.AUTO


@pytest.mark.parametrize(
    ("request_text", "group"),
    [
        ("Send Mom 'I'll be home by 8' on WhatsApp.", "whatsapp.*"),
        ("Play Arijit Singh on Spotify.", "media.*"),
    ],
)
def test_requests_reach_the_tools(request_text: str, group: str) -> None:
    assert group in filters_for([Message("user", request_text)])
