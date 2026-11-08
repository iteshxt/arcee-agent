"""`travel.search`: trains between two stations in India, and a flight search to open.

Trains come from erail.in's public timetable pages: every train between two stations
(and the stations next to them), with its times, the days it runs and its classes.
Station names are matched against erail's own station list, kept for a month. Seats and
fares need IRCTC, which needs the user's login and a captcha, so Arcee lists the trains
and leaves booking to the user (SHOP-005; the payment rule, SHOP-007, holds anyway).

Flights have no free timetable to read; the tool gives the Google Flights search for
the route and date, for browser.navigate when the user wants prices.
"""

import asyncio
import itertools
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, ClassVar, Final, Literal
from urllib.parse import quote

import httpx
from pydantic import Field

from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.web import USER_AGENT

STATIONS_URL: Final = "https://erail.in/js/cmp/stations.js"
TRAINS_URL: Final = "https://erail.in/trains-between-stations/{a}-{A}/{b}-{B}"
IRCTC: Final = "https://www.irctc.co.in/nget/train-search"
FLIGHTS: Final = "https://www.google.com/travel/flights?q={q}"
STATIONS_FRESH_S: Final = 30 * 86400.0
MAX_TRAINS: Final = 15
WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# What people say for a city with many stations: its main one (erail adds the others
# nearby to every search anyway).
CITY_STATIONS: Final = {
    "delhi": "NDLS",
    "new delhi": "NDLS",
    "mumbai": "CSMT",
    "bombay": "CSMT",
    "bangalore": "SBC",
    "bengaluru": "SBC",
    "kolkata": "HWH",
    "calcutta": "HWH",
    "chennai": "MAS",
    "madras": "MAS",
    "hyderabad": "SC",
    "secunderabad": "SC",
    "ahmedabad": "ADI",
    "jaipur": "JP",
    "lucknow": "LKO",
    "chandigarh": "CDG",
    "amritsar": "ASR",
    "pune": "PUNE",
    "agra": "AGC",
    "varanasi": "BSB",
    "banaras": "BSB",
    "shimla": "SML",
    "kalka": "KLK",
    "jammu": "JAT",
    "goa": "MAO",
    "patna": "PNBE",
    "bhopal": "BPL",
}


# Hill stations reached by a mountain railway from a railhead: no train runs to them
# from the plains. A search with no direct train goes by these stations instead (the
# last is the railhead; the ones before it lead there), leg by leg.
RAILHEADS: Final[dict[str, tuple[tuple[str, ...], str]]] = {
    "SML": (
        ("CDG", "KLK"),
        "the Kalka-Shimla toy train; buses and taxis also run from Chandigarh to Shimla",
    ),
    "UAM": (("CBE", "MTP"), "the Nilgiri Mountain Railway from Mettupalayam"),
    "DJ": (("NJP",), "the Darjeeling Himalayan Railway from New Jalpaiguri"),
    "MAE": (("NRL",), "the Matheran toy train from Neral"),
}
LEG_TRAINS: Final = 6


@dataclass(frozen=True, slots=True)
class Station:
    code: str
    name: str


def parse_stations(script: str) -> list[Station]:
    """erail's list: `var StationsData="<version>,<n>,CODE,Name,CODE,Name,..."`."""
    found = re.search(r'StationsData\s*=\s*"([^"]*)"', script)
    if not found:
        return []
    parts = found.group(1).split(",")[2:]
    return [
        Station(code.strip(), name.strip())
        for code, name in zip(parts[::2], parts[1::2], strict=False)
        if code.strip() and name.strip()
    ]


def first_names(stations: list[Station]) -> dict[str, Station]:
    """Each code's own station. erail lists places without a line under their railhead's
    code too (SML: Shimla, then Kullu and Manali): the first is the station's name."""
    by_code: dict[str, Station] = {}
    for station in stations:
        by_code.setdefault(station.code, station)
    return by_code


def match_station(wanted: str, stations: list[Station]) -> Station | None:
    """The station `wanted` names: its code, a city's main station, its exact name, the
    junction of that name, then names starting with it, the shortest first."""
    key = " ".join(wanted.lower().replace("station", "").replace("railway", "").split())
    if not key:
        return None
    by_code = first_names(stations)
    if wanted.strip().upper() in by_code:
        return by_code[wanted.strip().upper()]
    if key in CITY_STATIONS and CITY_STATIONS[key] in by_code:
        return by_code[CITY_STATIONS[key]]
    tests: tuple[Callable[[str], bool], ...] = (
        lambda name: name == key,
        lambda name: name in (f"{key} jn", f"{key} junction", f"{key} cantt"),
        lambda name: name.startswith(key + " "),
        lambda name: key in name,
    )
    for test in tests:
        hits = [s for s in stations if test(s.name.lower())]
        if hits:
            return min(hits, key=lambda s: len(s.name))
    return None


@dataclass(frozen=True, slots=True)
class Train:
    number: str
    name: str
    source: str  # station codes: this train's own, near the ones asked for
    dest: str
    departs: str  # HH:MM
    arrives: str
    days_later: int  # arrival, days after departure
    duration: str
    km: str
    runs: str  # "1111111", Monday first
    kind: str
    classes: tuple[str, ...]

    def runs_on(self, day: date) -> bool:
        return len(self.runs) == 7 and self.runs[day.weekday()] == "1"

    def days(self) -> str:
        if self.runs == "1111111":
            return "daily"
        return " ".join(d for d, bit in zip(WEEKDAYS, self.runs, strict=False) if bit == "1")


_TRAIN: Final = re.compile(r"data-train='([^']*)'(.*?)(?=data-train=|$)", re.DOTALL)
_CLASSES: Final = re.compile(r"class='tableclass'>(.*?)</table>", re.DOTALL)


def parse_trains(page: str) -> list[Train]:
    """The trains on an erail between-stations page, from each one's `data-train`."""
    trains: list[Train] = []
    for raw, rest in _TRAIN.findall(page):
        f = unescape(raw).split("_")
        if len(f) < 16 or not f[0].isdigit():
            continue
        try:
            later = int(f[6][:2]) - int(f[4][:2])
        except ValueError:
            later = 0
        found = _CLASSES.search(rest)
        classes = tuple(re.findall(r"<td>([0-9A-Z]{1,3})", found.group(1))) if found else ()
        trains.append(
            Train(
                number=f[0],
                name=f[1].title(),
                source=f[2],
                dest=f[3],
                departs=f[5],
                arrives=f[7],
                days_later=max(0, later),
                duration=f[8].replace(".", ":").replace(" hr", " h"),
                km=f[9],
                runs=f[13],
                kind=f[15],
                classes=classes,
            )
        )
    return trains


class TravelArgs(ToolArgs):
    mode: Literal["train", "flight"] = "train"
    origin: str = Field(min_length=2, description='From: a city or station, "Ludhiana", "LDH"')
    destination: str = Field(min_length=2, description='To: "New Delhi", "NDLS"')
    day: str = Field(
        default="",
        description="The date: YYYY-MM-DD, or today, tomorrow, a weekday (Saturday: the "
        "coming one) or weekend (the coming Saturday); empty: today",
    )
    after: str = Field(default="", description="Leaving at or after HH:MM, e.g. 05:00 for morning")
    before: str = Field(default="", description="Leaving before HH:MM")


class TravelSearch(Tool[TravelArgs]):
    name = "travel.search"
    description = (
        "Train timetable between two stations in India on a day: every train's number, "
        "name, times, duration, the days it runs, classes. For flights, the Google Flights "
        "search to open."
    )
    usage = Usage(
        when='"Trains from Ludhiana to Delhi tomorrow morning", "is there a train to Shimla '
        'on Saturday", "flights to Goa next Friday", the trains part of planning a trip. '
        "Use it instead of web.search for train schedules.",
        avoid="Booking: IRCTC needs the user's login and a captcha; give them the link.",
        mistakes="Saying seats are available or quoting fares: the timetable has neither.",
        example='travel.search(origin="Ludhiana", destination="New Delhi", day="2026-11-09", '
        'after="05:00", before="12:00")',
    )
    args_model = TravelArgs
    read_only: ClassVar[bool] = True
    timeout_s = 40

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        cache: Path | None = None,
        today: Any = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._today = today or date.today

    async def run(self, args: TravelArgs, ctx: ToolContext) -> ToolResult:
        day = parse_day(args.day, self._today())
        if day is None:
            return ToolResult.fail(f"“{args.day}” is not a date; give YYYY-MM-DD")
        if day < self._today():
            return ToolResult.fail(
                f"{day:%a %d %b} is past; today is {self._today():%a %d %b %Y}: give a date "
                "from today on (a weekend means the coming Saturday and Sunday)"
            )
        for edge in (args.after, args.before):
            if edge and not re.fullmatch(r"\d{2}:\d{2}", edge):
                return ToolResult.fail(f"“{edge}” is not a time; give HH:MM")
        if args.mode == "flight":
            return self._flights(args, day)
        client = self._client or httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        try:
            return await self._trains(client, args, day)
        except httpx.HTTPError as exc:
            return ToolResult.fail(f"the timetable could not be read: {type(exc).__name__}")
        finally:
            if self._client is None:
                await client.aclose()

    def _flights(self, args: TravelArgs, day: date) -> ToolResult:
        query = f"Flights from {args.origin} to {args.destination} on {day.isoformat()}"
        link = FLIGHTS.format(q=quote(query))
        return ToolResult.ok(
            f"Flight search for {args.origin} to {args.destination} on {day:%a %d %b}",
            data={
                "link": link,
                "note": "Times and prices are on that page: open it with browser.navigate "
                "if the user wants them. Arcee does not book.",
            },
        )

    async def _trains(self, client: httpx.AsyncClient, args: TravelArgs, day: date) -> ToolResult:
        stations = await self._stations(client)
        if not stations:
            return ToolResult.fail("the station list could not be read")
        ends: list[Station] = []
        for wanted in (args.origin, args.destination):
            station = match_station(wanted, stations)
            if station is None:
                return ToolResult.fail(f"no railway station matches “{wanted}”")
            ends.append(station)
        start, end = ends
        known = first_names(stations)
        trains, url, any_day = await self._between(client, start, end, day, args.after, args.before)
        if trains is None:
            return ToolResult.fail("the timetable could not be read")
        window = " ".join(
            x
            for x in (args.after and f"after {args.after}", args.before and f"before {args.before}")
            if x
        )
        route = f"{start.name} ({start.code}) to {end.name} ({end.code}) on {day:%a %d %b}"
        data: dict[str, Any] = {
            "trains": [_listed(t, known) for t in trains[:MAX_TRAINS]],
            "timetable": url,
            "book": IRCTC,
            "note": "Timetable only: seats and fares are on IRCTC, which needs the user's "
            "login; Arcee does not book.",
        }
        if len(trains) > MAX_TRAINS:
            data["more"] = f"{len(trains) - MAX_TRAINS} more on the timetable page"
        summary = f"{len(trains)} trains from {route}" + (f", leaving {window}" if window else "")
        if trains:
            return ToolResult.ok(summary, data=data)
        # To a hill station: by its feeder junction and railhead; from one, the other way.
        hill = RAILHEADS.get(end.code) or RAILHEADS.get(start.code)
        if hill is not None and not {start.code, end.code} & set(hill[0]):
            stops = [known[code] for code in hill[0] if code in known]
            if end.code not in RAILHEADS:
                stops.reverse()  # leaving the hills: the railhead first
            chains = [
                stops[-k:] if end.code in RAILHEADS else stops[:k] for k in range(1, len(stops) + 1)
            ]
            legs = await self._legs(client, start, end, chains, known, day, args)
            if legs:
                del data["trains"]
                data["legs"] = legs
                data["how"] = f"No direct train: change on the way; the hill leg is {hill[1]}."
                via = " and ".join(str(leg["from"]) for leg in legs[1:])
                summary = f"No direct train from {route}; by {via}: " + "; ".join(
                    f"{leg['from']} to {leg['to']}: "
                    + ", ".join(
                        f"{t['train'].split()[0]} at {t['departs']}" for t in leg["trains"][:4]
                    )
                    for leg in legs
                )
                return ToolResult.ok(summary, data=data)
        data["note"] = (
            "No direct train "
            + ("that day. " if any_day else "between these stations. ")
            + "Try a big junction near either end (search each leg), or another day. "
            + data["note"]
        )
        return ToolResult.ok(summary, data=data)

    async def _between(
        self,
        client: httpx.AsyncClient,
        start: Station,
        end: Station,
        day: date,
        after: str = "",
        before: str = "",
    ) -> tuple[list[Train] | None, str, bool]:
        """The trains from `start` to `end` on `day` (in the window), the timetable's
        address, and whether any train runs there on some day."""
        url = TRAINS_URL.format(a=_slug(start.name), A=start.code, b=_slug(end.name), B=end.code)
        response = await client.get(url)
        if response.status_code >= 400:
            return None, url, False
        every = parse_trains(response.text)
        trains = [
            t
            for t in every
            if t.runs_on(day)
            and (not after or t.departs >= after)
            and (not before or t.departs < before)
        ]
        trains.sort(key=lambda t: t.departs)
        return trains, url, bool(every)

    async def _legs(
        self,
        client: httpx.AsyncClient,
        start: Station,
        end: Station,
        chains: list[list[Station]],
        known: dict[str, Station],
        day: date,
        args: TravelArgs,
    ) -> list[dict[str, Any]]:
        """The journey by the first of `chains` (the stations to change at, fewest first)
        whose every leg has a train that day, leg by leg; empty when none has."""
        for chain in chains:
            path = [start, *chain, end]
            legs: list[dict[str, Any]] = []
            for n, (a, b) in enumerate(itertools.pairwise(path)):
                first = n == 0
                trains, url, _ = await self._between(
                    client, a, b, day, args.after if first else "", args.before if first else ""
                )
                if not trains:
                    break
                legs.append(
                    {
                        "from": f"{a.name} ({a.code})",
                        "to": f"{b.name} ({b.code})",
                        "trains": [_listed(t, known) for t in trains[:LEG_TRAINS]],
                        "timetable": url,
                    }
                )
            else:
                return legs
        return []

    async def _stations(self, client: httpx.AsyncClient) -> list[Station]:
        cache = self._cache or _cache_path()
        fresh = await asyncio.to_thread(_fresh, cache)
        if fresh is not None:
            return parse_stations(fresh)
        response = await client.get(STATIONS_URL, params={"dt": datetime.now().strftime("%Y%m%d")})
        response.raise_for_status()
        if parse_stations(response.text):
            await asyncio.to_thread(_save, cache, response.text)
        return parse_stations(response.text)


_WEEKDAY_NAMES: Final = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def parse_day(text: str, today: date) -> date | None:
    """The date a request names: YYYY-MM-DD, today, tomorrow, a weekday ("Saturday",
    "next Friday": the coming one), or the weekend (the coming Saturday)."""
    words = " ".join(text.lower().replace(",", " ").split())
    if not words or words == "today":
        return today
    try:
        return date.fromisoformat(words)
    except ValueError:
        pass
    if words in ("tomorrow", "tmrw"):
        return today + timedelta(days=1)
    if words == "day after tomorrow":
        return today + timedelta(days=2)
    words = re.sub(r"^(this|next|coming|the coming|on)\s+", "", words)
    if words in ("weekend", "the weekend"):
        words = "saturday"
    if words in _WEEKDAY_NAMES:
        ahead = (_WEEKDAY_NAMES.index(words) - today.weekday()) % 7
        return today + timedelta(days=ahead)
    return None


def _listed(t: Train, known: dict[str, Station]) -> dict[str, str]:
    def name(code: str) -> str:
        station = known.get(code)
        return f"{station.name} ({code})" if station else code

    return {
        "train": f"{t.number} {t.name}",
        "from": name(t.source),
        "departs": t.departs,
        "to": name(t.dest),
        "arrives": t.arrives + (f" (+{t.days_later} day)" if t.days_later else ""),
        "takes": t.duration,
        "runs": t.days(),
        "classes": " ".join(t.classes),
        "type": t.kind,
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _cache_path() -> Path:
    from arcee.context.store import data_dir

    return data_dir() / "stations.js"


def _fresh(path: Path) -> str | None:
    try:
        if time.time() - path.stat().st_mtime < STATIONS_FRESH_S:
            return path.read_text(encoding="utf-8")
    except OSError:
        return None
    return None


def _save(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def travel_tools() -> list[Tool[Any]]:
    return [TravelSearch()]
