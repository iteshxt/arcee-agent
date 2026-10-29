import json

from arcee.core.prompts import tool_message
from arcee.core.protocol import ExecutorResult


def result(**fields: object) -> ExecutorResult:
    return ExecutorResult.model_validate(
        {"n": 1, "tool": "fs.list", "status": "ok", "summary": "s", "duration_ms": 1, **fields}
    )


def test_small_results_pass_through() -> None:
    message = json.loads(tool_message(result(data={"count": 2, "entries": ["a", "b"]})))

    assert message == {"status": "ok", "data": {"count": 2, "entries": ["a", "b"]}}


def test_big_lists_are_shortened_with_a_note() -> None:
    entries = [{"name": f"file-{i:03}.txt", "size": "1.0 KB"} for i in range(200)]

    text = tool_message(result(data={"count": 200, "entries": entries}), limit=2000)

    data = json.loads(text)["data"]
    assert len(text) <= 2000
    assert data["count"] == 200
    assert data["entries"][0]["name"] == "file-000.txt"
    assert data["entries_total"] == 200
    assert data["note"] == f"entries: showing {len(data['entries'])} of 200"


def test_long_text_is_clipped() -> None:
    text = tool_message(result(output="x" * 10_000), limit=500)

    assert len(json.loads(text)["output"]) < 600
    assert text.endswith('more chars]"}')


def test_refusals_say_not_to_retry() -> None:
    message = json.loads(tool_message(result(status="refused", error="the user declined")))

    assert message["error"] == "the user declined"
    assert "Do not retry" in message["note"]
