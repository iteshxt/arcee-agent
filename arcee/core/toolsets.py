"""Which tools a request is offered (spec section 6, step 2: offer only what fits).

Every tool definition is sent with every request: prompt tokens (the binding
limit on free cloud tiers) and one more way to pick wrong. Groups are chosen
in code from the request before the model sees anything:

- a request only about the web (mail, a site, open tabs) gets the browser or web
  tools and the control tools, nothing local;
- a request only about this computer gets the local tools;
- a request that mixes both gets both, and a group the task has used stays.
"""

import re
from collections.abc import Sequence
from typing import Final

from arcee.models.base import Message

CONTROL: Final = ("control.*",)
LOCAL: Final = ("fs.*", "app.*", "system.*", "network.*", "shell.*")
ALWAYS: Final = LOCAL + CONTROL
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
# Words that bring this computer into a web request ("save the email to a file").
_LOCAL_REQUEST: Final = re.compile(
    r"\b(files?|folders?|director(y|ies)|disk|drive|downloads?|documents|desktop|"
    r"save|write|trash|ram|memory|cpu|ip address|wifi|apps?|application|command|"
    r"terminal|script|install|~/|home folder)\b|~/",
    re.IGNORECASE,
)


def filters_for(messages: Sequence[Message]) -> tuple[str, ...]:
    """Tool filters for a task, decided from the request and the tools used so far."""
    request = messages[0].text if messages else ""
    called = {call.name.split("_", 1)[0] for m in messages for call in m.tool_calls}
    browser = "browser" in called or bool(_BROWSER_REQUEST.search(request))
    web = "web" in called or bool(_WEB_REQUEST.search(request))
    local = (
        not (browser or web)
        or bool(_LOCAL_REQUEST.search(request))
        or bool(called & {"fs", "app", "system", "network", "shell"})
    )
    filters: tuple[str, ...] = (LOCAL if local else ()) + CONTROL
    if browser:
        filters += BROWSER
    if web:
        filters += WEB
    return filters
