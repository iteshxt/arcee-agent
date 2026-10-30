import json

from arcee.core.context import Handoff, core_view, shorten_old_results
from arcee.models.base import Message, WireCall


def result(n: int, size: int = 1000) -> list[Message]:
    call = WireCall("browser_get_elements", {"limit": n})
    body = json.dumps({"status": "ok", "data": {"rows": "x" * size}})
    return [
        Message("assistant", "", tool_calls=(call,)),
        Message("tool", body, tool_name=call.name, summary=f"page {n}: 100 elements"),
    ]


class TestShortenOldResults:
    def test_keeps_the_latest_results_whole(self) -> None:
        messages = [Message("user", "go"), *result(1), *result(2), *result(3)]

        out = shorten_old_results(messages, keep=2)

        tools = [m for m in out if m.role == "tool"]
        assert json.loads(tools[0].text) == {
            "status": "ok",
            "summary": "page 1: 100 elements",
            "note": "Shortened to save space; later results show the current state.",
        }
        assert tools[1:] == [m for m in messages if m.role == "tool"][1:]
        # Calls stay paired with their results.
        assert [m.role for m in out] == [m.role for m in messages]

    def test_short_results_stay_as_they_are(self) -> None:
        messages = [*result(1, size=10), *result(2), *result(3)]

        assert shorten_old_results(messages, keep=1)[1] == messages[1]


class TestCoreView:
    def test_a_step_sees_the_request_the_notes_and_its_own_turns(self) -> None:
        messages = [
            Message("user", "check my gmail"),
            *result(1),
            Message("assistant", "3 unread: A, B, C."),
            Message("user", "Now step 2: Open Calendar"),
            *result(2),
        ]

        view = core_view(
            messages,
            prelude=1,
            start=4,
            handoffs=[Handoff(1, "Read the inbox", "3 unread: A, B, C.")],
            asked={"which account?": "personal"},
        )

        assert [m.text for m in view[:3]] == [
            "check my gmail",
            "Done so far (earlier tool results are not shown again):\n"
            "Step 1: Read the inbox\nResult: 3 unread: A, B, C.\n"
            "You asked the user: which account?\nThey answered: personal",
            "Now step 2: Open Calendar",
        ]
        assert view[3:] == messages[5:]

    def test_without_a_plan_it_is_the_whole_conversation(self) -> None:
        messages = [Message("user", "hi"), *result(1)]

        assert core_view(messages, prelude=1, start=1) == messages
