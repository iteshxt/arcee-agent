"""`media.play`: play a song, artist, album or playlist on Spotify (MED-003).

Spotify's Web API needs the user's own developer keys, so this goes the way a person
would. The item is found with open.spotify.com's search in the user's Chrome (the
first link of the kind asked for). Then:

- with the Spotify app installed, the app plays it: MPRIS's OpenUri with its
  spotify: address, starting the app first when it is not running;
- otherwise the item's page is opened in Chrome and its Play button pressed.

Either way the result says what is playing, read back from the player (MPRIS).
"""

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, Protocol
from urllib.parse import quote

from pydantic import Field

from arcee.browser.client import BrowserError
from arcee.tools.base import ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.browser import BrowserSession, BrowserTool
from arcee.tools.media import MediaError, Player, Players

SEARCH: Final = "https://open.spotify.com/search/{q}/{section}"
SECTIONS: Final = {
    "song": "tracks",
    "artist": "artists",
    "album": "albums",
    "playlist": "playlists",
}
PATHS: Final = {"song": "track", "artist": "artist", "album": "album", "playlist": "playlist"}
LIKED: Final = frozenset(
    {"liked songs", "my liked songs", "liked", "my likes", "my favourites", "my favorites"}
)
LIKED_URI: Final = "spotify:collection:tracks"
LIKED_URL: Final = "https://open.spotify.com/collection/tracks"
PLAY_BUTTON: Final = (
    "[data-testid='action-bar-row'] [data-testid='play-button'], button[data-testid='play-button']"
)
APP_FILES: Final = (
    "/var/lib/snapd/desktop/applications/spotify_spotify.desktop",
    "/var/lib/flatpak/exports/share/applications/com.spotify.Client.desktop",
    "~/.local/share/flatpak/exports/share/applications/com.spotify.Client.desktop",
    "/usr/share/applications/spotify.desktop",
)
APP_START_S: Final = 25.0
PLAYING_S: Final = 8.0


class PlayerBus(Protocol):
    """The desktop's players (MPRIS), as media.play uses them; a fake in tests."""

    async def list(self) -> list[Player]: ...
    async def open_uri(self, bus_name: str, uri: str) -> None: ...
    async def command(self, player: Player, method: str) -> None: ...


def spotify_app() -> str | None:
    """The Spotify app's .desktop file (snap, Flatpak or .deb), if it is installed."""
    for name in APP_FILES:
        path = Path(os.path.expanduser(name))
        if path.exists():
            return str(path)
    return None


async def launch(desktop_file: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "gio",
        "launch",
        desktop_file,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    await process.wait()


class PlayArgs(ToolArgs):
    query: str = Field(
        min_length=1,
        description='What to play: "Kesariya", "Arijit Singh", "liked songs" for their likes',
    )
    kind: Literal["song", "artist", "album", "playlist"] = "song"
    where: Literal["auto", "app", "web"] = Field(
        default="auto", description="auto: the Spotify app when installed, else Chrome"
    )


class MediaPlay(BrowserTool[PlayArgs]):
    name = "media.play"
    description = (
        "Play a song, artist, album or playlist on Spotify: in the Spotify app when it is "
        "installed, else on open.spotify.com in Chrome. Says what is playing."
    )
    usage = Usage(
        when='"Play Arijit Singh on Spotify", "play Blinding Lights", "put on my liked '
        'songs", "play some lo-fi playlist".',
        avoid="Pause, skip or resume what is already playing: media.control.",
        example='media.play(query="Arijit Singh", kind="artist")',
    )
    args_model = PlayArgs
    observation_kind = "text"
    untrusted: ClassVar[bool] = True  # titles from Spotify's pages
    timeout_s = 75

    def __init__(
        self,
        session: BrowserSession,
        *,
        players: PlayerBus | None = None,
        app: Callable[[], str | None] = spotify_app,
        start_app: Callable[[str], Any] = launch,
        poll_s: float = 0.5,
        playing_s: float = PLAYING_S,
    ) -> None:
        super().__init__(session)
        self._players: PlayerBus = players or Players()
        self._app = app
        self._start_app = start_app
        self._poll_s = poll_s
        self._playing_s = playing_s

    async def run(self, args: PlayArgs, ctx: ToolContext) -> ToolResult:
        app = None if args.where == "web" else self._app()
        if args.where == "app" and app is None:
            return ToolResult.fail("the Spotify app is not installed; where='web' plays in Chrome")
        liked = " ".join(args.query.lower().split()) in LIKED
        try:
            if liked:
                uri, url, label = LIKED_URI, LIKED_URL, "your Liked Songs"
            else:
                found = await self._find(args, keep_tab=app is None)
                if found is None:
                    return ToolResult.fail(f"Spotify found no {args.kind} for “{args.query}”")
                uri, url, label = found
            if app is not None:
                return await self._in_app(app, uri, label)
            return await self._in_chrome(url, label)
        except BrowserError as exc:
            return ToolResult.fail(f"Chrome could not search Spotify: {exc}")
        except MediaError as exc:
            return ToolResult.fail(f"the player refused: {exc}")

    async def _find(self, args: PlayArgs, *, keep_tab: bool) -> tuple[str, str, str] | None:
        """The first `kind` Spotify's search lists: (spotify: address, page, its name)."""
        search = SEARCH.format(q=quote(args.query), section=SECTIONS[args.kind])
        tab = (await self._call("new_tab", url=search))["id"]
        try:
            links: list[dict[str, str]] = await self._call(
                "select",
                tab=tab,
                selector=f"a[href*='/{PATHS[args.kind]}/']",
                wait_ms=10_000,
                limit=3,
            )
        finally:
            if not keep_tab:
                await self._call("close_tab", tab=tab)
        if keep_tab:
            self.session.tab = tab
        for link in links:
            href = link.get("href", "")
            item = href.split(f"/{PATHS[args.kind]}/", 1)[-1].split("?")[0].split("/")[0]
            if item:
                url = f"https://open.spotify.com/{PATHS[args.kind]}/{item}"
                return f"spotify:{PATHS[args.kind]}:{item}", url, link.get("text") or args.query
        return None

    async def _spotify(self) -> Player | None:
        players = await self._players.list()
        return next((p for p in players if "spotify" in p.bus_name.lower()), None)

    async def _in_app(self, app: str, uri: str, label: str) -> ToolResult:
        player = await self._spotify()
        if player is None:
            await self._start_app(app)
            player = await self._wait(lambda p: p is not None, APP_START_S)
            if player is None:
                return ToolResult.fail("started Spotify, but it did not come up; is it signed in?")
            await asyncio.sleep(self._poll_s * 3)  # it answers MPRIS before it can play
        before = (player.status, player.title)
        await self._players.open_uri(player.bus_name, uri)

        def started(p: Player | None) -> bool:
            # Something already playing is still "Playing": wait for the new title.
            return p is not None and p.status == "Playing" and (p.status, p.title) != before

        playing = await self._wait(started, self._playing_s)
        if playing is None and uri == LIKED_URI:
            # Spotify opens Liked Songs from outside but will not start them.
            return ToolResult.ok(
                "Opened your Liked Songs in the Spotify app; Spotify does not let them be "
                "started from outside, so the user presses Play there",
                data={"app": "Spotify", "opened": label, "playing": False},
            )
        now = await self._spotify()
        if playing is None and now is not None and now.status == "Playing":
            if label.lower()[:20] not in now.title.lower():
                return ToolResult.fail(
                    f"Spotify kept playing “{now.title}”; it did not start {label}"
                )
            playing = now  # the same song asked for again
        if playing is None and (player := await self._spotify()) is not None:
            await self._players.command(player, "Play")
            playing = await self._wait(lambda p: p is not None and p.status == "Playing", 3.0)
        if playing is None:
            return ToolResult.fail(
                f"Spotify opened {label} but is not playing: it may need signing in, or "
                "Premium for that item"
            )
        what = " by ".join(x for x in (playing.title, playing.artist) if x) or label
        return ToolResult.ok(
            f"Playing {what} in the Spotify app",
            data={"app": "Spotify", "opened": label, **playing.public()},
        )

    async def _in_chrome(self, url: str, label: str) -> ToolResult:
        # Its own tab (the search's, or a new one): never the page the user is on.
        tab = self.session.tab
        if tab is None:
            tab = self.session.tab = (await self._call("new_tab", url=url))["id"]
        page = await self._call("navigate", url=url, tab=tab)
        await self.session.bring_forward(str(page.get("title", "")))
        pressed = await self._call(
            "select", tab=tab, selector=PLAY_BUTTON, action="click", wait_ms=10_000
        )
        if not pressed.get("done"):
            return ToolResult.fail(
                f"opened {label} in Chrome, but found no Play button: Spotify may want "
                "the user to sign in"
            )

        def chrome_playing(player: Player | None) -> bool:
            return player is not None and player.status == "Playing"

        playing = await self._wait(chrome_playing, self._playing_s, chrome=True)
        if playing is None:
            return ToolResult.ok(
                f"Opened {label} on open.spotify.com in Chrome and pressed Play; nothing is "
                "playing yet (Spotify may want the user to sign in)",
                data={"app": "Chrome", "opened": label, "url": url, "playing": False},
            )
        return ToolResult.ok(
            f"Playing {playing.title or label} on open.spotify.com in Chrome",
            data={"app": "Chrome", "opened": label, "url": url, **playing.public()},
        )

    async def _wait(
        self, ok: Callable[[Player | None], bool], seconds: float, *, chrome: bool = False
    ) -> Player | None:
        """The Spotify (or Chrome) player once `ok` holds for it, within `seconds`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            if chrome:
                players = await self._players.list()
                found = next((p for p in players if "chrom" in p.bus_name.lower() and ok(p)), None)
            else:
                found = await self._spotify()
                found = found if ok(found) else None
            if found is not None or loop.time() >= deadline:
                return found
            await asyncio.sleep(self._poll_s)
