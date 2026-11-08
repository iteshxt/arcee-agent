"""Attachments on sent mail, unsubscribing, and sending files to oneself."""

from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest

from arcee.core.policy import Policy, Tier
from arcee.core.toolsets import MAIL, filters_for
from arcee.models.base import Message
from arcee.tools.mail import (
    MailSend,
    MailUnsubscribe,
    ShareArgs,
    ShareFile,
    UnsubscribeArgs,
    WriteArgs,
    unsubscribe_ways,
)

from .test_mail import FakeGmail, tool_ctx


class Me(FakeGmail):
    async def address(self) -> str:
        return "me@gmail.com"


def newsletter(*headers: tuple[str, str]) -> bytes:
    message = EmailMessage()
    message["From"] = "Swiggy <news@swiggy.in>"
    message["Subject"] = "Deals"
    for name, value in headers:
        message[name] = value
    message.set_content("Deals!")
    return message.as_bytes()


async def test_a_file_is_attached_and_named_in_the_question(tmp_path: Path) -> None:
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "resume.pdf").write_bytes(b"%PDF-1.7 resume")
    gmail = FakeGmail()
    tool = MailSend(gmail)  # pyright: ignore[reportArgumentType]
    args = WriteArgs(
        to=["hr@example.com"],
        subject="Resume",
        body="Attached.",
        attachments=["~/Documents/resume.pdf"],
    )
    assert tool.preview(args)[-1] == "Attached: resume.pdf"
    result = await tool.run(args, tool_ctx(tmp_path))
    (sent,) = gmail.sent
    (part,) = list(sent.iter_attachments())
    assert (part.get_filename(), part.get_content_type()) == ("resume.pdf", "application/pdf")
    assert part.get_payload(decode=True) == b"%PDF-1.7 resume"
    assert result.summary == "Sent “Resume” to hr@example.com with 1 attachments"


async def test_attachments_must_be_files_in_home_and_not_too_big(tmp_path: Path) -> None:
    tool = MailSend(FakeGmail())  # pyright: ignore[reportArgumentType]
    missing = await tool.run(
        WriteArgs(to=["a@b.c"], body="x", attachments=["nope.pdf"]), tool_ctx(tmp_path)
    )
    outside = await tool.run(
        WriteArgs(to=["a@b.c"], body="x", attachments=["/etc/hostname"]), tool_ctx(tmp_path)
    )
    assert missing.error == "nope.pdf is not a file"
    assert outside.error == "/etc/hostname is outside your home folder"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (
            [
                ("List-Unsubscribe", "<mailto:u@swiggy.in?subject=stop>, <https://swiggy.in/u/1>"),
                ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
            ],
            ("https://swiggy.in/u/1", "mailto:u@swiggy.in?subject=stop", True),
        ),
        ([("List-Unsubscribe", "<https://swiggy.in/u/1>")], ("https://swiggy.in/u/1", None, False)),
        ([], (None, None, False)),
    ],
)
def test_the_ways_an_email_offers_to_unsubscribe(
    headers: list[tuple[str, str]], expected: tuple[str | None, str | None, bool]
) -> None:
    assert unsubscribe_ways(newsletter(*headers)) == expected


async def test_one_click_unsubscribe_posts_and_asks_first(tmp_path: Path) -> None:
    posted: list[bytes] = []

    def answer(request: httpx.Request) -> httpx.Response:
        posted.append(request.content)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    gmail = FakeGmail(
        newsletter(
            ("List-Unsubscribe", "<https://swiggy.in/u/1>"),
            ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
        )
    )
    tool = MailUnsubscribe(gmail, client)  # pyright: ignore[reportArgumentType]
    args = UnsubscribeArgs(message_id="1782", sender="Swiggy")
    assert tool.tier_for(args, Policy()) is Tier.CONFIRM
    assert tool.sentence(args) == "Unsubscribe from Swiggy's emails?"
    result = await tool.run(args, tool_ctx(tmp_path))
    await client.aclose()
    assert posted == [b"List-Unsubscribe=One-Click"]
    assert result.summary == "Unsubscribed from Swiggy (one-click)"


async def test_a_mailto_unsubscribe_sends_the_email(tmp_path: Path) -> None:
    gmail = FakeGmail(newsletter(("List-Unsubscribe", "<mailto:u@swiggy.in?subject=stop%20it>")))
    tool = MailUnsubscribe(gmail)  # pyright: ignore[reportArgumentType]
    result = await tool.run(UnsubscribeArgs(message_id="1782", sender="Swiggy"), tool_ctx(tmp_path))
    (sent,) = gmail.sent
    assert (sent["To"], sent["Subject"]) == ("u@swiggy.in", "stop it")
    assert result.data == {"how": "email", "to": "u@swiggy.in"}


async def test_a_website_unsubscribe_is_handed_back_as_a_link(tmp_path: Path) -> None:
    gmail = FakeGmail(newsletter(("List-Unsubscribe", "<https://swiggy.in/u/1>")))
    tool = MailUnsubscribe(gmail)  # pyright: ignore[reportArgumentType]
    result = await tool.run(UnsubscribeArgs(message_id="1782", sender="Swiggy"), tool_ctx(tmp_path))
    assert result.data == {"how": "website", "link": "https://swiggy.in/u/1"}
    assert gmail.sent == []


async def test_a_file_is_sent_to_the_users_own_gmail(tmp_path: Path) -> None:
    (tmp_path / "ticket.png").write_bytes(b"\x89PNG")
    gmail = Me()
    tool = ShareFile(gmail)  # pyright: ignore[reportArgumentType]
    args = ShareArgs(paths=["ticket.png"])
    assert tool.tier_for(args, Policy()) is Tier.CONFIRM
    result = await tool.run(args, tool_ctx(tmp_path))
    (sent,) = gmail.sent
    assert sent["To"] == "me@gmail.com"
    assert [p.get_filename() for p in sent.iter_attachments()] == ["ticket.png"]
    assert result.summary == "Sent ticket.png to me@gmail.com: open it on your phone's Gmail"


@pytest.mark.parametrize(
    "said",
    [
        "Unsubscribe me from this newsletter",
        "Send this photo to my phone",
        "Email my resume to hr@x.com",
    ],
)
def test_these_bring_the_mail_tools(said: str) -> None:
    assert set(MAIL) <= set(filters_for([Message("user", said)]))
