"""Music and video: play, pause, skip, and what is playing, through MPRIS.

Every player on the desktop that plays sound (Spotify, Chrome's tabs, VLC, Rhythmbox)
announces itself on the session bus as org.mpris.MediaPlayer2.<name> and takes the
same commands. So "pause", "next song" and "what's playing?" need no screen, no
window and no clicks: one D-Bus call. Without a player named, the one playing now
is meant, else the one paused most recently (the last in the bus's list).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from pydantic import Field

from arcee.tools.base import NoArgs, Tool, ToolArgs, ToolContext, ToolResult, Usage

PREFIX: Final = "org.mpris.MediaPlayer2."
PATH: Final = "/org/mpris/MediaPlayer2"
ROOT: Final = "org.mpris.MediaPlayer2"
PLAYER: Final = "org.mpris.MediaPlayer2.Player"
PROPERTIES: Final = "org.freedesktop.DBus.Properties"
# The commands, as MPRIS names them.
METHODS: Final = {
    "play": "Play",
    "pause": "Pause",
    "toggle": "PlayPause",
    "next": "Next",
    "previous": "Previous",
    "stop": "Stop",
}


class MediaError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Player:
    bus_name: str
    app: str  # what the player calls itself: "Spotify", "Google Chrome"
    status: str  # Playing, Paused, Stopped
    title: str
    artist: str
    album: str

    def describe(self) -> str:
        what = " by ".join(x for x in (self.title, self.artist) if x) or "nothing"
        return f"{self.app}: {what} ({self.status.lower()})"

    def public(self) -> dict[str, str]:
        return {
            "app": self.app,
            "status": self.status,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
        }


class Players:
    """The players on the session bus (another bus in tests)."""

    def __init__(self, bus_address: str | None = None) -> None:
        self._address = bus_address

    async def _connect(self) -> MessageBus:
        if self._address:
            return await MessageBus(bus_address=self._address).connect()
        return await MessageBus(bus_type=BusType.SESSION).connect()

    async def list(self) -> list[Player]:
        bus = await self._connect()
        try:
            reply = await _call(
                bus,
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ListNames",
            )
            names = [n for n in reply[0] if isinstance(n, str) and n.startswith(PREFIX)]
            return [await _player(bus, name) for name in names]
        finally:
            bus.disconnect()

    async def open_uri(self, bus_name: str, uri: str) -> None:
        """Ask a player to open and play `uri` (Spotify: spotify:track:<id>)."""
        bus = await self._connect()
        try:
            await _call(bus, bus_name, PATH, PLAYER, "OpenUri", "s", (uri,))
        finally:
            bus.disconnect()

    async def command(self, player: Player, method: str) -> None:
        bus = await self._connect()
        try:
            await _call(bus, player.bus_name, PATH, PLAYER, method)
        finally:
            bus.disconnect()


async def _call(
    bus: MessageBus,
    dest: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: Sequence[Any] = (),
) -> list[Any]:
    reply = await bus.call(
        Message(
            destination=dest,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=list(body),
        )
    )
    if reply.message_type is MessageType.ERROR:
        detail = reply.body[0] if reply.body else "no reply"
        raise MediaError(str(detail))
    return list(reply.body)


async def _get(bus: MessageBus, name: str, interface: str, prop: str) -> Any:
    try:
        reply = await _call(bus, name, PATH, PROPERTIES, "Get", "ss", (interface, prop))
    except MediaError:
        return None
    value = reply[0]
    return value.value if isinstance(value, Variant) else value


async def _player(bus: MessageBus, name: str) -> Player:
    app = await _get(bus, name, ROOT, "Identity") or name.removeprefix(PREFIX).split(".")[0]
    status = await _get(bus, name, PLAYER, "PlaybackStatus") or "Stopped"
    metadata: dict[str, Any] = await _get(bus, name, PLAYER, "Metadata") or {}

    def field(key: str) -> str:
        value: Any = metadata.get(key)
        value = value.value if isinstance(value, Variant) else value
        if isinstance(value, list):
            return ", ".join(str(v) for v in cast(list[Any], value))  # artists
        return str(value) if value else ""

    return Player(
        name,
        str(app),
        str(status),
        field("xesam:title"),
        field("xesam:artist"),
        field("xesam:album"),
    )


def choose(players: list[Player], wanted: str = "") -> Player | None:
    """The player meant: the one named, else the one playing, else the last paused."""
    if wanted:
        key = wanted.lower()
        named = [p for p in players if key in p.app.lower() or key in p.bus_name.lower()]
        return named[0] if named else None
    for status in ("Playing", "Paused"):
        matching = [p for p in players if p.status == status]
        if matching:
            return matching[-1]
    return players[-1] if players else None


# --- media.control -----------------------------------------------------------------


class ControlArgs(ToolArgs):
    action: Literal["play", "pause", "toggle", "next", "previous", "stop"] = Field(
        description="What to do: play, pause, toggle (play/pause), next, previous or stop"
    )
    player: str = Field(
        default="",
        description="An app the user named (spotify, chrome, vlc); empty for the one playing",
    )


class MediaControl(Tool[ControlArgs]):
    name = "media.control"
    # Runs without asking (Tier.AUTO), as pressing a media key would; not read_only, so
    # "next, next" is never taken for a repeat.
    description = (
        "Play, pause, skip to the next or previous track, or stop music and video in any "
        "player (Spotify, YouTube in Chrome, VLC), without the screen."
    )
    usage = Usage(
        when='"Pause the music", "next song", "resume", "stop the video", "play" (what was '
        "playing before).",
        avoid="Starting something new to play (a song, a video): open it in its app or site.",
        mistakes="Clicking the player's buttons on screen instead.",
        example='media.control(action="next")',
    )
    args_model = ControlArgs
    timeout_s = 10

    def __init__(self, players: Players | None = None) -> None:
        self._players = players or Players()

    async def run(self, args: ControlArgs, ctx: ToolContext) -> ToolResult:
        try:
            players = await self._players.list()
            player = choose(players, args.player)
            if player is None:
                if args.player:
                    return ToolResult.fail(f"no player called {args.player!r} is open")
                return ToolResult.fail("nothing is playing or paused (no media player is open)")
            await self._players.command(player, METHODS[args.action])
            after = choose(await self._players.list(), player.app) or player
        except (MediaError, OSError) as exc:
            return ToolResult.fail(f"the media player did not answer: {exc}")
        past = {"toggle": "toggled", "next": "skipped to the next", "previous": "went back"}
        done = past.get(args.action, f"{args.action}{'d' if args.action.endswith('e') else 'ed'}")
        return ToolResult.ok(f"{player.app}: {done}; now {after.describe()}", data=after.public())


# --- media.now_playing --------------------------------------------------------------


class MediaNowPlaying(Tool[NoArgs]):
    name = "media.now_playing"
    read_only = True
    description = (
        'The song or video playing now ("what song is this?"), and what each media player '
        "has: app, title, artist, album, and state."
    )
    usage = Usage(
        when='"What song is this?", "who sings this?", "is anything playing?"',
        avoid="Recognising music from the microphone: it only knows what players report.",
        example="media.now_playing()",
    )
    args_model = NoArgs
    timeout_s = 10

    def __init__(self, players: Players | None = None) -> None:
        self._players = players or Players()

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        try:
            players = await self._players.list()
        except (MediaError, OSError) as exc:
            return ToolResult.fail(f"could not ask the media players: {exc}")
        if not players:
            return ToolResult.ok("No media player is open", data={"players": []})
        current = choose(players)
        assert current is not None
        data = {"current": current.public(), "players": [p.public() for p in players]}
        return ToolResult.ok(current.describe(), data=data)
