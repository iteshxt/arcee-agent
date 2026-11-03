"""Music and video through MPRIS: stand-in players on a private D-Bus."""

import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusDict, DBusStr
from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_property, method

from arcee.tools.base import NoArgs, ToolContext
from arcee.tools.media import ControlArgs, MediaControl, MediaNowPlaying, Players

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon")
CTX = ToolContext("t_m", Path("/home/user"))


class Root(ServiceInterface):
    def __init__(self, identity: str) -> None:
        super().__init__("org.mpris.MediaPlayer2")
        self.identity = identity

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> DBusStr:
        return self.identity


class Player(ServiceInterface):
    def __init__(self, status: str, title: str, artist: str) -> None:
        super().__init__("org.mpris.MediaPlayer2.Player")
        self.status = status
        self.title = title
        self.artist = artist
        self.calls: list[str] = []

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> DBusStr:
        return self.status

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> DBusDict:
        return {
            "xesam:title": Variant("s", self.title),
            "xesam:artist": Variant("as", [self.artist]),
            "xesam:album": Variant("s", ""),
        }

    @method()
    def Pause(self) -> None:
        self.calls.append("Pause")
        self.status = "Paused"

    @method()
    def PlayPause(self) -> None:
        self.calls.append("PlayPause")
        self.status = "Paused" if self.status == "Playing" else "Playing"

    @method()
    def Next(self) -> None:
        self.calls.append("Next")
        self.title = "Next one"


@pytest.fixture
def bus_address() -> Iterator[str]:
    process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    address = process.stdout.readline().strip()
    yield address
    process.terminate()
    process.wait()
    process.stdout.close()


async def serve(address: str, name: str, identity: str, player: Player) -> MessageBus:
    bus = await MessageBus(bus_address=address).connect()
    bus.export("/org/mpris/MediaPlayer2", Root(identity))
    bus.export("/org/mpris/MediaPlayer2", player)
    await bus.request_name(f"org.mpris.MediaPlayer2.{name}")
    return bus


@pytest.fixture
async def two_players(bus_address: str) -> AsyncIterator[tuple[Player, Player]]:
    spotify = Player("Playing", "Tum Hi Ho", "Arijit Singh")
    chrome = Player("Paused", "lofi hip hop radio", "Lofi Girl")
    buses = [
        await serve(bus_address, "spotify", "Spotify", spotify),
        await serve(bus_address, "chromium.instance42", "Google Chrome", chrome),
    ]
    yield spotify, chrome
    for bus in buses:
        bus.disconnect()


async def test_what_is_playing(bus_address: str, two_players: tuple[Player, Player]) -> None:
    result = await MediaNowPlaying(Players(bus_address)).run(NoArgs(), CTX)

    assert result.summary == "Spotify: Tum Hi Ho by Arijit Singh (playing)"
    assert result.data is not None
    assert {p["app"] for p in result.data["players"]} == {"Spotify", "Google Chrome"}


async def test_pause_goes_to_the_player_that_is_playing(
    bus_address: str, two_players: tuple[Player, Player]
) -> None:
    spotify, chrome = two_players

    result = await MediaControl(Players(bus_address)).run(ControlArgs(action="pause"), CTX)

    assert spotify.calls == ["Pause"]
    assert chrome.calls == []
    assert result.summary == "Spotify: paused; now Spotify: Tum Hi Ho by Arijit Singh (paused)"


async def test_a_named_player(bus_address: str, two_players: tuple[Player, Player]) -> None:
    spotify, chrome = two_players

    result = await MediaControl(Players(bus_address)).run(
        ControlArgs(action="next", player="chrome"), CTX
    )

    assert chrome.calls == ["Next"]
    assert spotify.calls == []
    assert "Next one" in result.summary


async def test_no_player_open(bus_address: str) -> None:
    result = await MediaControl(Players(bus_address)).run(ControlArgs(action="toggle"), CTX)
    assert result.status != "ok"
    assert "no media player is open" in (result.error or "")

    empty = await MediaNowPlaying(Players(bus_address)).run(NoArgs(), CTX)
    assert empty.summary == "No media player is open"


def test_music_words_bring_the_media_tools() -> None:
    from arcee.core.toolsets import MEDIA, filters_for
    from arcee.models.base import Message

    for request in ("Pause the music.", "Next song", "What song is this?", "Pause YouTube"):
        assert set(MEDIA) <= set(filters_for([Message("user", request)])), request
    assert not set(MEDIA) & set(filters_for([Message("user", "Rename the photo")]))
