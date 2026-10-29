import pytest
from pydantic import BaseModel, ValidationError

from arcee.core.protocol import CoreAction, PlannerOutput
from arcee.models.jsonfix import (
    ReplyError,
    extract_object,
    format_validation_error,
    parse_reply,
    retry_prompt,
)

VALID = '{"action": {"tool": "fs.list", "args": {"path": "~"}}}'


class TestExtractObject:
    @pytest.mark.parametrize(
        "raw",
        [
            VALID,
            f"```json\n{VALID}\n```",
            f"Sure! Here is the action:\n{VALID}\nLet me know if you need more.",
            f"  \n{VALID}\n\n",
        ],
    )
    def test_finds_object_inside_surrounding_text(self, raw: str) -> None:
        assert extract_object(raw) == VALID

    def test_ignores_braces_inside_strings(self) -> None:
        raw = '{"thought": "use {curly} and } braces", "action": {"tool": "fs.list"}} trailing }'

        assert extract_object(raw) == raw.removesuffix(" trailing }")

    def test_handles_escaped_quotes_and_backslashes(self) -> None:
        obj = r'{"text": "a \"quoted\" } and a \\", "n": {"x": 1}}'

        assert extract_object(f"prefix {obj} suffix") == obj

    def test_returns_tail_of_truncated_object(self) -> None:
        assert extract_object('Reply: {"action": {"tool": "fs.li') == '{"action": {"tool": "fs.li'

    def test_returns_none_without_an_object(self) -> None:
        assert extract_object("I cannot help with that.") is None


class TestParseReply:
    def test_validates_clean_reply(self) -> None:
        action = parse_reply(VALID, CoreAction)

        assert action.action.args == {"path": "~"}

    @pytest.mark.parametrize(
        "raw",
        [
            '{"action": {"tool": "fs.list", "args": {"path": "~",},},}',
            "{'action': {'tool': 'fs.list', 'args': {'path': '~'}}}",
            '```\n{"action": {"tool": "fs.list", "args": {"path": "~"}}\n```',
        ],
        ids=["trailing-commas", "single-quotes", "unclosed-object"],
    )
    def test_repairs_common_json_mistakes(self, raw: str) -> None:
        assert parse_reply(raw, CoreAction).action.tool == "fs.list"

    def test_rejects_reply_without_json(self) -> None:
        with pytest.raises(ReplyError, match="No JSON object found"):
            parse_reply("Done!", CoreAction)

    def test_rejects_unrepairable_reply(self) -> None:
        with pytest.raises(ReplyError, match="not a valid JSON object"):
            parse_reply("{{{{", CoreAction)

    def test_reports_schema_errors_with_paths(self) -> None:
        with pytest.raises(ReplyError) as caught:
            parse_reply('{"action": {"tool": "FS"}, "confidence": 3}', CoreAction)

        message = str(caught.value)
        assert "action.tool: String should match pattern" in message
        assert "(got 'FS')" in message
        assert "confidence: Input should be less than or equal to 1" in message

    def test_reports_missing_fields_without_echoing_the_parent(self) -> None:
        with pytest.raises(ReplyError) as caught:
            parse_reply('{"thought": "hm"}', CoreAction)

        assert str(caught.value) == "action: Field required"


class _ManyFields(BaseModel):
    a: int
    b: int
    c: int


def test_format_validation_error_caps_reported_errors() -> None:
    with pytest.raises(ValidationError) as caught:
        _ManyFields.model_validate({"a": "x", "b": "y", "c": "z"})

    assert format_validation_error(caught.value, limit=2).endswith("; and 1 more")


def test_retry_prompt_matches_spec_wording() -> None:
    assert retry_prompt(ReplyError("action: Field required")) == (
        "VALIDATION ERROR: action: Field required. Return a corrected JSON object."
    )


def test_a_reply_echoing_the_schema_shape_is_unwrapped() -> None:
    echoed = (
        '{"description": "The Planner\'s task", "properties": {"goal": "g", '
        '"interpretation": "", "steps": [{"id": 1, "do": "d", "success": "s"}]}}'
    )

    plan = parse_reply(echoed, PlannerOutput)

    assert plan.goal == "g"
