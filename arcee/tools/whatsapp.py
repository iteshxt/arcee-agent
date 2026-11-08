"""`whatsapp.send`: a WhatsApp message, through WhatsApp Web in the user's Chrome (COMM-001).

WhatsApp has no API for a person's own account, and its contacts live on the phone,
so this does what the user would: in web.whatsapp.com, search the chat by name (or
open it by phone number), type the message, and press Send. Sending asks the user
first, with the message and the name. It only sends when the chat that opened has
exactly that name: a near match ("Mom" finding "Mom Office") is left as a draft for
the user to check.
"""

import re
from typing import Any, ClassVar, Final
from urllib.parse import quote

from pydantic import Field

from arcee.browser.client import BrowserError
from arcee.core.policy import Policy, Tier
from arcee.tools.base import ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.browser import BrowserTool

HOME: Final = "https://web.whatsapp.com/"
BY_NUMBER: Final = "https://web.whatsapp.com/send?phone={phone}&text={text}"
SIGNED_IN: Final = "#pane-side"
SEARCH_BOX: Final = (
    "input[aria-label='Search or start a new chat'], "
    "[role='textbox'][aria-label*='earch'], div[contenteditable='true'][data-tab='3']"
)
CHAT_NAME: Final = "#pane-side span[title]"
OPEN_CHAT: Final = "#main header span[title], #main header span[dir='auto']"
COMPOSE: Final = (
    "#main footer div[contenteditable='true'], #main div[contenteditable='true'][role='textbox']"
)
SEND: Final = (
    "#main footer button[aria-label='Send'], #main footer span[data-icon='send'], "
    "#main footer span[data-icon='wds-ic-send-filled']"
)
LOAD_MS: Final = 25_000
NOT_SIGNED_IN: Final = (
    "WhatsApp Web is not signed in: on the phone, WhatsApp → Linked devices → Link a "
    "device, and scan the code on web.whatsapp.com; then ask again"
)
_PHONE: Final = re.compile(r"^\+?[\d\s()-]{8,}$")


def phone_number(to: str) -> str | None:
    """The number to open a chat by (country code first; 10 digits taken as India's)."""
    if not _PHONE.match(to.strip()):
        return None
    digits = re.sub(r"\D", "", to)
    return "91" + digits if len(digits) == 10 else digits


class SendArgs(ToolArgs):
    to: str = Field(min_length=1, description="The chat's name as in WhatsApp, or a phone number")
    text: str = Field(min_length=1, max_length=4000)
    send: bool = Field(default=True, description="False: type it, leave it unsent")


class WhatsAppSend(BrowserTool[SendArgs]):
    name = "whatsapp.send"
    description = (
        "Send a WhatsApp message to a chat by its name (or a phone number), through "
        "WhatsApp Web in Chrome. Asks the user before sending."
    )
    usage = Usage(
        when="\"Send Mom 'I'll be home by 8' on WhatsApp\", \"WhatsApp Rahul that I'm late\".",
        avoid="Reading messages: browser tools on web.whatsapp.com.",
        mistakes="Rewording the message: send the user's words.",
        example='whatsapp.send(to="Mom", text="I\'ll be home by 8")',
    )
    args_model = SendArgs
    observation_kind = "text"
    untrusted: ClassVar[bool] = False
    timeout_s = 75

    def tier_for(self, args: SendArgs, policy: Policy) -> Tier:
        return Tier.CONFIRM if args.send else Tier.AUTO

    def sentence(self, args: SendArgs) -> str:
        return f"Send this WhatsApp message to {args.to}? “{args.text}”"

    def preview(self, args: SendArgs) -> tuple[str, ...]:
        return (args.to, args.text)

    async def run(self, args: SendArgs, ctx: ToolContext) -> ToolResult:
        try:
            return await self._send(args)
        except BrowserError as exc:
            return ToolResult.fail(f"WhatsApp Web did not answer: {exc}")

    async def _select(self, selector: str, **params: Any) -> Any:
        return await self._call("select", tab=self.session.tab, selector=selector, **params)

    async def _send(self, args: SendArgs) -> ToolResult:
        phone = phone_number(args.to)
        url = BY_NUMBER.format(phone=phone, text=quote(args.text)) if phone else HOME
        tab = await self._tab_on_site(HOME)
        if tab is None:
            tab = (await self._call("new_tab", url=url))["id"]
        else:
            await self._call("switch_tab", tab=tab)
            await self._call("navigate", tab=tab, url=url)
        self.session.tab = tab
        if not await self._select(SIGNED_IN, wait_ms=LOAD_MS, limit=1):
            return ToolResult.fail(NOT_SIGNED_IN)
        if phone is None:
            typed = await self._select(SEARCH_BOX, action="type", text=args.to)
            if not typed.get("done"):
                return ToolResult.fail("could not find WhatsApp's search box")
            opened = await self._select(CHAT_NAME, action="click", match=args.to, wait_ms=6000)
            if not opened.get("done"):
                return ToolResult.fail(f"no WhatsApp chat is called “{args.to}”")
            typed = await self._select(COMPOSE, action="type", text=args.text, wait_ms=10_000)
            if not typed.get("done"):
                return ToolResult.fail("the chat opened, but its message box did not")
        elif not await self._select(COMPOSE, wait_ms=LOAD_MS, limit=1):
            return ToolResult.fail(f"WhatsApp could not open a chat with {args.to}")
        header = await self._select(OPEN_CHAT, limit=1)
        chat = header[0]["text"] if header else args.to
        await self.session.bring_forward("WhatsApp")
        data = {"chat": chat, "text": args.text}
        exact = phone is not None or chat.strip().lower() == args.to.strip().lower()
        if not args.send or not exact:
            why = "" if exact else f": the chat that opened is “{chat}”, not “{args.to}”"
            return ToolResult.ok(
                f"Typed the message in the chat with {chat}, not sent{why}",
                data={**data, "sent": False},
            )
        pressed = await self._select(SEND, action="click", wait_ms=5000)
        if not pressed.get("done"):
            return ToolResult.fail(f"typed it in the chat with {chat}, but found no Send button")
        left = await self._select(COMPOSE, limit=1)
        if left and left[0].get("text"):
            return ToolResult.fail(
                f"pressed Send in the chat with {chat}, but the text is still there"
            )
        return ToolResult.ok(f"Sent to {chat} on WhatsApp", data={**data, "sent": True})
