"""Which tools a request is offered (spec section 6, step 2: offer only what fits).

Every tool definition is sent with every request, and on Groq's free tier input
tokens are the binding limit: tool definitions were 89% of a simple request.
So code picks groups from the request's words before the model sees anything:
system facts, files, apps, shell, browser, web. Control tools always come. A
request matching no group gets every local tool, so nothing is lost by a missed
word; and a group the task has already used stays offered.
"""

import re
from collections.abc import Sequence
from typing import Final

from arcee.models.base import Message

# The control tools every task needs. wait, note and speak cost tokens on every call
# and tempted the model ("Hi" got control.speak instead of a reply), so they come
# only when the request calls for them.
CONTROL: Final = ("control.done", "control.ask_user", "control.replan")
NOTE: Final = ("control.note",)
WAIT: Final = ("control.wait",)
SYSTEM: Final = ("system.*", "network.*")
FILES: Final = ("fs.*",)
APPS: Final = ("app.*",)
SHELL: Final = ("shell.*",)
LOCAL: Final = FILES + APPS + SYSTEM + SHELL
ALWAYS: Final = LOCAL + CONTROL
BROWSER: Final = ("browser.*",)
WEB: Final = ("web.*",)


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_GROUPS: Final = (
    (
        SYSTEM,
        _words(
            r"\b(ram|memory|cpu|processor|disk space|free space|storage|space left|uptime|"
            r"ubuntu|os|operating system|version|kernel|release|battery|hardware|"
            r"ip|ip address|wifi|wi-fi|network|online|offline|connected|connection|"
            r"internet connection)\b"
        ),
    ),
    (
        FILES,
        _words(
            r"\b(files?|folders?|director(y|ies)|paths?|documents|downloads|desktop|pictures|"
            r"photos?|music|videos?|pdfs?|notes?|readme|rename|move|copy|delete|remove|trash|"
            r"save|write|read|list|find|locate|where|biggest|largest|newest|latest|oldest|"
            r"backup|clean|zip|installers?|todo|resume|cv)\b|~/|\.\w{2,4}\b"
        ),
    ),
    (
        APPS,
        _words(r"\b(apps?|applications?|launch|start|installed|programs?)\b"),
    ),
    (
        SHELL,
        _words(
            r"\b(commands?|terminal|shell|scripts?|run|running|process(es)?|calculate|compute|"
            r"power of|exactly|arithmetic|math|ps|grep|du|df|uname)\b"
        ),
    ),
)
_BROWSER_REQUEST: Final = _words(
    r"\b(browser|chrome|tabs?|gmail|e-?mails?|mail|inbox|youtube|my account|"
    r"log ?in|sign ?in|click|this page|the page|open page|site i)\b"
)
_WEB_REQUEST: Final = _words(
    r"\b(web|websites?|internet|google|search (the|for|online)|look up|news|"
    r"wikipedia|links?|urls?|articles?)\b|https?://|www\.|\.(com|org|net|io)\b"
)
# Greetings, thanks and questions about Arcee itself need no tools at all.
_CHAT: Final = _words(
    r"^\W*(hi|hello|hey|thanks|thank you|thx|ok|okay|cool|great|bye|good (morning|night|"
    r"evening)|what can you do|who are you|how are you)\b[^?]{0,40}[.!?]*\s*$"
)
_REMEMBER: Final = _words(r"\b(remember|note (that|down)|keep in mind|don't forget)\b")
# "Open X" may mean a file or an app: it brings in both.
_OPEN: Final = _words(r"\bopen\b")
# In a request about the web, only words that clearly involve this computer add local
# tools ("save it to a file"); "Ubuntu" in a web search is not about this machine.
_CLEARLY_LOCAL: Final = _words(
    r"\b(save|write|files?|folders?|documents|downloads|desktop|my computer|this computer)\b|~/"
)
_ADDRESS: Final = _words(r"https?://\S+|\bwww\.\S+|\b[\w-]+\.(com|org|net|io|in|dev|ai)\b\S*")
_NAMESPACE_GROUP: Final = {
    "fs": FILES,
    "app": APPS,
    "system": SYSTEM,
    "network": SYSTEM,
    "shell": SHELL,
    "browser": BROWSER,
    "web": WEB,
}


def filters_for(messages: Sequence[Message]) -> tuple[str, ...]:
    """Tool filters for a task, decided from the request and the tools used so far."""
    request = messages[0].text if messages else ""
    used = {
        _NAMESPACE_GROUP[name]
        for m in messages
        for call in m.tool_calls
        if (name := call.name.split("_", 1)[0]) in _NAMESPACE_GROUP
    }
    browser = BROWSER in used or bool(_BROWSER_REQUEST.search(request))
    web = WEB in used or bool(_WEB_REQUEST.search(request))
    words = _ADDRESS.sub(" ", request)  # example.com is not a file extension
    local = [g for g, pattern in _GROUPS if g in used or pattern.search(words)]
    if _OPEN.search(words):
        local += [g for g in (FILES, APPS) if g not in local]
    if (browser or web) and not _CLEARLY_LOCAL.search(words):
        local = [g for g in local if g in used]
    if not local and not (browser or web) and not used:
        if _CHAT.search(request):
            return CONTROL
        local = [FILES, APPS, SYSTEM, SHELL]  # nothing recognised: offer everything local
    filters: tuple[str, ...] = tuple(f for group in local for f in group) + CONTROL
    if _REMEMBER.search(request) or "control_note" in _called(messages):
        filters += NOTE
    if browser or APPS in local:
        filters += WAIT  # pages and apps take a moment to load
    if browser:
        filters += BROWSER
    if web:
        filters += WEB
    return filters


def _called(messages: Sequence[Message]) -> set[str]:
    return {call.name for m in messages for call in m.tool_calls}
