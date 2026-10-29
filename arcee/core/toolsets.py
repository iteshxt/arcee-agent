"""Which tool namespaces a request needs (spec section 6, step 2: offer only what fits).

Every tool offered costs prompt space and a chance to pick the wrong one, and
the browser and web tools add ten. They are offered only when the request is
about the web, or once the task has used them; the rest are always offered.
"""

import re
from collections.abc import Sequence
from typing import Final

from arcee.models.base import Message

ALWAYS: Final = ("fs.*", "app.*", "system.*", "network.*", "shell.*", "control.*")
WEB: Final = ("web.*", "browser.*")
_WEB_REQUEST: Final = re.compile(
    r"\b(browser|chrome|tabs?|web|websites?|sites?|pages?|links?|urls?|online|internet|"
    r"google|search the|look up|news|gmail|e-?mails?|mail|inbox|youtube|wikipedia|"
    r"download from|log ?in|sign ?in)\b|https?://|www\.|\.(com|org|net|io|in)\b",
    re.IGNORECASE,
)


def filters_for(messages: Sequence[Message]) -> tuple[str, ...]:
    """Tool filters for a task: web tools when the request or earlier calls involve the web."""
    request = messages[0].text if messages else ""
    used_web = any(
        call.name.startswith(("web_", "browser_")) for m in messages for call in m.tool_calls
    )
    return ALWAYS + WEB if used_web or _WEB_REQUEST.search(request) else ALWAYS
