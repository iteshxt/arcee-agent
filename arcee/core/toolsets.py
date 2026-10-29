"""Which tool namespaces a request needs (spec section 6, step 2: offer only what fits).

Every tool offered costs prompt space and a chance to pick the wrong one. The
browser tools act in the user's own, signed-in Chrome; the web tools fetch and
search anonymously. Offered together, a 1.7B model fetched Gmail anonymously
instead of reading the open, signed-in tab. So each group comes only with
requests about it, or once the task has used it.
"""

import re
from collections.abc import Sequence
from typing import Final

from arcee.models.base import Message

ALWAYS: Final = ("fs.*", "app.*", "system.*", "network.*", "shell.*", "control.*")
BROWSER: Final = ("browser.*",)
WEB: Final = ("web.*",)
_BROWSER_REQUEST: Final = re.compile(
    r"\b(browser|chrome|tabs?|gmail|e-?mails?|mail|inbox|youtube|my account|"
    r"log ?in|sign ?in|click|this page|the page|open page|site i)\b",
    re.IGNORECASE,
)
_WEB_REQUEST: Final = re.compile(
    r"\b(web|websites?|online|internet|google|search (the|for|online)|look up|news|"
    r"wikipedia|links?|urls?|articles?)\b|https?://|www\.|\.(com|org|net|io|in)\b",
    re.IGNORECASE,
)


def filters_for(messages: Sequence[Message]) -> tuple[str, ...]:
    """Tool filters for a task: each optional group when the request or earlier calls need it."""
    request = messages[0].text if messages else ""
    called = {call.name.split("_", 1)[0] for m in messages for call in m.tool_calls}
    filters = ALWAYS
    if "browser" in called or _BROWSER_REQUEST.search(request):
        filters += BROWSER
    if "web" in called or _WEB_REQUEST.search(request):
        filters += WEB
    return filters
