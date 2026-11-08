"""Money and maps: today's exchange rates, stock and crypto prices, driving routes.

Each was a web search whose answer the model had to pick out of a page (and say the
time of). These ask free services with no key directly: Frankfurter (the European
Central Bank's rates) with ExchangeRate-API's open rates for the rest; Yahoo Finance's
chart data for shares and CoinGecko for coins; OpenStreetMap's Nominatim to find places
and the OSRM router for the drive. Nothing personal goes to any of them, except a
route's two ends.
"""

import re
from datetime import datetime
from typing import Any, ClassVar, Final, Literal, cast
from urllib.parse import quote

import httpx
from pydantic import Field

from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage

USER_AGENT: Final = "Arcee/0.3 (desktop agent; https://iteshxt.me)"
FRANKFURTER: Final = "https://api.frankfurter.dev/v1/latest"
OPEN_RATES: Final = "https://open.er-api.com/v6/latest/"
YAHOO_SEARCH: Final = "https://query1.finance.yahoo.com/v1/finance/search"
YAHOO_CHART: Final = "https://query1.finance.yahoo.com/v8/finance/chart/"
COINGECKO: Final = "https://api.coingecko.com/api/v3"
NOMINATIM: Final = "https://nominatim.openstreetmap.org/search"
OSRM: Final = "https://router.project-osrm.org/route/v1/"
WTTR_HERE: Final = "https://wttr.in/?format=j1"

CURRENCY_WORDS: Final = {
    "dollar": "USD",
    "dollars": "USD",
    "usd": "USD",
    "$": "USD",
    "rupee": "INR",
    "rupees": "INR",
    "rs": "INR",
    "₹": "INR",
    "euro": "EUR",
    "euros": "EUR",
    "€": "EUR",
    "pound": "GBP",
    "pounds": "GBP",
    "£": "GBP",
    "yen": "JPY",
    "yuan": "CNY",
    "dirham": "AED",
    "dirhams": "AED",
    "riyal": "SAR",
    "ringgit": "MYR",
    "baht": "THB",
    "won": "KRW",
    "franc": "CHF",
    "francs": "CHF",
    "canadian dollar": "CAD",
    "canadian dollars": "CAD",
    "australian dollar": "AUD",
    "australian dollars": "AUD",
    "singapore dollar": "SGD",
    "singapore dollars": "SGD",
    "taka": "BDT",
    "nepali rupee": "NPR",
    "nepali rupees": "NPR",
    "pakistani rupee": "PKR",
}
COINS: Final = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "ether": "ethereum",
    "solana": "solana",
    "sol": "solana",
    "dogecoin": "dogecoin",
    "doge": "dogecoin",
    "xrp": "ripple",
    "ripple": "ripple",
    "cardano": "cardano",
    "ada": "cardano",
    "tether": "tether",
    "usdt": "tether",
    "bnb": "binancecoin",
    "litecoin": "litecoin",
    "ltc": "litecoin",
}


def currency_code(word: str) -> str | None:
    """ "rupees" -> INR, "usd" -> USD; None when it is not a currency it knows."""
    key = word.lower().strip().strip(".")
    if key in CURRENCY_WORDS:
        return CURRENCY_WORDS[key]
    return key.upper() if re.fullmatch(r"[a-z]{3}", key) else None


def _client(client: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient, bool]:
    if client is not None:
        return client, False
    return httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}), True


# --- convert.currency ---------------------------------------------------------------------------


class CurrencyArgs(ToolArgs):
    amount: float = Field(default=1, ge=0, description="How much")
    source: str = Field(description='The currency it is in: "USD", "dollars", "rupees"')
    target: str = Field(description='The currency wanted: "INR", "euros"')


class ConvertCurrency(Tool[CurrencyArgs]):
    name = "convert.currency"
    description = "Convert money between currencies at today's exchange rate."
    usage = Usage(
        when='"100 dollars in rupees?", "how much is 50 euros in INR", "dollar rate today".',
        example='convert.currency(amount=100, source="USD", target="INR")',
    )
    args_model = CurrencyArgs
    read_only: ClassVar[bool] = True
    timeout_s = 20

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def run(self, args: CurrencyArgs, ctx: ToolContext) -> ToolResult:
        source, target = currency_code(args.source), currency_code(args.target)
        if source is None or target is None:
            unknown = args.source if source is None else args.target
            return ToolResult.fail(f"“{unknown}” is not a currency; give its code (USD, INR)")
        client, own = _client(self._client)
        try:
            rate, date, by = await _rate(client, source, target)
        except (httpx.HTTPError, LookupError, ValueError) as exc:
            return ToolResult.fail(f"no exchange rate for {source} to {target}: {exc}")
        finally:
            if own:
                await client.aclose()
        value = args.amount * rate
        shown = f"{value:,.2f}" if value >= 1 else f"{value:.4g}"
        return ToolResult.ok(
            f"{args.amount:g} {source} = {shown} {target} (rate of {date})",
            data={
                "amount": args.amount,
                "from": source,
                "to": target,
                "result": round(value, 4),
                "rate": rate,
                "rate_date": date,
                "source": by,
            },
        )


async def _rate(client: httpx.AsyncClient, source: str, target: str) -> tuple[float, str, str]:
    if source == target:
        return 1.0, datetime.now().date().isoformat(), "same currency"
    response = await client.get(FRANKFURTER, params={"base": source, "symbols": target})
    if response.status_code == 200:
        body = response.json()
        if target in body.get("rates", {}):
            return float(body["rates"][target]), str(body["date"]), "European Central Bank"
    response = await client.get(OPEN_RATES + source)
    body = response.json()
    if body.get("result") != "success" or target not in body.get("rates", {}):
        raise LookupError("not a currency the rate services know")
    when = datetime.fromtimestamp(int(body.get("time_last_update_unix", 0))).date().isoformat()
    return float(body["rates"][target]), when, "ExchangeRate-API"


# --- market.quote -------------------------------------------------------------------------------


class QuoteArgs(ToolArgs):
    name: str = Field(
        description='A company, a ticker or a coin: "Reliance", "TCS.NS", "AAPL", "bitcoin"'
    )
    kind: Literal["auto", "stock", "crypto"] = "auto"
    currency: str = Field(default="", description="For coins: the currency to price it in")


class MarketQuote(Tool[QuoteArgs]):
    name = "market.quote"
    description = "A share's or a cryptocurrency's latest price and today's change."
    usage = Usage(
        when='"What\'s the price of Bitcoin?", "how is Reliance stock doing?", "Apple '
        'share price".',
        mistakes="Saying the price without its time: markets close; say when it was.",
        example='market.quote(name="Reliance")',
    )
    args_model = QuoteArgs
    read_only: ClassVar[bool] = True
    timeout_s = 20

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def run(self, args: QuoteArgs, ctx: ToolContext) -> ToolResult:
        client, own = _client(self._client)
        try:
            coin = COINS.get(args.name.lower().strip())
            if args.kind == "crypto" or (args.kind == "auto" and coin):
                data = await _coin(client, coin or args.name, args.currency or "INR")
            else:
                data = await _stock(client, args.name)
        except (httpx.HTTPError, LookupError, KeyError, ValueError, IndexError) as exc:
            return ToolResult.fail(f"no price for “{args.name}”: {exc}")
        finally:
            if own:
                await client.aclose()
        change = data.get("change_percent")
        moved = f", {change:+.2f}% today" if isinstance(change, float) else ""
        return ToolResult.ok(
            f"{data['name']}: {data['price']:,} {data['currency']}{moved} ({data['as_of']})",
            data=data,
        )


async def _coin(client: httpx.AsyncClient, wanted: str, currency: str) -> dict[str, Any]:
    coin = wanted.lower().strip()
    if coin not in COINS.values():
        found: dict[str, Any] = (
            await client.get(f"{COINGECKO}/search", params={"query": wanted})
        ).json()
        coins = cast(list[dict[str, Any]], found.get("coins") or [])
        if not coins:
            raise LookupError("no such coin")
        coin = str(coins[0]["id"])
    vs = (currency_code(currency) or "INR").lower()
    response = await client.get(
        f"{COINGECKO}/simple/price",
        params={
            "ids": coin,
            "vs_currencies": vs,
            "include_24hr_change": "true",
            "include_last_updated_at": "true",
        },
    )
    body = response.json()[coin]
    when = datetime.fromtimestamp(int(body.get("last_updated_at", 0)))
    return {
        "name": coin.title(),
        "price": round(float(body[vs]), 2),
        "currency": vs.upper(),
        "change_percent": round(float(body.get(f"{vs}_24h_change") or 0), 2),
        "as_of": f"{when:%d %b %H:%M}, 24 h change",
        "source": "CoinGecko",
    }


async def _stock(client: httpx.AsyncClient, wanted: str) -> dict[str, Any]:
    symbol = wanted.strip().upper()
    if not re.fullmatch(r"[A-Z0-9^.=-]{1,15}", symbol) or " " in wanted.strip():
        symbol = ""
    if not symbol or not await _has_chart(client, symbol):
        found: dict[str, Any] = (
            await client.get(
                YAHOO_SEARCH,
                params={"q": wanted, "quotesCount": 5, "newsCount": 0},
                headers={"User-Agent": "Mozilla/5.0"},
            )
        ).json()
        kinds = ("EQUITY", "ETF", "INDEX")
        listed = cast(list[dict[str, Any]], found.get("quotes") or [])
        quotes = [q for q in listed if q.get("quoteType") in kinds]
        if not quotes:
            raise LookupError("no such share")
        # An Indian listing first, for a user in India: NSE, then BSE.
        quotes.sort(key=lambda q: {"NSI": 0, "BSE": 1}.get(str(q.get("exchange")), 2))
        symbol = str(quotes[0]["symbol"])
    meta = (await _chart(client, symbol))["meta"]
    price = float(meta["regularMarketPrice"])
    previous = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
    change = round((price - previous) / previous * 100, 2) if previous else None
    when = datetime.fromtimestamp(int(meta.get("regularMarketTime", 0)))
    return {
        "name": str(meta.get("longName") or meta.get("shortName") or symbol),
        "symbol": symbol,
        "price": round(price, 2),
        "currency": str(meta.get("currency", "")),
        "change_percent": change,
        "exchange": str(meta.get("fullExchangeName") or meta.get("exchangeName") or ""),
        "as_of": f"{when:%d %b %H:%M}",
        "source": "Yahoo Finance",
    }


async def _chart(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    response = await client.get(
        YAHOO_CHART + quote(symbol),
        params={"range": "1d", "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    body: dict[str, Any] = response.json()
    chart = cast(dict[str, Any], body.get("chart") or {})
    result = cast(list[dict[str, Any]], chart.get("result") or [])
    if not result:
        raise LookupError(f"no chart for {symbol}")
    return result[0]


async def _has_chart(client: httpx.AsyncClient, symbol: str) -> bool:
    try:
        await _chart(client, symbol)
    except LookupError, httpx.HTTPError, ValueError:
        return False
    return True


# --- maps.route ---------------------------------------------------------------------------------


class RouteArgs(ToolArgs):
    destination: str = Field(description='Where to: "Chandigarh", "Delhi airport"')
    origin: str = Field(default="", description="From where; empty for where the user is")
    mode: Literal["driving", "cycling", "walking"] = "driving"


async def _place(client: httpx.AsyncClient, name: str) -> tuple[float, float, str]:
    response = await client.get(NOMINATIM, params={"q": name, "format": "json", "limit": 1})
    found = response.json()
    if not found:
        raise LookupError(f"no place called “{name}”")
    spot = found[0]
    parts: list[str] = []
    for part in str(spot.get("display_name", name)).split(", "):
        if not any(part in seen or seen in part for seen in parts) and not part.isdigit():
            parts.append(part)
    label = ", ".join(parts[:2])
    return float(spot["lat"]), float(spot["lon"]), label


async def _here(client: httpx.AsyncClient) -> tuple[float, float, str]:
    """Where the user is, roughly (from the network, as web.weather finds it)."""
    area = (await client.get(WTTR_HERE)).json()["nearest_area"][0]
    name = ", ".join(a[0]["value"] for a in (area["areaName"], area["region"]) if a)
    return float(area["latitude"]), float(area["longitude"]), name


def _hours(seconds: float) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


class MapsRoute(Tool[RouteArgs]):
    name = "maps.route"
    description = "How far and how long by road (or bike, or on foot) between two places."
    usage = Usage(
        when='"How long to drive to Chandigarh?", "distance from Delhi to Agra", "how far '
        'is the airport?".',
        mistakes="It is without live traffic: say so, and give the Google Maps link for it.",
        example='maps.route(destination="Chandigarh")',
    )
    args_model = RouteArgs
    read_only: ClassVar[bool] = True
    timeout_s = 30

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def run(self, args: RouteArgs, ctx: ToolContext) -> ToolResult:
        client, own = _client(self._client)
        try:
            start = await (_place(client, args.origin) if args.origin else _here(client))
            end = await _place(client, args.destination)
            profile = {"driving": "driving", "cycling": "bike", "walking": "foot"}[args.mode]
            response = await client.get(
                f"{OSRM}{profile}/{start[1]},{start[0]};{end[1]},{end[0]}",
                params={"overview": "false"},
            )
            body = response.json()
            if body.get("code") != "Ok":
                raise LookupError(str(body.get("message") or body.get("code")))
            route = body["routes"][0]
        except (httpx.HTTPError, LookupError, KeyError, ValueError, IndexError) as exc:
            return ToolResult.fail(f"no route: {exc}")
        finally:
            if own:
                await client.aclose()
        km = round(route["distance"] / 1000, 1)
        time = _hours(route["duration"])
        travel = {"driving": "driving", "cycling": "bicycling", "walking": "walking"}[args.mode]
        link = (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={start[0]},{start[1]}&destination={quote(args.destination)}"
            f"&travelmode={travel}"
        )
        return ToolResult.ok(
            f"{start[2]} to {end[2]}: {km} km, about {time} {args.mode} (no live traffic)",
            data={
                "from": start[2],
                "to": end[2],
                "km": km,
                "time": time,
                "mode": args.mode,
                "traffic": "not included",
                "google_maps": link,
            },
        )


def market_tools() -> list[Tool[Any]]:
    return [ConvertCurrency(), MarketQuote(), MapsRoute()]
