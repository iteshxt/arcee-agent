"""The user's Google Calendar, through GNOME Online Accounts (decision 0017).

Google's Calendar API with GOA's token: every calendar the user has (theirs, shared,
holidays), searching, free time, and changes. Reading runs without asking; adding,
changing and deleting ask first, with what will happen (decision 0017's table), and an
event with guests names them, because Google emails them. What a calendar holds is
personal: before it goes to a cloud model the user is asked, as they chose (Settings →
Permissions → Privacy). Event descriptions come from other people (invitations): they
are outside content, never instructions.
"""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from urllib.parse import quote

import httpx
from pydantic import Field

from arcee.core.policy import Tier
from arcee.google import GoogleAccount, GoogleError
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage

API: Final = "https://www.googleapis.com/calendar/v3"
TIMEOUT: Final = httpx.Timeout(20.0, connect=5.0)
MAX_EVENTS: Final = 60
DESCRIPTION_CHARS: Final = 240
ID_SEPARATOR: Final = "::"  # calendar id :: event id, one string for the model
DASH: Final = "\u2013"  # an en dash, between times


def _local(moment: datetime) -> datetime:
    return (
        moment.astimezone()
        if moment.tzinfo
        else moment.replace(tzinfo=datetime.now().astimezone().tzinfo)
    )


def parse_when(text: str) -> date | datetime:
    """'2026-11-05' (a day) or '2026-11-05T15:30' (a moment, local time unless it says)."""
    text = text.strip()
    if len(text) == 10:
        return date.fromisoformat(text)
    return _local(datetime.fromisoformat(text))


def _rfc3339(moment: datetime) -> str:
    return _local(moment).isoformat(timespec="seconds")


def _day_start(day: date) -> datetime:
    return _local(datetime.combine(day, time()))


def _event_time(raw: dict[str, Any]) -> tuple[datetime, bool]:
    """An event's start or end as a local moment, and whether it is a whole day."""
    if "dateTime" in raw:
        return _local(datetime.fromisoformat(raw["dateTime"])), False
    return _day_start(date.fromisoformat(raw["date"])), True


def _human(start: datetime, end: datetime, all_day: bool) -> str:
    day = f"{start:%a} {start.day} {start:%b}"
    if all_day:
        last = end - timedelta(days=1)
        return (
            day
            + ("" if last.date() == start.date() else f" to {last:%a} {last.day} {last:%b}")
            + ", all day"
        )
    return f"{day}, {start:%H:%M}{DASH}{end:%H:%M}"


class Calendar:
    """Google Calendar's API, with a token from GOA for each call."""

    def __init__(
        self, account: GoogleAccount | None = None, client: httpx.AsyncClient | None = None
    ) -> None:
        self._account = account or GoogleAccount()
        self._client = client

    async def request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None
    ) -> Any:
        token, _ = await self._account.token("calendar")
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            response = await client.request(
                method,
                f"{API}{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise GoogleError(f"Google Calendar did not answer ({type(exc).__name__})") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            try:
                message = response.json().get("error", {}).get("message", "")
            except ValueError:
                message = response.text[:200]
            raise GoogleError(f"Google Calendar refused it ({response.status_code}): {message}")
        return response.json()

    async def calendars(self) -> list[dict[str, Any]]:
        listed = await self.request(
            "GET",
            "/users/me/calendarList",
            params={"fields": "items(id,summary,primary,accessRole)"},
        )
        return list(listed.get("items", []))

    async def events(
        self, calendar_id: str, start: datetime, end: datetime, query: str = ""
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeMin": _rfc3339(start),
            "timeMax": _rfc3339(end),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": MAX_EVENTS,
        }
        if query:
            params["q"] = query
        found = await self.request("GET", f"/calendars/{_quote(calendar_id)}/events", params=params)
        return list(found.get("items", []))


def _quote(calendar_id: str) -> str:
    return quote(calendar_id, safe="")  # ids hold @ and #: one path segment


def free_times(
    busy: list[tuple[datetime, datetime]],
    first: date,
    days: int,
    from_hour: int,
    to_hour: int,
    minutes: int,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """The gaps of at least `minutes` between busy times, within the hours of each day."""
    now = now or datetime.now(UTC).astimezone()
    out: list[dict[str, str]] = []
    for n in range(days):
        today = first + timedelta(days=n)
        close = _day_start(today) + timedelta(hours=to_hour)
        cursor = max(_day_start(today) + timedelta(hours=from_hour), now)
        for b_start, b_end in [*busy, (close, close)]:
            if cursor >= close:
                break
            if b_end <= cursor:
                continue
            gap_end = min(b_start, close)
            if gap_end - cursor >= timedelta(minutes=minutes):
                out.append(
                    {
                        "when": f"{cursor:%a} {cursor.day} {cursor:%b}, "
                        f"{cursor:%H:%M}{DASH}{gap_end:%H:%M}",
                        "minutes": str(int((gap_end - cursor).total_seconds() // 60)),
                    }
                )
            cursor = max(cursor, b_end)
    return out


def _pick(calendars: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    if not name:
        return calendars
    key = name.lower()
    return [c for c in calendars if key in str(c.get("summary", "")).lower()]


def _writable(calendars: list[dict[str, Any]], name: str) -> dict[str, Any]:
    mine = [c for c in calendars if c.get("accessRole") in {"owner", "writer"}]
    if name:
        named = _pick(mine, name)
        if not named:
            names = ", ".join(str(c.get("summary")) for c in mine)
            raise GoogleError(f"no calendar called {name!r} that can be changed; there is {names}")
        return named[0]
    return next((c for c in mine if c.get("primary")), mine[0] if mine else {"id": "primary"})


def _event(item: dict[str, Any], calendar: dict[str, Any]) -> dict[str, Any]:
    start, all_day = _event_time(item.get("start", {}))
    end, _ = _event_time(item.get("end", item.get("start", {})))
    out: dict[str, Any] = {
        "event_id": f"{calendar['id']}{ID_SEPARATOR}{item['id']}",
        "title": item.get("summary") or "(no title)",
        "when": _human(start, end, all_day),
        "start": start.isoformat(timespec="minutes") if not all_day else start.date().isoformat(),
        "calendar": calendar.get("summary", ""),
    }
    if item.get("location"):
        out["location"] = item["location"]
    if item.get("hangoutLink"):
        out["meet"] = item["hangoutLink"]
    attendees: list[dict[str, Any]] = item.get("attendees") or []
    if attendees:
        out["guests"] = len(attendees)
        me = next((a for a in attendees if a.get("self")), None)
        if me and me.get("responseStatus") != "accepted":
            out["your_answer"] = {"needsAction": "not answered"}.get(
                me["responseStatus"], me["responseStatus"]
            )
    if description := (item.get("description") or "").strip():
        out["description"] = description[:DESCRIPTION_CHARS]
    return out


def _split(event_id: str) -> tuple[str, str]:
    calendar_id, sep, item_id = event_id.partition(ID_SEPARATOR)
    if not sep or not item_id:
        raise GoogleError("that is not an event_id from calendar.list")
    return calendar_id, item_id


# --- calendar.list --------------------------------------------------------------------


class ListArgs(ToolArgs):
    start: str = Field(default="", description="First day, YYYY-MM-DD (today when empty)")
    days: int = Field(default=1, ge=1, le=31, description="How many days from start")
    query: str = Field(default="", description="Words to find in events (title, place, notes)")
    calendar: str = Field(default="", description="One calendar by name; empty for all")


class CalendarList(Tool[ListArgs]):
    name = "calendar.list"
    read_only = True
    untrusted = True  # invitations' descriptions are other people's words
    personal = "your calendar"
    description = (
        "The user's Google Calendar events for a day or several (all their calendars, holidays "
        "included), or those matching words: title, time, place, Meet link, calendar, event_id."
    )
    usage = Usage(
        when='"What\'s on my calendar tomorrow?", "my schedule this week", "when is the '
        'dentist?", "is Monday a holiday?", "what\'s my next meeting?"',
        avoid="Opening Google Calendar in Chrome or the Calendar app: this reads it directly.",
        mistakes="Guessing dates: work out start from Now (YYYY-MM-DD) and set days.",
        example='calendar.list(start="2026-11-05", days=1)',
    )
    args_model = ListArgs
    timeout_s = 30

    def __init__(self, calendar: Calendar | None = None) -> None:
        self._calendar = calendar or Calendar()

    async def run(self, args: ListArgs, ctx: ToolContext) -> ToolResult:
        try:
            first = parse_when(args.start) if args.start else date.today()
            day = first.date() if isinstance(first, datetime) else first
            start, end = _day_start(day), _day_start(day + timedelta(days=args.days))
            calendars = _pick(await self._calendar.calendars(), args.calendar)
            found = await asyncio.gather(
                *(self._calendar.events(c["id"], start, end, args.query) for c in calendars)
            )
        except (GoogleError, ValueError) as exc:
            return ToolResult.fail(str(exc))
        events = [
            _event(item, c) for c, items in zip(calendars, found, strict=True) for item in items
        ]
        events.sort(key=lambda e: e["start"])
        span = f"{day:%a} {day.day} {day:%b}" + (
            f" + {args.days - 1} days" if args.days > 1 else ""
        )
        what = f" matching {args.query!r}" if args.query else ""
        summary = f"{len(events)} events{what}, {span}"
        return ToolResult.ok(
            summary, data={"from": day.isoformat(), "days": args.days, "events": events}
        )


# --- calendar.free ----------------------------------------------------------------------


class FreeArgs(ToolArgs):
    start: str = Field(default="", description="First day, YYYY-MM-DD (today when empty)")
    days: int = Field(default=1, ge=1, le=7)
    # Small models send 0 for "the usual": 0 (or an end before the start) means the default.
    from_hour: int = Field(default=9, ge=0, le=23, description="The day's free time starts at")
    to_hour: int = Field(default=18, ge=0, le=24, description="and ends at")
    minutes: int = Field(
        default=30, ge=0, le=480, description="The shortest free time worth naming"
    )


class CalendarFree(Tool[FreeArgs]):
    name = "calendar.free"
    read_only = True
    personal = "your calendar"
    description = "Free time in the user's calendars over one or more days, within given hours."
    usage = Usage(
        when='"When am I free on Thursday?", "find me an hour this week", "am I free at 3?"',
        avoid="Listing what the events are: calendar.list.",
        example='calendar.free(start="2026-11-06", days=1, from_hour=9, to_hour=18)',
    )
    args_model = FreeArgs
    timeout_s = 30

    def __init__(self, calendar: Calendar | None = None) -> None:
        self._calendar = calendar or Calendar()

    async def run(self, args: FreeArgs, ctx: ToolContext) -> ToolResult:
        try:
            first = parse_when(args.start) if args.start else date.today()
            day = first.date() if isinstance(first, datetime) else first
            calendars = [
                c
                for c in await self._calendar.calendars()
                if c.get("accessRole") in {"owner", "writer"}
            ]
            start, end = _day_start(day), _day_start(day + timedelta(days=args.days))
            body = {
                "timeMin": _rfc3339(start),
                "timeMax": _rfc3339(end),
                "items": [{"id": c["id"]} for c in calendars],
            }
            answer = await self._calendar.request("POST", "/freeBusy", body=body)
        except (GoogleError, ValueError) as exc:
            return ToolResult.fail(str(exc))
        busy = sorted(
            (_local(datetime.fromisoformat(b["start"])), _local(datetime.fromisoformat(b["end"])))
            for c in answer.get("calendars", {}).values()
            for b in c.get("busy", [])
        )
        hours = (args.from_hour, args.to_hour) if args.to_hour > args.from_hour else (9, 18)
        free = free_times(busy, day, args.days, *hours, args.minutes if args.minutes >= 5 else 30)
        summary = f"{len(free)} free times" + (f", first {free[0]['when']}" if free else "")
        return ToolResult.ok(
            summary, data={"free": free, "hours": f"{hours[0]}:00{DASH}{hours[1]}:00"}
        )


# --- calendar.add -----------------------------------------------------------------------


class AddArgs(ToolArgs):
    title: str = Field(description="What the event is")
    start: str = Field(description="YYYY-MM-DDTHH:MM (local time), or YYYY-MM-DD for all day")
    end: str = Field(default="", description="Same form as start; empty for start + minutes")
    minutes: int = Field(default=60, ge=5, le=24 * 60, description="Length when end is empty")
    calendar: str = Field(default="", description="A calendar by name; empty for the main one")
    location: str = ""
    notes: str = Field(default="", description="The event's description")
    guests: list[str] = Field(default_factory=list[str], description="Guests' email addresses")


class CalendarAdd(Tool[AddArgs]):
    name = "calendar.add"
    tier = Tier.CONFIRM
    risk = "medium"
    description = "Add an event to the user's Google Calendar (asks the user first)."
    usage = Usage(
        when='"Add a dentist appointment tomorrow at 10:30", "put lunch with Priya on Friday", '
        '"block 3 to 5 pm today".',
        avoid="Opening Google Calendar in Chrome or the Calendar app.",
        mistakes="Looking at the calendar first: add it directly (the user sees it before it is "
        "saved). Dates from memory: work them out from Now. Guests only when the user named "
        "them: Google emails each one.",
        example='calendar.add(title="Dentist", start="2026-11-05T10:30", minutes=45)',
    )
    args_model = AddArgs
    timeout_s = 30

    def __init__(self, calendar: Calendar | None = None) -> None:
        self._calendar = calendar or Calendar()

    def sentence(self, args: AddArgs) -> str:
        return f"Add “{args.title}” to {args.calendar or 'your calendar'}?"

    def preview(self, args: AddArgs) -> tuple[str, ...]:
        lines = [_when_line(args.start, args.end, args.minutes)]
        if args.location:
            lines.append(f"At {args.location}")
        if args.guests:
            lines.append(f"Google will email: {', '.join(args.guests)}")
        return tuple(lines)

    async def run(self, args: AddArgs, ctx: ToolContext) -> ToolResult:
        try:
            calendar = _writable(await self._calendar.calendars(), args.calendar)
            body = {"summary": args.title, **_times(args.start, args.end, args.minutes)}
            if args.location:
                body["location"] = args.location
            if args.notes:
                body["description"] = args.notes
            if args.guests:
                body["attendees"] = [{"email": g} for g in args.guests]
            params = {"sendUpdates": "all" if args.guests else "none"}
            item = await self._calendar.request(
                "POST", f"/calendars/{_quote(calendar['id'])}/events", params=params, body=body
            )
        except (GoogleError, ValueError) as exc:
            return ToolResult.fail(str(exc))
        event = _event(item, calendar)
        return ToolResult.ok(f"Added “{event['title']}”, {event['when']}", data=event)


def _times(start_text: str, end_text: str, minutes: int) -> dict[str, Any]:
    start = parse_when(start_text)
    if not isinstance(start, datetime):
        last = parse_when(end_text) if end_text else start
        last_day = last.date() if isinstance(last, datetime) else last
        return {
            "start": {"date": start.isoformat()},
            "end": {"date": (last_day + timedelta(days=1)).isoformat()},
        }
    end = parse_when(end_text) if end_text else start + timedelta(minutes=minutes)
    if not isinstance(end, datetime):
        raise ValueError("end needs a time too (YYYY-MM-DDTHH:MM)")
    if end <= start:
        raise ValueError("the event ends before it starts")
    return {"start": {"dateTime": _rfc3339(start)}, "end": {"dateTime": _rfc3339(end)}}


def _when_line(start_text: str, end_text: str, minutes: int) -> str:
    try:
        times = _times(start_text, end_text, minutes)
    except ValueError as exc:
        return f"When: {start_text} ({exc})"
    start, all_day = _event_time(times["start"])
    end, _ = _event_time(times["end"])
    return _human(start, end, all_day)


# --- calendar.update and calendar.delete ------------------------------------------------


class UpdateArgs(ToolArgs):
    event_id: str = Field(description="The event_id from calendar.list")
    title: str = Field(description="The event's title as listed (shown to the user to confirm)")
    new_title: str = ""
    start: str = Field(default="", description="New start (as calendar.add); empty to keep")
    end: str = Field(default="", description="New end; empty keeps the event's length")
    location: str = ""
    notes: str = ""


class CalendarUpdate(Tool[UpdateArgs]):
    name = "calendar.update"
    tier = Tier.CONFIRM
    risk = "medium"
    description = (
        "Change an event in the user's Google Calendar: time, title, place or notes (asks first)."
    )
    usage = Usage(
        when='"Move my 4 pm meeting to 5", "rename the dentist event", "change the place".',
        avoid="Deleting it: calendar.delete.",
        mistakes="An event_id not from calendar.list: list first, then change it.",
        example='calendar.update(event_id="…", title="Team sync", start="2026-11-05T17:00")',
    )
    args_model = UpdateArgs
    timeout_s = 30

    def __init__(self, calendar: Calendar | None = None) -> None:
        self._calendar = calendar or Calendar()

    def sentence(self, args: UpdateArgs) -> str:
        return f"Change “{args.title}”?"

    def preview(self, args: UpdateArgs) -> tuple[str, ...]:
        lines: list[str] = []
        if args.new_title:
            lines.append(f"Title: {args.new_title}")
        if args.start:
            lines.append(
                f"New time: {args.start}" + (f" to {args.end}" if args.end else " (same length)")
            )
        if args.location:
            lines.append(f"Place: {args.location}")
        if args.notes:
            lines.append("New notes")
        return tuple(lines)

    async def run(self, args: UpdateArgs, ctx: ToolContext) -> ToolResult:
        try:
            calendar_id, item_id = _split(args.event_id)
            path = f"/calendars/{_quote(calendar_id)}/events/{item_id}"
            current = await self._calendar.request("GET", path)
            _same_event(current, args.title)
            body: dict[str, Any] = {}
            if args.new_title:
                body["summary"] = args.new_title
            if args.location:
                body["location"] = args.location
            if args.notes:
                body["description"] = args.notes
            if args.start:
                old_start, all_day = _event_time(current["start"])
                old_end, _ = _event_time(current["end"])
                # Moved without a new end: it keeps its length (a whole day becomes an hour).
                length = 60 if all_day else int((old_end - old_start).total_seconds() // 60)
                body |= _times(args.start, args.end, max(5, length))
            if not body:
                return ToolResult.fail("nothing to change: give a new title, time, place or notes")
            guests = bool(current.get("attendees"))
            item = await self._calendar.request(
                "PATCH", path, params={"sendUpdates": "all" if guests else "none"}, body=body
            )
        except (GoogleError, ValueError, KeyError) as exc:
            return ToolResult.fail(str(exc))
        calendar = {"id": calendar_id, "summary": ""}
        event = _event(item, calendar)
        told = " (guests were told)" if guests else ""
        return ToolResult.ok(f"Changed “{event['title']}”: {event['when']}{told}", data=event)


class DeleteArgs(ToolArgs):
    event_id: str = Field(description="The event_id from calendar.list")
    title: str = Field(description="The event's title as listed (shown to the user to confirm)")


class CalendarDelete(Tool[DeleteArgs]):
    name = "calendar.delete"
    tier = Tier.CONFIRM
    risk = "high"
    description = "Delete an event from the user's Google Calendar (asks first)."
    usage = Usage(
        when='"Cancel tomorrow\'s dentist", "delete the 4 pm meeting".',
        mistakes="An event_id not from calendar.list: list first.",
        example='calendar.delete(event_id="…", title="Dentist")',
    )
    args_model = DeleteArgs
    timeout_s = 30

    def __init__(self, calendar: Calendar | None = None) -> None:
        self._calendar = calendar or Calendar()

    def sentence(self, args: DeleteArgs) -> str:
        return f"Delete “{args.title}” from your calendar?"

    async def run(self, args: DeleteArgs, ctx: ToolContext) -> ToolResult:
        try:
            calendar_id, item_id = _split(args.event_id)
            path = f"/calendars/{_quote(calendar_id)}/events/{item_id}"
            current = await self._calendar.request("GET", path)
            _same_event(current, args.title)
            guests = bool(current.get("attendees"))
            await self._calendar.request(
                "DELETE", path, params={"sendUpdates": "all" if guests else "none"}
            )
        except (GoogleError, ValueError, KeyError) as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(f"Deleted “{current.get('summary') or args.title}”")


def _same_event(current: dict[str, Any], title: str) -> None:
    """The event found is the one the user was asked about, or nothing is changed."""
    found = (current.get("summary") or "(no title)").strip().lower()
    if title.strip().lower() not in found and found not in title.strip().lower():
        raise GoogleError(
            f"that event is “{current.get('summary')}”, not “{title}”: nothing changed; list again"
        )


def calendar_tools(calendar: Calendar | None = None) -> list[Tool[Any]]:
    shared = calendar or Calendar()
    return [
        CalendarList(shared),
        CalendarFree(shared),
        CalendarAdd(shared),
        CalendarUpdate(shared),
        CalendarDelete(shared),
    ]
