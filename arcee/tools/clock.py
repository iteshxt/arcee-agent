"""Clocks: the time in another place, and a stopwatch (the TIME tasks).

"What time is it in New York?" was a shell command (TZ=America/New_York date) the
model had to get right, zone name included. `time.in` takes the place as said: a city
in the time zone database's names ("New York", "Tokyo"), a common one it lacks
("Delhi", "San Francisco", "Bangalore"), a zone abbreviation (IST, PST), or else any
place Open-Meteo's geocoder knows (the one web.weather uses), which gives its zone.
It also converts a time: "3 pm in London is what here?".
"""

import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, ClassVar, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import httpx
from pydantic import Field

from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.reminders import when_at

GEOCODE: Final = "https://geocoding-api.open-meteo.com/v1/search"
# Places people name that are not zone names (the database names one city per zone).
ALIASES: Final = {
    "india": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "bombay": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "chennai": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "pune": "Asia/Kolkata",
    "ist": "Asia/Kolkata",
    "nyc": "America/New_York",
    "washington": "America/New_York",
    "boston": "America/New_York",
    "miami": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "eastern": "America/New_York",
    "san francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "california": "America/Los_Angeles",
    "silicon valley": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "texas": "America/Chicago",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "cst": "America/Chicago",
    "uk": "Europe/London",
    "england": "Europe/London",
    "gmt": "Etc/GMT",
    "utc": "UTC",
    "bst": "Europe/London",
    "cet": "Europe/Paris",
    "germany": "Europe/Berlin",
    "france": "Europe/Paris",
    "japan": "Asia/Tokyo",
    "jst": "Asia/Tokyo",
    "china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "uae": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "sydney": "Australia/Sydney",
    "australia": "Australia/Sydney",
    "aest": "Australia/Sydney",
    "canada": "America/Toronto",
    "nepal": "Asia/Kathmandu",
    "pakistan": "Asia/Karachi",
    "bangladesh": "Asia/Dhaka",
    "sri lanka": "Asia/Colombo",
    "singapore": "Asia/Singapore",
}


def _zone_names() -> dict[str, str]:
    """ "new york" -> "America/New_York", from the zone database's city names."""
    names: dict[str, str] = {}
    for zone in available_timezones():
        if "/" in zone and not zone.startswith(("Etc/", "SystemV/", "posix/", "right/")):
            names.setdefault(zone.rsplit("/", 1)[1].replace("_", " ").lower(), zone)
    return names


_zone_cache: dict[str, str] | None = None


def local_zone(place: str) -> str | None:
    """The zone for `place` without the network, or None."""
    global _zone_cache
    key = re.sub(r"\s+", " ", place.lower().strip().strip(".?!")).removesuffix(" time")
    if key in ALIASES:
        return ALIASES[key]
    if "/" in place:
        try:
            ZoneInfo(place.strip())
        except ZoneInfoNotFoundError, ValueError:
            return None
        return place.strip()
    if _zone_cache is None:
        _zone_cache = _zone_names()
    return _zone_cache.get(key) or _zone_cache.get(key.split(",")[0].strip())


async def geocoded_zone(place: str, client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    """(zone, "City, Country") from Open-Meteo's geocoder."""
    own = client is None
    http = client or httpx.AsyncClient(timeout=10, headers={"User-Agent": "Arcee"})
    try:
        response = await http.get(GEOCODE, params={"name": place, "count": 1})
        results = response.json().get("results") if response.status_code == 200 else None
    finally:
        if own:
            await http.aclose()
    if not results:
        raise LookupError(f"no place called “{place}”")
    spot: dict[str, Any] = results[0]
    name = ", ".join(str(x) for x in (spot.get("name"), spot.get("country")) if x)
    return str(spot["timezone"]), name


def _offset(moment: datetime) -> str:
    delta = moment.utcoffset() or timedelta()
    minutes = round(delta.total_seconds() / 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


class TimeInArgs(ToolArgs):
    place: str = Field(
        min_length=2, max_length=80, description='A city, country or zone: "New York", "IST"'
    )
    at: str = Field(
        default="",
        description='Convert this time instead of now: "15:00", "3 pm" (in from_place)',
    )
    from_place: str = Field(
        default="", description="Where `at` is; empty for here (this computer's time)"
    )


class TimeIn(Tool[TimeInArgs]):
    name = "time.in"
    description = "The time now in another city or time zone, or a time there converted."
    usage = Usage(
        when='"What time is it in New York?", "3 pm IST in London time?", "when it\'s 9 '
        'am in San Francisco, what time is it here?".',
        example='time.in(place="New York")',
    )
    args_model = TimeInArgs
    read_only: ClassVar[bool] = True
    timeout_s = 15

    def __init__(
        self,
        now: Callable[[], datetime] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now().astimezone())
        self._client = client

    async def _zone(self, place: str) -> tuple[ZoneInfo, str]:
        if (zone := local_zone(place)) is not None:
            return ZoneInfo(zone), place.strip()
        zone, name = await geocoded_zone(place, self._client)
        return ZoneInfo(zone), name

    async def run(self, args: TimeInArgs, ctx: ToolContext) -> ToolResult:
        try:
            there, there_name = await self._zone(args.place)
            if args.from_place:
                source, source_name = await self._zone(args.from_place)
            else:
                source, source_name = None, "here"
        except (LookupError, httpx.HTTPError) as exc:
            return ToolResult.fail(f"could not find that place's time zone: {exc}")
        now = self._now()
        if args.at:
            base = now.astimezone(source) if source is not None else now
            try:
                moment = when_at(args.at, base.replace(hour=0, minute=0, second=0))
            except ValueError as exc:
                return ToolResult.fail(str(exc))
        else:
            moment = now
        local = moment.astimezone(there)
        here = moment.astimezone()
        day = ""
        if local.date() != here.date():
            day = " (the next day)" if local.date() > here.date() else " (the day before)"
        said = f"{local:%H:%M} {local:%a %d %b} in {there_name}{day}"
        data: dict[str, Any] = {
            "place": there_name,
            "time": f"{local:%H:%M}",
            "date": local.date().isoformat(),
            "zone": str(there.key),
            "offset": _offset(local),
        }
        if args.at:
            data["from"] = f"{moment:%H:%M} {source_name}"
            data["here"] = f"{here:%H:%M} {here:%a %d %b}"
        return ToolResult.ok(said, data=data)


# --- time.stopwatch -------------------------------------------------------------------------


class StopwatchArgs(ToolArgs):
    action: Literal["start", "lap", "stop", "status", "reset"] = Field(
        description="start (or resume), lap, stop (pause), status, reset"
    )


def _spoken(seconds: float) -> str:
    whole = round(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min {secs} s"
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


class Stopwatch(Tool[StopwatchArgs]):
    name = "time.stopwatch"
    description = "A stopwatch Arcee keeps: start, lap, stop, how long, reset."
    usage = Usage(
        when='"Start a stopwatch", "how long since I started?", "lap", "stop the stopwatch".',
        avoid="Counting down (reminder.add with in_minutes); checking on something again "
        "later (control.set_timer).",
        example='time.stopwatch(action="start")',
    )
    args_model = StopwatchArgs

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started: float | None = None  # running since
        self._before = 0.0  # time run before the last start
        self._laps: list[float] = []

    def _elapsed(self) -> float:
        running = self._clock() - self._started if self._started is not None else 0.0
        return self._before + running

    async def run(self, args: StopwatchArgs, ctx: ToolContext) -> ToolResult:
        if args.action == "start":
            if self._started is None:
                self._started = self._clock()
        elif args.action == "lap":
            if self._started is None:
                return ToolResult.fail("the stopwatch is not running")
            self._laps.append(self._elapsed())
        elif args.action == "stop":
            self._before = self._elapsed()
            self._started = None
        elif args.action == "reset":
            self._started, self._before, self._laps = None, 0.0, []
            return ToolResult.ok("Stopwatch reset", data={"seconds": 0})
        elapsed = self._elapsed()
        state = "running" if self._started is not None else "stopped"
        data: dict[str, Any] = {"seconds": round(elapsed, 1), "state": state}
        if self._laps:
            data["laps"] = [_spoken(lap) for lap in self._laps]
        return ToolResult.ok(f"Stopwatch {state}: {_spoken(elapsed)}", data=data)


def clock_tools() -> list[Tool[Any]]:
    return [TimeIn(), Stopwatch()]
