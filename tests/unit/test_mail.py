"""Gmail over IMAP: parsing, composing, and the tools against a stand-in mailbox."""

from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path

import pytest

from arcee.core.toolsets import MAIL, filters_for
from arcee.models.base import Message
from arcee.tools.base import ToolContext
from arcee.tools.mail import (
    AttachmentsArgs,
    Envelope,
    Letter,
    ListArgs,
    MailAttachments,
    MailError,
    MailList,
    MailRead,
    MailSend,
    MailUpdate,
    ReadArgs,
    UpdateArgs,
    WriteArgs,
    compose,
    gmail_query,
    parse_envelopes,
    readable,
    special_folders,
)

HEADERS = (
    b"From: =?UTF-8?Q?Rahul_K=C3=A4mper?= <rahul@example.com>\r\n"
    b"To: me@example.com\r\nSubject: Running late\r\nDate: Wed, 04 Nov 2026 09:15:00 +0530\r\n\r\n"
)


def test_imap_fetch_answers_become_envelopes() -> None:
    data = [
        (b"1 (UID 41 X-GM-MSGID 1782 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM)] {120}", HEADERS),
        b")",
        (b"2 (X-GM-MSGID 1790 UID 42 FLAGS () BODY[HEADER.FIELDS (FROM)] {120}", HEADERS),
        b")",
    ]
    first, second = parse_envelopes(data)
    assert first.message_id == "1782"
    assert not first.unread
    assert first.sender == "Rahul Kämper <rahul@example.com>"
    assert first.subject == "Running late"
    assert first.date.startswith("Wed 04 Nov 2026")
    assert second.message_id == "1790"
    assert second.unread


def email_bytes(
    text: str = "", html: str = "", attachment: tuple[str, bytes] | None = None
) -> bytes:
    message = EmailMessage()
    message["From"] = "Bank <alerts@bank.example>"
    message["To"] = "me@example.com"
    message["Subject"] = "Your statement"
    message["Date"] = "Tue, 03 Nov 2026 18:00:00 +0530"
    message["Message-ID"] = "<abc@bank.example>"
    if text:
        message.set_content(text)
    if html:
        if text:
            message.add_alternative(html, subtype="html")
        else:
            message.set_content(html, subtype="html")
    if attachment:
        message.add_attachment(
            attachment[1], maintype="application", subtype="pdf", filename=attachment[0]
        )
    return message.as_bytes()


def test_a_message_reads_as_its_own_text() -> None:
    raw = email_bytes(
        text="Your statement is ready.\n\nThanks,\nBank\n\nOn Mon, 2 Nov 2026 Rahul wrote:\n> old",
        attachment=("statement.pdf", b"%PDF" * 600),
    )
    letter = readable(raw, "1782")
    assert letter.text == "Your statement is ready.\n\nThanks,\nBank"  # the quote is cut
    assert letter.attachments == [("statement.pdf", 2400)]
    assert letter.header_id == "<abc@bank.example>"
    assert letter.envelope.message_id == "1782"


def test_an_html_only_message_reads_as_text() -> None:
    letter = readable(
        email_bytes(html="<html><body><h1>Hi</h1><p>Pay by <b>Friday</b>.</p></body></html>")
    )
    assert "Pay by Friday." in letter.text
    assert "<" not in letter.text


def test_folders_by_use_whatever_their_language() -> None:
    lines = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\All \\HasNoChildren) "/" "[Google Mail]/Alle Nachrichten"',
        b'(\\Drafts \\HasNoChildren) "/" "[Google Mail]/Entw&APw-rfe"',
        b'(\\HasNoChildren \\Trash) "/" "[Google Mail]/Papierkorb"',
    ]
    found = special_folders(lines)
    assert found["\\All"] == '"[Google Mail]/Alle Nachrichten"'
    assert found["\\Trash"] == '"[Google Mail]/Papierkorb"'


def test_a_gmail_search_is_one_quoted_string() -> None:
    assert gmail_query('from:bank "statement"') == '"from:bank \\"statement\\""'


def letter(subject: str = "Lunch?", header_id: str = "<orig@x>") -> Letter:
    return Letter(
        Envelope("1782", "Priya <priya@example.com>", "me@example.com", subject, "Tue", False),
        "Lunch on Friday?",
        header_id=header_id,
        references="<earlier@x>",
    )


def test_a_reply_threads_under_the_email_it_answers() -> None:
    message = compose(["priya@example.com"], "", "Yes, Friday works.", replying=letter())
    assert message["Subject"] == "Re: Lunch?"
    assert message["In-Reply-To"] == "<orig@x>"
    assert message["References"] == "<earlier@x> <orig@x>"


def test_a_forward_carries_the_original() -> None:
    message = compose(["rahul@example.com"], "", "FYI", forwarding=letter())
    assert message["Subject"] == "Fwd: Lunch?"
    body = message.get_content()
    assert "---------- Forwarded message ----------" in body
    assert "Lunch on Friday?" in body


class FakeGmail:
    def __init__(self, raw: bytes = b"") -> None:
        self.raw = raw
        self.sent: list[EmailMessage] = []
        self.updated: list[tuple[Sequence[str], Sequence[str], str]] = []

    async def envelopes(self, *, unread_only: bool, query: str, n: int) -> list[Envelope]:
        return [Envelope("1782", "Rahul <rahul@example.com>", "me", "Running late", "Wed", True)][
            :n
        ]

    async def fetch(self, message_id: str) -> tuple[Letter, bytes]:
        if message_id != "1782":
            raise MailError("no such email")
        return readable(self.raw, message_id), self.raw

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    async def update(
        self, ids: Sequence[str], subjects: Sequence[str], action: str
    ) -> list[Envelope]:
        self.updated.append((ids, subjects, action))
        return [Envelope(i, "x", "me", s, "Wed", False) for i, s in zip(ids, subjects, strict=True)]


def tool_ctx(home: Path) -> ToolContext:
    return ToolContext("t_m", home)


async def test_list_and_read(tmp_path: Path) -> None:
    gmail = FakeGmail(email_bytes(text="See you at 8."))
    listed = await MailList(gmail).run(ListArgs(unread_only=True), tool_ctx(tmp_path))  # pyright: ignore[reportArgumentType]
    assert listed.summary == "1 unread emails in the inbox"
    assert listed.data is not None
    assert listed.data["emails"][0] == {
        "message_id": "1782",
        "from": "Rahul <rahul@example.com>",
        "subject": "Running late",
        "date": "Wed",
        "unread": True,
    }
    read = await MailRead(gmail).run(ReadArgs(message_id="1782"), tool_ctx(tmp_path))  # pyright: ignore[reportArgumentType]
    assert read.data is not None
    assert read.data["text"] == "See you at 8."
    assert MailRead.untrusted
    assert MailRead.personal == "your emails"


async def test_attachments_land_in_downloads_without_overwriting(tmp_path: Path) -> None:
    gmail = FakeGmail(email_bytes(text="x", attachment=("../../.bashrc", b"%PDF-1")))
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / ".bashrc").write_text("mine")

    tool = MailAttachments(gmail)  # pyright: ignore[reportArgumentType]
    result = await tool.run(AttachmentsArgs(message_id="1782"), tool_ctx(tmp_path))

    assert result.data == {"saved": ["~/Downloads/.bashrc (2)"]}
    assert (tmp_path / "Downloads" / ".bashrc").read_text() == "mine"  # untouched
    assert not (tmp_path / ".bashrc").exists()  # no path from the email is followed


async def test_send_asks_first_showing_the_whole_email(tmp_path: Path) -> None:
    gmail = FakeGmail(email_bytes(text="Lunch?"))
    tool = MailSend(gmail)  # pyright: ignore[reportArgumentType]
    args = WriteArgs(
        to=["rahul@example.com"], subject="Running late", body="Hi Rahul,\n\nBy 8.\n\nItesh"
    )

    assert tool.tier.value == "confirm"
    assert tool.sentence(args) == "Send this email to rahul@example.com?"
    assert tool.preview(args) == (
        "To: rahul@example.com",
        "Subject: Running late",
        "Hi Rahul,",
        "By 8.",
        "Itesh",
    )
    result = await tool.run(args, tool_ctx(tmp_path))
    assert result.summary == "Sent “Running late” to rahul@example.com"
    assert gmail.sent[0]["To"] == "rahul@example.com"

    reply = WriteArgs(to=["alerts@bank.example"], body="Thanks", reply_to="1782")
    assert tool.sentence(reply) == "Send this reply to alerts@bank.example?"
    await tool.run(reply, tool_ctx(tmp_path))
    assert gmail.sent[1]["In-Reply-To"] == "<abc@bank.example>"


async def test_update_asks_first_and_takes_at_most_fifty(tmp_path: Path) -> None:
    gmail = FakeGmail()
    tool = MailUpdate(gmail)  # pyright: ignore[reportArgumentType]
    args = UpdateArgs(message_ids=["1", "2"], subjects=["Sale!", "Digest"], action="trash")
    assert tool.sentence(args) == "Move to Trash 2 emails?"
    assert tool.preview(args) == ("“Sale!”", "“Digest”")

    done = await tool.run(args, tool_ctx(tmp_path))
    assert done.summary == "Moved to Trash: 2 emails"

    many = UpdateArgs(message_ids=[str(n) for n in range(51)], subjects=["x"] * 51, action="read")
    refused = await tool.run(many, tool_ctx(tmp_path))
    assert refused.status != "ok"
    assert len(gmail.updated) == 1


def test_mail_words_bring_the_mail_tools_and_not_the_browser() -> None:
    for request in ("Do I have new emails?", "Reply to Priya's email", "Check my Gmail inbox"):
        offered = filters_for([Message("user", request)])
        assert set(MAIL) <= set(offered), request
        assert "browser.*" not in offered, request


@pytest.mark.parametrize("bad", ["", "abc", "17 82"])
async def test_only_real_message_ids_are_looked_up(bad: str, tmp_path: Path) -> None:
    from arcee.tools.mail import _digits  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(MailError):
        _digits(bad)
