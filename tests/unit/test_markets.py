"""Exchange rates, prices and routes, against mocked services."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from arcee.core.toolsets import WEB, filters_for
from arcee.models.base import Message
from arcee.tools.base import ToolContext
from arcee.tools.markets import (
    ConvertCurrency,
    CurrencyArgs,
    MapsRoute,
    MarketQuote,
    QuoteArgs,
    RouteArgs,
    currency_code,
)

CTX = ToolContext("t", Path("/home/user"))


def mocked(routes: dict[str, Any]) -> httpx.AsyncClient:
    """Answers by the first key found in the URL; records the URLs asked."""
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        asked.append(url)
        for key, body in routes.items():
            if key in url:
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    client.asked = asked  # type: ignore[attr-defined]
    return client


@pytest.mark.parametrize(
    ("said", "code"),
    [("dollars", "USD"), ("Rupees", "INR"), ("€", "EUR"), ("gbp", "GBP"), ("bananas", None)],
)
def test_currencies_as_people_say_them(said: str, code: str | None) -> None:
    assert currency_code(said) == code


async def test_a_conversion_at_the_central_banks_rate() -> None:
    client = mocked({"frankfurter": {"date": "2026-11-06", "rates": {"INR": 93.92}}})
    result = await ConvertCurrency(client).run(
        CurrencyArgs(amount=100, source="dollars", target="rupees"), CTX
    )
    assert result.summary == "100 USD = 9,392.00 INR (rate of 2026-11-06)"


async def test_a_currency_the_bank_lacks_comes_from_the_open_rates() -> None:
    client = mocked(
        {
            "frankfurter": {"date": "2026-11-06", "rates": {}},
            "er-api": {"result": "success", "time_last_update_unix": 0, "rates": {"NPR": 1.6}},
        }
    )
    result = await ConvertCurrency(client).run(
        CurrencyArgs(amount=500, source="INR", target="NPR"), CTX
    )
    assert result.data is not None
    assert (result.data["result"], result.data["source"]) == (800.0, "ExchangeRate-API")


async def test_a_coin_is_priced_in_rupees() -> None:
    client = mocked(
        {
            "simple/price": {
                "bitcoin": {"inr": 7124589, "inr_24h_change": -0.1516, "last_updated_at": 0}
            }
        }
    )
    result = await MarketQuote(client).run(QuoteArgs(name="BTC"), CTX)
    assert result.summary.startswith("Bitcoin: 7,124,589.0 INR, -0.15% today")


CHART = {
    "chart": {
        "result": [
            {
                "meta": {
                    "regularMarketPrice": 1324.0,
                    "chartPreviousClose": 1313.4,
                    "currency": "INR",
                    "longName": "Reliance Industries Limited",
                    "fullExchangeName": "NSE",
                    "regularMarketTime": 0,
                }
            }
        ]
    }
}


async def test_a_company_is_found_on_the_indian_exchange_first() -> None:
    client = mocked(
        {
            "finance/search": {
                "quotes": [
                    {"symbol": "RS", "exchange": "NYQ", "quoteType": "EQUITY"},
                    {"symbol": "RELIANCE.NS", "exchange": "NSI", "quoteType": "EQUITY"},
                ]
            },
            "chart/RELIANCE.NS": CHART,
        }
    )
    result = await MarketQuote(client).run(QuoteArgs(name="Reliance"), CTX)
    assert result.data is not None
    assert (result.data["symbol"], result.data["change_percent"]) == ("RELIANCE.NS", 0.81)


async def test_a_route_between_two_places() -> None:
    client = mocked(
        {
            "q=Ludhiana": [
                {"lat": "30.90", "lon": "75.85", "display_name": "Ludhiana, Punjab, India"}
            ],
            "q=Chandigarh": [
                {"lat": "30.73", "lon": "76.77", "display_name": "Chandigarh, 160018, India"}
            ],
            "project-osrm": {"code": "Ok", "routes": [{"distance": 99400.0, "duration": 4740.0}]},
        }
    )
    result = await MapsRoute(client).run(
        RouteArgs(origin="Ludhiana", destination="Chandigarh"), CTX
    )
    assert result.summary == (
        "Ludhiana, Punjab to Chandigarh, India: 99.4 km, about 1 h 19 min driving (no live traffic)"
    )
    assert result.data is not None
    assert "google.com/maps/dir" in result.data["google_maps"]
    assert "/driving/75.85,30.9;76.77,30.73" in client.asked[-1]  # type: ignore[attr-defined]


async def test_an_unknown_place_says_so() -> None:
    client = mocked({"nominatim": []})
    result = await MapsRoute(client).run(RouteArgs(origin="X", destination="Nowhere"), CTX)
    assert result.error == "no route: no place called “X”"


@pytest.mark.parametrize(
    "said",
    [
        "How many rupees is 100 dollars?",
        "What's the price of Bitcoin?",
        "How is Reliance stock doing?",
        "How long to drive to Chandigarh?",
    ],
)
def test_money_and_maps_words_bring_the_tools(said: str) -> None:
    assert set(WEB) <= set(filters_for([Message("user", said)]))
