"""Secret redaction for logs and the store (spec section 12, "Secrets").

Anything that looks like a known API key or token format, or a value assigned to
a password-like name, is replaced before it is written anywhere.
"""

import re
from typing import Any, Final, cast

REDACTED: Final = "[redacted]"

_PATTERNS: Final = (
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google
    re.compile(r"sk-(?:proj-|ant-)?[0-9A-Za-z_\-]{20,}"),  # OpenAI, Anthropic
    re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"),  # GitHub
    re.compile(r"nvapi-[0-9A-Za-z_\-]{30,}"),  # NVIDIA
    re.compile(r"gsk_[0-9A-Za-z]{30,}"),  # Groq
    re.compile(r"xox[abposr]-[0-9A-Za-z\-]{10,}"),  # Slack
    re.compile(r"(?i)\bbearer\s+[0-9A-Za-z._\-]{20,}"),
)
_ASSIGNED: Final = re.compile(
    r"(?i)\b([\w.-]*(?:password|passwd|secret|token|api[_-]?key)[\w.-]*)(\s*[=:]\s*)(\S+)"
)


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _ASSIGNED.sub(lambda m: f"{m[1]}{m[2]}{REDACTED}", text)


def redact_value(value: Any) -> Any:
    """Redact every string inside a JSON-like value."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in cast(dict[str, Any], value).items()}
    if isinstance(value, list | tuple):
        return [redact_value(item) for item in cast(list[Any], value)]
    return value
