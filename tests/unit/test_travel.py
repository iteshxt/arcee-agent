"""travel.search: trains from erail's timetable pages, a flight search to open."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from arcee.core.toolsets import filters_for
from arcee.models.base import Message
from arcee.tools.base import ToolContext
from arcee.tools.travel import (
    Station,
    TravelArgs,
    TravelSearch,
    match_station,
    parse_day,
    parse_stations,
    parse_trains,
)

STATIONS = (
    'var StationsData="BIGN,10510461,ABR,Abu Road,LDH,Ludhiana Jn,DLI,Delhi,'
    "NDLS,New Delhi,SML,Shimla,SML,Manali,KLK,Kalka,LDHC,Ludhiana Cantt Halt,CDG,Chandigarh,"
    'CSMT,C Shivaji Maharaj T,BCT,Mumbai Central";'
)


def train(fields: str, days: str, classes: str) -> str:
    """One train as erail's page writes it."""
    cells = "".join(f"<td>{c}" for c in classes.split())
    return (
        f"<div class='OneTrain' data-train='{fields}'><table><tr><td>{days}</table>"
        f"<table class='tableclass'><tr>{cells}</table></div>"
    )


PAGE = "".join(
    (
        "<html><body>",
        train(
            "12014_AMRITSAR SHTABDI_LDH_NDLS_01-Jan-1900_07:02_01-Jan-1900_11:02_04.00 hr_"
            "312 km_5 min_0 min_1458_1111111_000100000010000_Shatabdi_06.57_07.02_",
            "Daily",
            "CC EC",
        ),
        train(
            "12204_SHC GARIB RATH_LDH_DLI_03-Jan-1900_06:30_03-Jan-1900_10:50_04.20 hr_304 km_"
            "10 min_15 min_1712_0010011_001000000000000_Garib Rath_06.20_06.30_",
            "W S S",
            "3A",
        ),
        train(
            "12926_PASCHIM EXP_LDH_NDLS_01-Jan-1900_22:40_02-Jan-1900_03:55_05.15 hr_312 km_"
            "10 min_5 min_99_1111111_111001000000000_Super Fast_22.30_22.40_",
            "Daily",
            "1A 2A 3A SL",
        ),
        "</body></html>",
    )
)


def test_the_station_list_and_names_people_say() -> None:
    stations = parse_stations(STATIONS)

    assert stations[0] == Station("ABR", "Abu Road")
    assert match_station("Ludhiana", stations) == Station("LDH", "Ludhiana Jn")
    assert match_station("delhi", stations) == Station("NDLS", "New Delhi")  # the city's main
    assert match_station("Abu Road station", stations) == Station("ABR", "Abu Road")
    assert match_station("sml", stations) == Station("SML", "Shimla")  # not Manali
    assert match_station("Mumbai", stations) == Station("CSMT", "C Shivaji Maharaj T")
    assert match_station("Atlantis", stations) is None


def test_trains_are_read_from_the_page() -> None:
    shatabdi, garib_rath, paschim = parse_trains(PAGE)

    assert (shatabdi.number, shatabdi.name, shatabdi.departs, shatabdi.arrives) == (
        "12014",
        "Amritsar Shtabdi",
        "07:02",
        "11:02",
    )
    assert shatabdi.classes == ("CC", "EC")
    assert shatabdi.days() == "daily"
    assert garib_rath.days() == "Wed Sat Sun"
    assert not garib_rath.runs_on(date(2026, 11, 9))  # a Monday
    assert paschim.days_later == 1
    assert paschim.duration == "05:15 h"


class Erail:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        if "stations.js" in request.url.path:
            return httpx.Response(200, text=STATIONS)
        return httpx.Response(200, text=PAGE)


@pytest.fixture
def erail() -> Erail:
    return Erail()


def tool(erail: Erail, tmp_path: Path) -> TravelSearch:
    client = httpx.AsyncClient(transport=httpx.MockTransport(erail.handler))
    return TravelSearch(client, cache=tmp_path / "stations.js", today=lambda: date(2026, 11, 9))


CTX = ToolContext("t_1", Path("/home/user"))


async def test_trains_on_a_day_in_a_window(erail: Erail, tmp_path: Path) -> None:
    result = await tool(erail, tmp_path).run(
        TravelArgs(origin="Ludhiana", destination="Delhi", after="05:00", before="12:00"), CTX
    )

    assert result.summary == (
        "1 trains from Ludhiana Jn (LDH) to New Delhi (NDLS) on Mon 09 Nov, "
        "leaving after 05:00 before 12:00"
    )
    assert result.data is not None
    [only] = result.data["trains"]
    assert only == {
        "train": "12014 Amritsar Shtabdi",
        "from": "Ludhiana Jn (LDH)",
        "departs": "07:02",
        "to": "New Delhi (NDLS)",
        "arrives": "11:02",
        "takes": "04:00 h",
        "runs": "daily",
        "classes": "CC EC",
        "type": "Shatabdi",
    }
    assert (
        erail.asked[-1] == "https://erail.in/trains-between-stations/ludhiana-jn-LDH/new-delhi-NDLS"
    )
    assert "does not book" in result.data["note"]


async def test_the_station_list_is_kept(erail: Erail, tmp_path: Path) -> None:
    search = tool(erail, tmp_path)
    await search.run(TravelArgs(origin="LDH", destination="NDLS", day="2026-11-11"), CTX)
    later = await search.run(TravelArgs(origin="LDH", destination="NDLS", day="2026-11-11"), CTX)

    assert sum("stations.js" in url for url in erail.asked) == 1
    assert later.data is not None
    assert [t["train"] for t in later.data["trains"]] == [
        "12204 Shc Garib Rath",  # Wednesday: it runs (06:30, first)
        "12014 Amritsar Shtabdi",
        "12926 Paschim Exp",
    ]
    assert later.data["trains"][2]["arrives"] == "03:55 (+1 day)"


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"origin": "Atlantis", "destination": "Delhi"}, "no railway station matches “Atlantis”"),
        ({"origin": "LDH", "destination": "NDLS", "day": "someday"}, "not a date"),
        ({"origin": "LDH", "destination": "NDLS", "after": "morning"}, "not a time"),
        ({"origin": "LDH", "destination": "NDLS", "day": "2026-11-07"}, "Sat 07 Nov is past"),
    ],
)
async def test_what_cannot_be_searched_is_said(
    erail: Erail, tmp_path: Path, args: dict[str, Any], error: str
) -> None:
    result = await tool(erail, tmp_path).run(TravelArgs(**args), CTX)

    assert result.error is not None
    assert error in result.error


async def test_flights_give_the_search_to_open(erail: Erail, tmp_path: Path) -> None:
    result = await tool(erail, tmp_path).run(
        TravelArgs(mode="flight", origin="Chandigarh", destination="Goa", day="2026-11-20"), CTX
    )

    assert result.data is not None
    assert result.data["link"] == (
        "https://www.google.com/travel/flights?q=Flights%20from%20Chandigarh%20to%20Goa%20on"
        "%202026-11-20"
    )
    assert erail.asked == []


@pytest.mark.parametrize(
    "request_text",
    [
        "Find trains from Ludhiana to Delhi tomorrow morning.",
        "Plan a weekend trip to Shimla: trains, weather, hotels.",
        "Any flights to Goa next Friday?",
    ],
)
def test_travel_requests_get_the_travel_tools(request_text: str) -> None:
    assert "travel.*" in filters_for([Message("user", request_text)])


def timetable(*trains: tuple[str, str, str, str]) -> str:
    """A between-stations page with daily trains: (number, from, to, departs)."""
    return "".join(
        train(
            f"{n}_TRAIN {n}_{a}_{b}_01-Jan-1900_{dep}_01-Jan-1900_23:00_02.00 hr_90 km_"
            f"5 min_0 min_1_1111111_000100000000000_Express_x_x_",
            "Daily",
            "CC",
        )
        for n, a, b, dep in trains
    )


async def test_a_hill_station_is_reached_leg_by_leg(tmp_path: Path) -> None:
    pages = {
        ("LDH", "SML"): "",
        ("LDH", "KLK"): "",  # nothing straight to the railhead: by Chandigarh
        ("LDH", "CDG"): timetable(("14542", "LDH", "CDG", "07:17")),
        ("CDG", "KLK"): timetable(("12011", "CDG", "KLK", "11:07")),
        ("KLK", "SML"): timetable(
            ("52451", "KLK", "SML", "05:45"), ("52459", "KLK", "SML", "07:00")
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "stations.js" in request.url.path:
            return httpx.Response(200, text=STATIONS)
        a, b = (part.rsplit("-", 1)[1] for part in request.url.path.split("/")[-2:])
        return httpx.Response(200, text=pages[(a, b)])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    search = TravelSearch(client, cache=tmp_path / "s.js", today=lambda: date(2026, 11, 9))

    result = await search.run(TravelArgs(origin="Ludhiana", destination="Shimla"), CTX)

    assert result.summary == (
        "No direct train from Ludhiana Jn (LDH) to Shimla (SML) on Mon 09 Nov; by Chandigarh "
        "(CDG) and Kalka (KLK): Ludhiana Jn (LDH) to Chandigarh (CDG): 14542 at 07:17; "
        "Chandigarh (CDG) to Kalka (KLK): 12011 at 11:07; Kalka (KLK) to Shimla (SML): "
        "52451 at 05:45, 52459 at 07:00"
    )
    assert result.data is not None
    assert [leg["trains"][0]["train"] for leg in result.data["legs"]] == [
        "14542 Train 14542",
        "12011 Train 12011",
        "52451 Train 52451",
    ]
    assert "Kalka-Shimla toy train" in result.data["how"]


async def test_no_train_and_no_known_way_says_so(erail: Erail, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "stations.js" in request.url.path:
            return httpx.Response(200, text=STATIONS)
        return httpx.Response(200, text="<html>no trains</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    search = TravelSearch(client, cache=tmp_path / "s.js", today=lambda: date(2026, 11, 9))

    result = await search.run(TravelArgs(origin="Abu Road", destination="Ludhiana"), CTX)

    assert result.data is not None
    assert result.data["note"].startswith("No direct train between these stations. Try a big")


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("", date(2026, 11, 8)),
        ("2026-11-20", date(2026, 11, 20)),
        ("tomorrow", date(2026, 11, 9)),
        ("Saturday", date(2026, 11, 14)),
        ("next friday", date(2026, 11, 13)),
        ("this weekend", date(2026, 11, 14)),
        ("Sunday", date(2026, 11, 8)),  # today is a Sunday
        ("someday", None),
    ],
)
def test_days_people_say(said: str, expected: date | None) -> None:
    assert parse_day(said, date(2026, 11, 8)) == expected


async def test_coming_back_from_the_hills_changes_the_other_way(tmp_path: Path) -> None:
    pages = {
        ("SML", "LDH"): "",
        ("SML", "KLK"): timetable(("52452", "SML", "KLK", "10:40")),
        ("KLK", "LDH"): "",
        ("KLK", "CDG"): timetable(("12012", "KLK", "CDG", "17:45")),
        ("CDG", "LDH"): timetable(("14541", "CDG", "LDH", "19:00")),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "stations.js" in request.url.path:
            return httpx.Response(200, text=STATIONS)
        a, b = (part.rsplit("-", 1)[1] for part in request.url.path.split("/")[-2:])
        return httpx.Response(200, text=pages[(a, b)])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    search = TravelSearch(client, cache=tmp_path / "s.js", today=lambda: date(2026, 11, 9))

    result = await search.run(TravelArgs(origin="Shimla", destination="Ludhiana"), CTX)

    assert result.data is not None
    assert [(leg["from"], leg["to"]) for leg in result.data["legs"]] == [
        ("Shimla (SML)", "Kalka (KLK)"),
        ("Kalka (KLK)", "Chandigarh (CDG)"),
        ("Chandigarh (CDG)", "Ludhiana Jn (LDH)"),
    ]
