"""The user's Gmail, through GNOME Online Accounts (decision 0017).

IMAP with GOA's token (XOAUTH2) to read, search (Gmail's own syntax: X-GM-RAW), keep
drafts and change state; SMTP to send. Gmail's REST API refuses GNOME's token; IMAP is
what GNOME's own mail apps use. Reading never marks mail as read (BODY.PEEK, EXAMINE).

What Arcee may do (decision 0017's table): list, search and read run; drafts run and
stay in Drafts; saving attachments runs (into ~/Downloads, never opened); sending and
changing mail (read, archive, Trash) ask first, every time, with what will happen; at
most 50 messages at once. Nothing deletes for good or changes a setting: no tool can.
Mail is outside content and personal: never instructions, and to a cloud model only as
the user chose (Permissions → Privacy).
"""

import asyncio
import contextlib
import email
import email.policy
import imaplib
import mimetypes
import re
import smtplib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, TypeVar, cast

from pydantic import Field

from arcee.core.policy import Tier
from arcee.google import Account, GoogleAccount, GoogleError
from arcee.tools.base import Tool, ToolArgs, ToolContext, ToolResult, Usage
from arcee.tools.web import extract_text

IMAP_HOST: Final = "imap.gmail.com"
SMTP_HOST: Final = "smtp.gmail.com"
SMTP_PORT: Final = 587
MAX_LIST: Final = 25
MAX_UPDATE: Final = 50
READ_CHARS: Final = 6000
SUBJECT_CHARS: Final = 120
PREVIEW_LINES: Final = 10
MAX_ATTACHMENTS: Final = 10
MAX_ATTACH_BYTES: Final = 20_000_000  # Gmail's 25 MB, less the encoding's third
HEADERS: Final = "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)]"
T = TypeVar("T")


class MailError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Envelope:
    message_id: str  # Gmail's X-GM-MSGID: one id for the message in every folder
    sender: str
    to: str
    subject: str
    date: str
    unread: bool

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "message_id": self.message_id,
            "from": self.sender,
            "subject": self.subject or "(no subject)",
            "date": self.date,
        }
        if self.unread:
            out["unread"] = True
        return out


@dataclass(frozen=True, slots=True)
class Letter:
    envelope: Envelope
    text: str
    cc: str = ""
    attachments: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    header_id: str = ""  # the Message-ID header, for replies
    references: str = ""


# --- reading messages -----------------------------------------------------------------


def _decoded(message: Any, name: str) -> str:
    return " ".join(str(message.get(name, "") or "").split())


def _when(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).astimezone().strftime("%a %d %b %Y, %H:%M")
    except TypeError, ValueError:
        return raw


def _who(raw: str) -> str:
    """'Rahul <rahul@x.com>' as "Rahul <rahul@x.com>"; several, comma-separated."""
    # Written out, not formataddr: that encodes "Kämper" again for sending (=?utf-8?q?...).
    return ", ".join(f"{name} <{addr}>" if name else addr for name, addr in getaddresses([raw]))


_META: Final = re.compile(rb"UID (\d+).*?X-GM-MSGID (\d+).*?FLAGS \(([^)]*)\)", re.DOTALL)
_META_ALT: Final = re.compile(rb"X-GM-MSGID (\d+).*?UID (\d+).*?FLAGS \(([^)]*)\)", re.DOTALL)


def parse_envelopes(data: Sequence[Any]) -> list[Envelope]:
    """imaplib's FETCH answer for (UID X-GM-MSGID FLAGS HEADERS) as envelopes."""
    out: list[Envelope] = []
    for item in data:
        if not isinstance(item, tuple):
            continue
        meta, headers = cast(tuple[bytes, bytes], item)[:2]
        msgid, flags = "", b""
        if m := _META.search(meta):
            msgid, flags = m.group(2).decode(), m.group(3)
        elif m := _META_ALT.search(meta):
            msgid, flags = m.group(1).decode(), m.group(3)
        message = email.message_from_bytes(headers, policy=email.policy.default)
        out.append(
            Envelope(
                message_id=msgid,
                sender=_who(_decoded(message, "From")),
                to=_who(_decoded(message, "To")),
                subject=_decoded(message, "Subject")[:SUBJECT_CHARS],
                date=_when(_decoded(message, "Date")),
                unread=b"\\Seen" not in flags,
            )
        )
    return out


_QUOTED_START: Final = re.compile(
    r"^(On .{5,200} wrote:|-+ ?Original Message ?-+|-+ ?Forwarded message ?-+|From: .+ Sent: .+)$",
    re.IGNORECASE,
)


def readable(raw: bytes, message_id: str = "") -> Letter:
    """A message's text (plain, else HTML made text), quoted history cut, attachments named."""
    message: Any = email.message_from_bytes(raw, policy=email.policy.default)
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        text = extract_text(content)[1] if body.get_content_type() == "text/html" else content
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if _QUOTED_START.match(line.strip()):
            break  # what follows is the earlier message, quoted
        if not line.lstrip().startswith(">"):
            kept.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if len(text) > READ_CHARS:
        text = text[:READ_CHARS] + "\n… (the rest of the email is cut)"
    attachments = [
        (str(part.get_filename() or "attachment"), len(part.get_content() or b""))
        for part in message.iter_attachments()
    ]
    envelope = Envelope(
        message_id=message_id,
        sender=_who(_decoded(message, "From")),
        to=_who(_decoded(message, "To")),
        subject=_decoded(message, "Subject")[:SUBJECT_CHARS],
        date=_when(_decoded(message, "Date")),
        unread=False,
    )
    return Letter(
        envelope,
        text,
        cc=_who(_decoded(message, "Cc")),
        attachments=attachments,
        header_id=_decoded(message, "Message-ID"),
        references=_decoded(message, "References"),
    )


def gmail_query(text: str) -> str:
    """A Gmail search as one IMAP quoted string."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- the mailbox ----------------------------------------------------------------------


_LIST_LINE: Final = re.compile(r'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$')


def special_folders(lines: Sequence[bytes]) -> dict[str, str]:
    """Gmail's folders by use (\\All, \\Drafts, \\Trash, \\Sent), whatever their language."""
    found: dict[str, str] = {}
    for raw in lines:
        if m := _LIST_LINE.match(raw.decode(errors="replace")):
            name = m.group("name").strip()
            for flag in m.group("flags").split():
                if flag in {"\\All", "\\Drafts", "\\Trash", "\\Sent"}:
                    found[flag] = name if name.startswith('"') else f'"{name}"'
    return found


class Gmail:
    """IMAP and SMTP with a token from GOA; each call logs in, works, and logs out."""

    def __init__(self, account: GoogleAccount | None = None) -> None:
        self._account = account or GoogleAccount()

    async def _imap(self, work: Callable[[imaplib.IMAP4_SSL, dict[str, str], Account], T]) -> T:
        token, account = await self._account.token("mail")

        def session() -> T:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
            try:
                auth = f"user={account.email}\x01auth=Bearer {token}\x01\x01".encode()
                imap.authenticate("XOAUTH2", lambda _challenge: auth)
                _, lines = imap.list()
                folders = special_folders([x for x in lines if isinstance(x, bytes)])
                return work(imap, folders, account)
            except imaplib.IMAP4.error as exc:
                raise MailError(f"Gmail refused it: {exc}") from exc
            finally:
                with contextlib.suppress(imaplib.IMAP4.error, OSError):
                    imap.logout()

        try:
            return await asyncio.to_thread(session)
        except OSError as exc:
            raise MailError(f"Gmail did not answer ({type(exc).__name__})") from exc

    @staticmethod
    def _uids(imap: imaplib.IMAP4_SSL, *criteria: str) -> list[bytes]:
        status, found = imap.uid("SEARCH", *criteria)
        if status != "OK":
            raise MailError(f"search failed: {found}")
        return found[0].split() if found and found[0] else []

    @staticmethod
    def _envelopes(imap: imaplib.IMAP4_SSL, uids: list[bytes]) -> list[Envelope]:
        if not uids:
            return []
        _, data = imap.uid("FETCH", b",".join(uids).decode(), f"(UID X-GM-MSGID FLAGS {HEADERS})")
        return parse_envelopes(data)

    async def envelopes(self, *, unread_only: bool, query: str, n: int) -> list[Envelope]:
        """The newest `n` of the inbox (or its unread), or of all mail matching `query`."""

        def work(
            imap: imaplib.IMAP4_SSL, folders: dict[str, str], _account: Account
        ) -> list[Envelope]:
            if query:
                imap.select(folders.get("\\All", "INBOX"), readonly=True)
                criteria = ["X-GM-RAW", gmail_query(query + (" is:unread" if unread_only else ""))]
            else:
                imap.select("INBOX", readonly=True)
                criteria = ["UNSEEN" if unread_only else "ALL"]
            uids = self._uids(imap, *criteria)[-n:]
            found = self._envelopes(imap, uids)
            return sorted(found, key=lambda e: int(e.message_id or 0), reverse=True)

        return await self._imap(work)

    async def fetch(self, message_id: str) -> tuple[Letter, bytes]:
        def work(
            imap: imaplib.IMAP4_SSL, folders: dict[str, str], _account: Account
        ) -> tuple[Letter, bytes]:
            imap.select(folders.get("\\All", "INBOX"), readonly=True)
            uids = self._uids(imap, "X-GM-MSGID", _digits(message_id))
            if not uids:
                raise MailError("no such email: use a message_id from mail.list or mail.search")
            _, data = imap.uid("FETCH", uids[0].decode(), "(BODY.PEEK[])")
            raw = next((cast(bytes, item[1]) for item in data if isinstance(item, tuple)), b"")
            return readable(raw, message_id), raw

        return await self._imap(work)

    async def draft(self, message: EmailMessage) -> None:
        def work(imap: imaplib.IMAP4_SSL, folders: dict[str, str], account: Account) -> None:
            message["From"] = account.email
            folder = folders.get("\\Drafts")
            if folder is None:
                raise MailError("Gmail has no Drafts folder here")
            imap.append(
                folder, "(\\Draft)", imaplib.Time2Internaldate(time.time()), message.as_bytes()
            )

        await self._imap(work)

    async def update(
        self, message_ids: Sequence[str], subjects: Sequence[str], action: str
    ) -> list[Envelope]:
        """Mark read or unread, archive (out of the inbox) or move to Trash: only when each
        email found has a subject the user was shown, else nothing changes."""

        def work(
            imap: imaplib.IMAP4_SSL, folders: dict[str, str], _account: Account
        ) -> list[Envelope]:
            imap.select(folders.get("\\All", "INBOX"))
            uids = [u for m in message_ids for u in self._uids(imap, "X-GM-MSGID", _digits(m))[:1]]
            if len(uids) != len(message_ids):
                raise MailError("some of those emails were not found: list them again")
            changed = self._envelopes(imap, uids)
            shown = {s.strip().lower() for s in subjects}
            if strays := [e.subject for e in changed if e.subject.strip().lower() not in shown]:
                raise MailError(
                    f"those are not the emails listed ({', '.join(strays[:3])}): nothing changed; "
                    "list them again"
                )
            store = {
                "read": ("+FLAGS", "(\\Seen)"),
                "unread": ("-FLAGS", "(\\Seen)"),
                "archive": ("-X-GM-LABELS", "(\\Inbox)"),
                "trash": ("+X-GM-LABELS", "(\\Trash)"),
            }[action]
            imap.uid("STORE", b",".join(uids).decode(), *store)
            return changed

        return await self._imap(work)

    async def address(self) -> str:
        """The user's own Gmail address."""
        _, account = await self._account.token("mail")
        return account.email

    async def send(self, message: EmailMessage) -> None:
        token, account = await self._account.token("mail")
        message["From"] = account.email

        def deliver() -> None:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.starttls()
                auth = f"user={account.email}\x01auth=Bearer {token}\x01\x01"

                def xoauth2(_challenge: bytes | None = None) -> str:
                    return auth

                smtp.auth("XOAUTH2", xoauth2, initial_response_ok=True)
                smtp.send_message(message)  # Gmail files it in Sent itself

        try:
            await asyncio.to_thread(deliver)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"Gmail did not send it: {exc}") from exc


def _digits(message_id: str) -> str:
    if not message_id.strip().isdigit():
        raise MailError("that is not a message_id from mail.list or mail.search")
    return message_id.strip()


def compose(
    to: Sequence[str],
    subject: str,
    body: str,
    cc: Sequence[str] = (),
    replying: Letter | None = None,
    forwarding: Letter | None = None,
) -> EmailMessage:
    """The email to send or keep as a draft: a reply threads under the one it answers."""
    message = EmailMessage()
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if replying is not None:
        original = replying.envelope.subject
        subject = subject or (original if original.lower().startswith("re:") else f"Re: {original}")
        if replying.header_id:
            message["In-Reply-To"] = replying.header_id
            message["References"] = f"{replying.references} {replying.header_id}".strip()
    if forwarding is not None:
        original = forwarding.envelope
        subject = subject or f"Fwd: {original.subject}"
        body += (
            f"\n\n---------- Forwarded message ----------\nFrom: {original.sender}\n"
            f"Date: {original.date}\nSubject: {original.subject}\n\n{forwarding.text}"
        )
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="arcee.local")
    message.set_content(body)
    return message


# --- the tools --------------------------------------------------------------------------


class ListArgs(ToolArgs):
    unread_only: bool = Field(default=False, description="Only unread mail")
    n: int = Field(default=10, ge=1, le=MAX_LIST, description="How many, newest first")


class MailList(Tool[ListArgs]):
    name = "mail.list"
    read_only = True
    untrusted = True
    personal = "your emails"
    description = (
        "The newest emails in the user's Gmail inbox (or only unread): from, subject, date, "
        "message_id."
    )
    usage = Usage(
        when='"Do I have new emails?", "check my inbox", "anything unread?"',
        avoid="Opening Gmail in Chrome. Finding particular mail: mail.search.",
        example="mail.list(unread_only=true, n=10)",
    )
    args_model = ListArgs
    timeout_s = 45

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    async def run(self, args: ListArgs, ctx: ToolContext) -> ToolResult:
        try:
            found = await self._gmail.envelopes(unread_only=args.unread_only, query="", n=args.n)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        what = "unread emails" if args.unread_only else "newest emails"
        return ToolResult.ok(
            f"{len(found)} {what} in the inbox", data={"emails": [e.public() for e in found]}
        )


class SearchArgs(ToolArgs):
    query: str = Field(
        description="Gmail search: words, from:, to:, subject:, has:attachment, newer_than:7d, "
        "older_than:, after:YYYY/MM/DD, is:unread, in:sent, label:"
    )
    n: int = Field(default=10, ge=1, le=MAX_LIST)


class MailSearch(Tool[SearchArgs]):
    name = "mail.search"
    read_only = True
    untrusted = True
    personal = "your emails"
    description = "Find emails in all of the user's Gmail with Gmail's own search: newest first."
    usage = Usage(
        when='"Find the email from the bank about my statement", "Rahul\'s emails this week", '
        '"emails with attachments from HR".',
        mistakes="Long sentences as the query: Gmail matches words and operators.",
        example='mail.search(query="from:bank statement newer_than:30d")',
    )
    args_model = SearchArgs
    timeout_s = 45

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    async def run(self, args: SearchArgs, ctx: ToolContext) -> ToolResult:
        try:
            found = await self._gmail.envelopes(unread_only=False, query=args.query, n=args.n)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(
            f"{len(found)} emails match {args.query!r}",
            data={"emails": [e.public() for e in found]},
        )


class ReadArgs(ToolArgs):
    message_id: str = Field(description="A message_id from mail.list or mail.search")


class MailRead(Tool[ReadArgs]):
    name = "mail.read"
    read_only = True
    untrusted = True
    personal = "your emails"
    description = (
        "Read one email: from, to, date, subject, its text (earlier quoted messages cut), and "
        "the names of its attachments. It stays unread if it was."
    )
    usage = Usage(
        when='"Read me the latest email", "what does Rahul\'s email say?"',
        avoid="Several emails at once: their subjects from mail.list may be enough.",
        example='mail.read(message_id="1782345678901234567")',
    )
    args_model = ReadArgs
    timeout_s = 45

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    async def run(self, args: ReadArgs, ctx: ToolContext) -> ToolResult:
        try:
            letter, _ = await self._gmail.fetch(args.message_id)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        data = letter.envelope.public() | {"to": letter.envelope.to, "text": letter.text}
        if letter.cc:
            data["cc"] = letter.cc
        if letter.attachments:
            data["attachments"] = [
                {"name": n, "size_kb": max(1, s // 1024)} for n, s in letter.attachments
            ]
        return ToolResult.ok(
            f"Read “{letter.envelope.subject}” from {letter.envelope.sender}", data=data
        )


class AttachmentsArgs(ToolArgs):
    message_id: str = Field(description="A message_id from mail.list or mail.search")
    names: list[str] = Field(
        default_factory=list[str], description="Which (from mail.read); empty for all"
    )


class MailAttachments(Tool[AttachmentsArgs]):
    name = "mail.attachments"
    personal = "your emails"
    description = "Save an email's attachments into ~/Downloads (never opens them); returns where."
    usage = Usage(
        when='"Save the attachments from Rahul\'s email", "download the PDF the bank sent".',
        example='mail.attachments(message_id="1782345678901234567")',
    )
    args_model = AttachmentsArgs
    timeout_s = 60

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    async def run(self, args: AttachmentsArgs, ctx: ToolContext) -> ToolResult:
        try:
            _, raw = await self._gmail.fetch(args.message_id)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        message: Any = email.message_from_bytes(raw, policy=email.policy.default)
        wanted = {n.lower() for n in args.names}
        folder = ctx.home / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for part in message.iter_attachments():
            name = Path(str(part.get_filename() or "attachment")).name  # no paths from mail
            if wanted and name.lower() not in wanted:
                continue
            target = _free_name(folder / name)
            content = part.get_content()
            target.write_bytes(content if isinstance(content, bytes) else str(content).encode())
            saved.append(f"~/Downloads/{target.name}")
        if not saved:
            return ToolResult.fail("that email has no such attachments")
        return ToolResult.ok(
            f"Saved {len(saved)} attachments to ~/Downloads", data={"saved": saved}
        )


def _free_name(path: Path) -> Path:
    """`path`, or `name (2).ext` and on when it is taken: nothing is overwritten."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise MailError(f"too many files called {path.name}")


class WriteArgs(ToolArgs):
    to: list[str] = Field(description="Recipients' email addresses")
    subject: str = Field(default="", description="Empty for 'Re: …' or 'Fwd: …' of the original")
    body: str = Field(description="The email's text, written as the user would")
    cc: list[str] = Field(default_factory=list[str])
    reply_to: str = Field(default="", description="message_id of the email this replies to")
    forward: str = Field(default="", description="message_id of the email this forwards")
    attachments: list[str] = Field(
        default_factory=list[str],
        max_length=MAX_ATTACHMENTS,
        description="Files to attach, by path (~/Documents/resume.pdf)",
    )


def attachment_files(paths: Sequence[str], home: Path) -> list[Path]:
    """The files to attach: inside home, existing, MAX_ATTACH_BYTES in all."""
    files: list[Path] = []
    for raw in paths:
        path = home / raw[2:] if raw.startswith("~/") else Path(raw)
        path = path if path.is_absolute() else home / path
        if not path.is_file():
            raise MailError(f"{raw} is not a file")
        if home not in path.resolve().parents:
            raise MailError(f"{raw} is outside your home folder")
        files.append(path)
    total = sum(p.stat().st_size for p in files)
    if total > MAX_ATTACH_BYTES:
        raise MailError(
            f"the attachments come to {total / 1e6:.1f} MB; Gmail takes 25 MB at most "
            "(share a larger file with a Drive link)"
        )
    return files


def attach(message: EmailMessage, files: Sequence[Path]) -> None:
    for path in files:
        kind, _ = mimetypes.guess_type(path.name)
        main, sub = (kind or "application/octet-stream").split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)


async def _composed(gmail: Gmail, args: WriteArgs, home: Path) -> EmailMessage:
    replying = (await gmail.fetch(args.reply_to))[0] if args.reply_to else None
    forwarding = (await gmail.fetch(args.forward))[0] if args.forward else None
    message = compose(args.to, args.subject, args.body, args.cc, replying, forwarding)
    if args.attachments:
        files = await asyncio.to_thread(attachment_files, args.attachments, home)
        await asyncio.to_thread(attach, message, files)
    return message


class MailDraft(Tool[WriteArgs]):
    name = "mail.draft"
    description = "Write an email and leave it in the user's Gmail Drafts, not sent."
    usage = Usage(
        when='"Draft a reply to Rahul", "write an email to HR and I\'ll send it myself".',
        avoid="Sending it when the user asked to send: mail.send (which asks them first).",
        example='mail.draft(to=["hr@example.com"], subject="Leave on Friday", body="Hi, …")',
    )
    args_model = WriteArgs
    timeout_s = 45

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    async def run(self, args: WriteArgs, ctx: ToolContext) -> ToolResult:
        try:
            message = await _composed(self._gmail, args, ctx.home)
            await self._gmail.draft(message)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(f"Saved a draft to {', '.join(args.to)}: “{message['Subject']}”")


class MailSend(Tool[WriteArgs]):
    name = "mail.send"
    tier = Tier.CONFIRM
    risk = "high"
    description = (
        "Send an email from the user's Gmail: new, a reply (reply_to) or a forward (forward). "
        "The user sees it whole and confirms first, every time."
    )
    usage = Usage(
        when='"Email Rahul that I\'ll be late", "reply yes to the last one", "forward it".',
        avoid="Addresses you are not sure of: find them first (mail.search from:, the email "
        "being answered, contacts), or ask.",
        mistakes="Asking the user whether to send: call it, and Arcee shows them the whole email "
        "and asks. Opening Gmail in Chrome to send.",
        example='mail.send(to=["rahul@example.com"], subject="Running late", body="Hi Rahul, …")',
    )
    args_model = WriteArgs
    timeout_s = 60

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    def sentence(self, args: WriteArgs) -> str:
        kind = "this reply" if args.reply_to else "this forward" if args.forward else "this email"
        return f"Send {kind} to {', '.join(args.to)}?"

    def preview(self, args: WriteArgs) -> tuple[str, ...]:
        lines = [f"To: {', '.join(args.to)}"]
        if args.cc:
            lines.append(f"Cc: {', '.join(args.cc)}")
        lines.append(f"Subject: {args.subject or '(Re:/Fwd: the original)'}")
        body = [line for line in args.body.splitlines() if line.strip()]
        lines += body[:PREVIEW_LINES]
        if len(body) > PREVIEW_LINES:
            lines.append(f"… and {len(body) - PREVIEW_LINES} more lines")
        if args.forward:
            lines.append("(with the forwarded email below)")
        if args.attachments:
            lines.append(f"Attached: {', '.join(Path(a).name for a in args.attachments)}")
        return tuple(lines)

    async def run(self, args: WriteArgs, ctx: ToolContext) -> ToolResult:
        try:
            message = await _composed(self._gmail, args, ctx.home)
            await self._gmail.send(message)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        attached = f" with {len(args.attachments)} attachments" if args.attachments else ""
        return ToolResult.ok(f"Sent “{message['Subject']}” to {', '.join(args.to)}{attached}")


Action = Literal["read", "unread", "archive", "trash"]


class UpdateArgs(ToolArgs):
    message_ids: list[str] = Field(description="message_ids from mail.list or mail.search")
    subjects: list[str] = Field(
        description="Their subjects as listed (shown to the user to confirm)"
    )
    action: Action = Field(description="read, unread, archive (out of the inbox) or trash")


class MailUpdate(Tool[UpdateArgs]):
    name = "mail.update"
    tier = Tier.CONFIRM
    risk = "medium"
    description = (
        "Mark emails read or unread, archive them, or move them to Trash (recoverable for 30 "
        "days); at most 50, and the user confirms first."
    )
    usage = Usage(
        when='"Mark these as read", "archive the newsletters", "delete the promotions emails".',
        mistakes="message_ids not from mail.list or mail.search: list first.",
        example='mail.update(message_ids=["178…"], subjects=["Weekly digest"], action="archive")',
    )
    args_model = UpdateArgs
    timeout_s = 60

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    def sentence(self, args: UpdateArgs) -> str:
        verb = {
            "read": "Mark as read",
            "unread": "Mark as unread",
            "archive": "Archive",
            "trash": "Move to Trash",
        }
        n = len(args.message_ids)
        return f"{verb[args.action]} {n} email{'s' if n != 1 else ''}?"

    def preview(self, args: UpdateArgs) -> tuple[str, ...]:
        shown = [f"“{s}”" for s in args.subjects[:8]]
        if len(args.subjects) > 8:
            shown.append(f"… and {len(args.subjects) - 8} more")
        return tuple(shown)

    async def run(self, args: UpdateArgs, ctx: ToolContext) -> ToolResult:
        if not args.message_ids or len(args.message_ids) > MAX_UPDATE:
            return ToolResult.fail(f"give between 1 and {MAX_UPDATE} message_ids")
        try:
            changed = await self._gmail.update(args.message_ids, args.subjects, args.action)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        done = {
            "read": "Marked read",
            "unread": "Marked unread",
            "archive": "Archived",
            "trash": "Moved to Trash",
        }
        summary = f"{done[args.action]}: {len(changed)} emails"
        return ToolResult.ok(summary, data={"emails": [e.public() for e in changed]})


# --- mail.unsubscribe and share.file -----------------------------------------------------


def unsubscribe_ways(raw: bytes) -> tuple[str | None, str | None, bool]:
    """From an email's List-Unsubscribe headers: (https link, mailto address, one-click),
    one-click being RFC 8058's: a POST to the link unsubscribes, with nothing to click."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    header = str(message.get("List-Unsubscribe", ""))
    links = re.findall(r"<([^>]+)>", header) or [h.strip() for h in header.split(",") if h.strip()]
    web = next((link for link in links if link.lower().startswith("https://")), None)
    mailto = next((link for link in links if link.lower().startswith("mailto:")), None)
    post = str(message.get("List-Unsubscribe-Post", ""))
    return web, mailto, bool(web) and "one-click" in post.lower()


class UnsubscribeArgs(ToolArgs):
    message_id: str = Field(description="message_id of an email from the list (mail.list)")
    sender: str = Field(description="Who it is from, as listed (shown to the user)")


class MailUnsubscribe(Tool[UnsubscribeArgs]):
    name = "mail.unsubscribe"
    tier = Tier.CONFIRM
    risk = "medium"
    description = (
        "Unsubscribe from a mailing list or newsletter, the way its email offers "
        "(List-Unsubscribe): one-click, or an unsubscribe email from the user's Gmail."
    )
    usage = Usage(
        when='"Unsubscribe me from this newsletter", "stop these emails from Swiggy".',
        mistakes="Clicking unsubscribe links in the email's text yourself: this tool does it "
        "the safe way; if it returns a link, open it in the browser for the user to finish.",
        example='mail.unsubscribe(message_id="178…", sender="Swiggy")',
    )
    args_model = UnsubscribeArgs
    timeout_s = 60

    def __init__(self, gmail: Gmail | None = None, client: Any = None) -> None:
        self._gmail = gmail or Gmail()
        self._client = client

    def sentence(self, args: UnsubscribeArgs) -> str:
        return f"Unsubscribe from {args.sender}'s emails?"

    async def run(self, args: UnsubscribeArgs, ctx: ToolContext) -> ToolResult:
        import httpx

        try:
            _letter, raw = await self._gmail.fetch(args.message_id)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        web, mailto, one_click = unsubscribe_ways(raw)
        if one_click and web:
            client = self._client or httpx.AsyncClient(timeout=20, follow_redirects=True)
            try:
                response = await client.post(
                    web,
                    content=b"List-Unsubscribe=One-Click",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                return ToolResult.fail(f"{args.sender}'s unsubscribe did not answer: {exc}")
            finally:
                if self._client is None:
                    await client.aclose()
            if response.status_code < 400:
                return ToolResult.ok(
                    f"Unsubscribed from {args.sender} (one-click)", data={"how": "one-click"}
                )
        if mailto:
            address = mailto[len("mailto:") :].split("?", 1)
            subject = "unsubscribe"
            if len(address) > 1 and (m := re.search(r"subject=([^&]+)", address[1])):
                from urllib.parse import unquote

                subject = unquote(m[1])
            message = compose([address[0]], subject, "Please unsubscribe me from this list.")
            try:
                await self._gmail.send(message)
            except (MailError, GoogleError) as exc:
                return ToolResult.fail(str(exc))
            return ToolResult.ok(
                f"Asked {args.sender} to unsubscribe you (an email to {address[0]})",
                data={"how": "email", "to": address[0]},
            )
        if web:
            return ToolResult.ok(
                f"{args.sender} unsubscribes on its website: open the link for the user to "
                "finish there",
                data={"how": "website", "link": web},
            )
        return ToolResult.fail(
            f"{args.sender}'s email offers no unsubscribe; archive or filter it instead"
        )


class ShareArgs(ToolArgs):
    paths: list[str] = Field(
        min_length=1, max_length=MAX_ATTACHMENTS, description="The files to send to yourself"
    )
    note: str = Field(default="", description="A line to go with them")


class ShareFile(Tool[ShareArgs]):
    name = "share.file"
    tier = Tier.CONFIRM
    description = (
        "Send files to the user's own phone or other devices: an email to their own Gmail "
        "with the files attached."
    )
    usage = Usage(
        when='"Send this photo to my phone", "get this PDF onto my phone", "mail it to myself".',
        avoid="Sending files to someone else: mail.send with attachments.",
        example='share.file(paths=["~/Pictures/ticket.png"])',
    )
    args_model = ShareArgs
    timeout_s = 90

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    def sentence(self, args: ShareArgs) -> str:
        names = ", ".join(Path(p).name for p in args.paths)
        return f"Email {names} to yourself (your Gmail)?"

    async def run(self, args: ShareArgs, ctx: ToolContext) -> ToolResult:
        try:
            files = await asyncio.to_thread(attachment_files, args.paths, ctx.home)
            me = await self._gmail.address()
            names = ", ".join(p.name for p in files)
            message = compose([me], f"From your computer: {names}", args.note or names)
            await asyncio.to_thread(attach, message, files)
            await self._gmail.send(message)
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        return ToolResult.ok(
            f"Sent {names} to {me}: open it on your phone's Gmail",
            data={"to": me, "files": [p.name for p in files]},
        )


# --- contacts.find ---------------------------------------------------------------------------

AUTOMATED: Final = re.compile(
    r"no-?reply|do-?not-?reply|notifications?@|newsletter|mailer|bounce|support@|info@|alerts?@",
    re.IGNORECASE,
)


def people(envelopes: Sequence[Envelope], wanted: str, me: str = "") -> list[dict[str, Any]]:
    """The addresses in `envelopes` whose name or address has `wanted` in it, with how
    often each was seen, most first; the user's own and machines' left out."""
    key = wanted.lower().strip()
    seen: dict[str, dict[str, Any]] = {}
    for envelope in envelopes:
        for name, address in getaddresses([envelope.sender, envelope.to]):
            address = address.lower()
            if not address or address == me.lower() or AUTOMATED.search(address):
                continue
            if key not in name.lower() and key not in address:
                continue
            entry = seen.setdefault(address, {"email": address, "name": name, "emails": 0})
            entry["emails"] += 1
            if name and not entry["name"]:
                entry["name"] = name
    return sorted(seen.values(), key=lambda e: e["emails"], reverse=True)


class ContactsArgs(ToolArgs):
    name: str = Field(min_length=2, max_length=80, description="Who: a name, as the user said it")


class ContactsFind(Tool[ContactsArgs]):
    name = "contacts.find"
    description = (
        "Find someone's email address from the user's Gmail: the people they have written "
        "to or heard from, by name, most frequent first."
    )
    usage = Usage(
        when="\"What's Priya's email?\", and before mail.send to someone named but not "
        'addressed ("email Rahul that ..."). Several matches: ask which.',
        avoid="Phone numbers: Arcee cannot see the phone's contacts.",
        example='contacts.find(name="Rahul")',
    )
    args_model = ContactsArgs
    read_only: ClassVar[bool] = True
    personal: ClassVar[str] = "your contacts"
    timeout_s = 45

    def __init__(self, gmail: Gmail | None = None) -> None:
        self._gmail = gmail or Gmail()

    async def run(self, args: ContactsArgs, ctx: ToolContext) -> ToolResult:
        query = " ".join(args.name.split())
        try:
            me = await self._gmail.address()
            found = await self._gmail.envelopes(
                unread_only=False, query=f"from:({query}) OR to:({query})", n=MAX_LIST * 4
            )
        except (MailError, GoogleError) as exc:
            return ToolResult.fail(str(exc))
        matches = people(found, args.name, me)[:8]
        if not matches:
            return ToolResult.ok(
                f"No one called “{args.name}” in your email", data={"contacts": []}
            )
        return ToolResult.ok(
            f"{len(matches)} people match “{args.name}”", data={"contacts": matches}
        )


def mail_tools(gmail: Gmail | None = None) -> list[Tool[Any]]:
    shared = gmail or Gmail()
    return [
        MailList(shared),
        MailSearch(shared),
        MailRead(shared),
        MailAttachments(shared),
        MailDraft(shared),
        MailSend(shared),
        MailUpdate(shared),
        MailUnsubscribe(shared),
        ShareFile(shared),
        ContactsFind(shared),
    ]
