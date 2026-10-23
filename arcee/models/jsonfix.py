"""Turn a model's raw reply into a validated protocol object (spec section 5).

Models wrap JSON in code fences, add a sentence before or after it, leave trailing
commas or use single quotes. `parse_reply` tolerates all of that; anything it
cannot fix becomes a `ReplyError` whose message is written for the model, so the
adapter can send it back verbatim in the retry turn.
"""

import json
import re
from typing import Final, cast

import json_repair
from pydantic import BaseModel, ValidationError

MAX_REPORTED_ERRORS: Final = 5

# Characters that change string or nesting state while scanning for the object.
_STRUCTURAL: Final = re.compile(r'[{}"\\]')


class ReplyError(ValueError):
    """A reply that could not be parsed or validated. `str(error)` is feedback for the model."""


def extract_object(text: str) -> str | None:
    """Return the first top-level `{...}` in `text`, or None if there is no `{`.

    Braces inside JSON strings are ignored. If the object never closes (a truncated
    reply), everything from the opening brace is returned so repair can try.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped_pos = -1
    for match in _STRUCTURAL.finditer(text, start):
        pos = match.start()
        if pos == escaped_pos:
            continue
        match match.group():
            case "\\":
                if in_string:
                    escaped_pos = pos + 1
            case '"':
                in_string = not in_string
            case "{" if not in_string:
                depth += 1
            case "}" if not in_string:
                depth -= 1
                if depth == 0:
                    return text[start : pos + 1]
            case _:
                pass
    return text[start:]


def _load_object(candidate: str) -> dict[str, object]:
    try:
        data: object = json.loads(candidate)
    except json.JSONDecodeError:
        # json_repair is slow and occasionally creative, so it only sees broken input.
        data = json_repair.loads(candidate)
    if not isinstance(data, dict):
        raise ReplyError("The reply is not a valid JSON object.")
    return cast(dict[str, object], data)  # JSON object keys are always strings


def format_validation_error(exc: ValidationError, limit: int = MAX_REPORTED_ERRORS) -> str:
    """Render Pydantic errors as `path: message` pairs a model can act on."""
    parts: list[str] = []
    for error in exc.errors(include_url=False)[:limit]:
        path = ".".join(str(part) for part in error["loc"]) or "(root)"
        part = f"{path}: {error['msg']}"
        value = error.get("input")
        if error["type"] != "missing" and isinstance(value, str | int | float | bool):
            part += f" (got {value!r:.60})"
        parts.append(part)
    if (hidden := exc.error_count() - limit) > 0:
        parts.append(f"and {hidden} more")
    return "; ".join(parts)


def parse_reply[M: BaseModel](raw: str, schema: type[M]) -> M:
    """Extract, load and validate one `schema` object from a model reply."""
    candidate = extract_object(raw)
    if candidate is None:
        raise ReplyError("No JSON object found. Respond with exactly one JSON object.")
    data = _load_object(candidate)
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise ReplyError(format_validation_error(exc)) from exc


def retry_prompt(error: ReplyError) -> str:
    """The user turn appended after a failed reply (spec section 5, step 4)."""
    return f"VALIDATION ERROR: {error}. Return a corrected JSON object."
