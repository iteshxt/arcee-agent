"""The user's Google account, through GNOME Online Accounts (decision 0017).

Ubuntu's Settings → Online Accounts signs in to Google with GNOME's own client and keeps
the tokens in the keyring. Arcee asks GOA for a fresh access token each time it needs
one (about an hour's life) and keeps none: no password, no sign-in of its own.
Removing the account, or turning its Calendar or Mail off, cuts Arcee off at once.
"""

from dataclasses import dataclass
from typing import Any, Final, Literal

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

GOA: Final = "org.gnome.OnlineAccounts"
ROOT: Final = "/org/gnome/OnlineAccounts"
SETUP: Final = "Add your Google account in Settings → Online Accounts"
Service = Literal["calendar", "mail", "contacts"]
DISABLED: Final = {
    "calendar": "CalendarDisabled",
    "mail": "MailDisabled",
    "contacts": "ContactsDisabled",
}


class GoogleError(Exception):
    """Why Google cannot be reached, in words for the user."""


@dataclass(frozen=True, slots=True)
class Account:
    path: str  # GOA's object
    email: str


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Variant) else value


class GoogleAccount:
    """GOA's Google account (the first, if there are several), and its tokens."""

    def __init__(self, bus_address: str | None = None) -> None:
        self._address = bus_address

    async def _bus(self) -> MessageBus:
        if self._address:
            return await MessageBus(bus_address=self._address).connect()
        return await MessageBus(bus_type=BusType.SESSION).connect()

    async def _call(self, bus: MessageBus, path: str, interface: str, member: str) -> list[Any]:
        try:
            reply = await bus.call(
                Message(destination=GOA, path=path, interface=interface, member=member)
            )
        except OSError as exc:
            raise GoogleError(f"GNOME Online Accounts did not answer ({exc})") from exc
        if reply.message_type is MessageType.ERROR:
            detail = str(reply.body[0]) if reply.body else reply.error_name
            raise GoogleError(f"GNOME Online Accounts: {detail}")
        return list(reply.body)

    async def token(self, service: Service) -> tuple[str, Account]:
        """A fresh access token for `service`, and the account it is for."""
        bus = await self._bus()
        try:
            body = await self._call(
                bus, ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"
            )
            google = [
                (path, ifaces)
                for path, ifaces in body[0].items()
                if _value(ifaces.get(f"{GOA}.Account", {}).get("ProviderType")) == "google"
            ]
            if not google:
                raise GoogleError(f"No Google account is set up. {SETUP}.")
            enabled = [
                (path, ifaces)
                for path, ifaces in google
                if not _value(ifaces[f"{GOA}.Account"].get(DISABLED[service]))
            ]
            if not enabled:
                raise GoogleError(
                    f"{service.capitalize()} is turned off for the Google account. Turn it on "
                    "in Settings → Online Accounts → Google."
                )
            path, ifaces = enabled[0]
            account = ifaces[f"{GOA}.Account"]
            if _value(account.get("AttentionNeeded")):
                raise GoogleError(
                    "The Google account needs signing in again: open Settings → Online Accounts."
                )
            token, _expires = await self._call(bus, path, f"{GOA}.OAuth2Based", "GetAccessToken")
            email = str(
                _value(account.get("PresentationIdentity")) or _value(account.get("Identity"))
            )
            return str(token), Account(path, email)
        finally:
            bus.disconnect()
