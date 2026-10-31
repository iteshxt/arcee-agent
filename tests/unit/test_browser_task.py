"""A whole browser task through the agent loop, against a fake Chrome that changes.

The Gmail runs failed in ways only a whole task shows: reading the page again after
a search returned the old page, and a step's closing note tripped the narration
guard. This replays that shape: open, search, read, open an email, answer.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from arcee.core.agent import Agent, Event, Guarded, ToolRan
from arcee.core.executor import Executor
from arcee.core.gate import Gate
from arcee.core.planner import Planner
from arcee.core.policy import Policy
from arcee.core.protocol import ConfirmRequest
from arcee.models.base import Completion, ModelProfile, WireCall
from arcee.models.fake import FakeAdapter
from arcee.tools.browser import BrowserSession
from arcee.tools.builtin import builtin_registry

NOW = datetime(2026, 10, 31, 9, 0)
CORE = ModelProfile(model="core", context_tokens=8192, cost_tier="local", call_mode="native")
PLANNER = CORE.model_copy(update={"model": "planner", "call_mode": "schema", "json_mode": "schema"})

INBOX = {
    "url": "https://mail.example/#inbox",
    "title": "Inbox",
    "elements": [{"id": "el_1", "role": "combobox", "name": "Search mail"}],
    "text_excerpt": "Inbox: 40 emails",
}
RESULTS = {
    "url": "https://mail.example/#search/is:unread",
    "title": "Search results",
    "elements": [{"id": "el_1", "role": "row", "name": "Interview schedule, 3 Nov 9:30"}],
    "text_excerpt": "1 unread",
}
EMAIL = "Your interview is on 3 November at 9:30 at the main office."


class FakeChrome:
    """Answers like the extension, and its page changes with what Arcee does."""

    def __init__(self) -> None:
        self.page = INBOX
        self.opened = False
        self.calls: list[str] = []

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(method)
        match method:
            case "tabs":
                return [{"id": 1, "title": self.page["title"], "url": self.page["url"]}]
            case "switch_tab" | "new_tab":
                return {"id": 1, "title": self.page["title"]}
            case "navigate":
                self.page, self.opened = INBOX, False
                return {"id": 1, "url": INBOX["url"], "title": INBOX["title"]}
            case "elements":
                return json.loads(json.dumps(self.page))
            case "fill":
                self.page = RESULTS
                return {"filled": "el_1", "submitted": True}
            case "click":
                self.opened = True
                return {"clicked": "el_1", "url": self.page["url"], "title": "Interview"}
            case "read_text":
                text = EMAIL if self.opened else self.page["text_excerpt"]
                return {"url": self.page["url"], "title": "Interview", "text": text}
            case _:
                raise AssertionError(f"unexpected {method} {params}")


class User:
    async def ask(self, question: str, options: Sequence[str] = ()) -> str:
        raise AssertionError(f"asked the user: {question}")

    async def confirm(self, request: ConfirmRequest) -> bool:
        return True


def call(name: str, **args: Any) -> Completion:
    return Completion("", tool_calls=(WireCall(name, args),))


def plan(*steps: str) -> str:
    return json.dumps(
        {
            "goal": "g",
            "interpretation": "",
            "steps": [
                {"id": i, "do": do, "tools": ["browser.*"], "success": "s"}
                for i, do in enumerate(steps, start=1)
            ],
        }
    )


async def test_a_gmail_task_reads_each_new_page_and_hands_over_between_steps() -> None:
    chrome = FakeChrome()
    registry = builtin_registry(shell_limits=False, browser=BrowserSession(client=chrome))  # pyright: ignore[reportArgumentType]
    core = FakeAdapter(
        [
            # Step 1: open Gmail and search; reading after the search must see results.
            call("browser_navigate", url="https://mail.example/"),
            call("browser_get_elements"),
            call("browser_fill", target="el_1", text="is:unread", submit=True),
            call("browser_get_elements"),
            Completion("One unread: el_1 'Interview schedule'; browser_click opens it next."),
            # Step 2: open the email and read it; reading after the click must see it.
            call("browser_get_elements"),
            call("browser_click", target="el_1"),
            call("browser_read_text"),
            Completion("Your interview is on 3 November at 9:30."),
        ],
        CORE,
    )
    events: list[Event] = []
    user = User()
    presence = Presence()
    agent = Agent(
        [core],
        registry,
        Executor(Gate(Policy(), user)),
        user,
        home=Path("/tmp"),
        planner=Planner(
            FakeAdapter([plan("Search unread mail", "Open and read it")], PLANNER),
            registry,
            home=Path("/tmp"),
            clock=lambda: NOW,
        ),
        on_event=events.append,
        clock=lambda: NOW,
        presence=presence,
    )

    outcome = await agent.run("check my gmail for unread mail and tell me what it says")

    assert (outcome.status, outcome.reply) == ("done", "Your interview is on 3 November at 9:30.")
    assert not [e for e in events if isinstance(e, Guarded)]  # no stale repeats, no nudges
    ran = [e.result for e in events if isinstance(e, ToolRan)]
    assert [r.summary for r in ran][3] == "Search results: 1 elements"
    assert EMAIL in json.dumps(ran[-1].data)
    # Step 2 starts from step 1's note, not from step 1's page reads.
    step_two = core.requests[5].messages
    assert not [m for m in step_two if m.role == "tool"]
    assert "One unread" in step_two[1].text
    # Every action in Chrome first took control of the screen (the glow).
    assert presence.acted == len(ran)


class Presence:
    held = False

    def __init__(self) -> None:
        self.acted = 0

    async def acting(self) -> None:
        self.acted += 1

    async def release(self) -> None:
        return None
