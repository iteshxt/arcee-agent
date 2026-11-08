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
# Looking up and forgetting what Arcee remembers (recall already brings what matters).
MEMORY: Final = ("memory.*", "control.note")
WAIT: Final = ("control.wait", "control.wait_for")
SYSTEM: Final = ("system.*", "network.*")
FILES: Final = ("fs.*",)
APPS: Final = ("app.*", "terminal.*")
SHELL: Final = ("shell.*",)
LOCAL: Final = FILES + APPS + SYSTEM + SHELL
ALWAYS: Final = LOCAL + CONTROL
BROWSER: Final = ("browser.*",)
# Desktop apps: their window tree first, the screen (OCR, clicks, keys) as fallback.
DESKTOP: Final = ("ui.*", "screen.*", "kbd.*", "window.*")
WEB: Final = ("web.*", "convert.*", "market.*", "maps.*")
MEDIA: Final = ("media.*",)
CALENDAR: Final = ("calendar.*",)
MAIL: Final = ("mail.*", "share.*", "contacts.*")
REMINDERS: Final = ("reminder.*", "routine.*", "time.*")
VOICE: Final = ("voice.*",)
# The desktop's settings: sound, screen, appearance, radios, power, default apps, updates.
SETTINGS: Final = ("settings.*", "bluetooth.*", "session.*", "updates.*")
WINDOWS: Final = ("window.*", "notifications.*")
# The clipboard, screenshots kept for the user, recordings.
CAPTURE: Final = ("clipboard.*", "screen.save", "record.*")
# Documents made, photos edited, printing.
DOCS: Final = ("doc.*", "image.*", "print.*", "fs.read")


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
            r"backup|clean|zip|installers?|todo|resume|cv|tak(e|es|ing) up|filling|"
            r"most space)\b|~/|\.\w{2,4}\b"
        ),
    ),
    (
        APPS,
        _words(
            r"\b(apps?|applications?|launch|start|installed|install|programs?|close|quit|"
            r"terminal|dev server|run .* where i can (see|watch))\b"
        ),
    ),
    (
        DESKTOP,
        _words(
            r"\b(windows?|clicks?|clicking|buttons?|menus?|dialogs?|gui|toolbars?|sidebar|"
            r"screen|keyboard|shortcuts?|press(es|ing)?|typ(e|ing) (it )?in(to)?|"
            r"(the|its|that|this) app|in (the )?(files|nautilus|settings|text editor|"
            r"calculator|calendar)( app)?|files app|settings app|calendar app|text editor|"
            r"calculator|nautilus|minimi[sz](e|ed|ing)|maximi[sz](e|ed|ing)|full ?screen|close|"
            r"quit)\b"
        ),
    ),
    (
        SHELL,
        _words(
            r"\b(commands?|terminal|shell|scripts?|run|running|process(es)?|calculate|compute|"
            r"power of|exactly|arithmetic|math|ps|grep|du|df|uname)\b"
        ),
    ),
)
# Mail is the mail tools' (Gmail over IMAP, decision 0017), not the browser's: only
# "in Chrome", a site or the page itself bring the browser.
_BROWSER_REQUEST: Final = _words(
    r"\b(browser|chrome|tabs?|youtube|my account|log ?in|sign ?in|click|this page|the page|"
    r"bookmarks?|reading list|"
    r"open page|site i)\b"
)
_WEB_REQUEST: Final = _words(
    r"\b(web|websites?|internet|google|search (the|for|online)|look up|news|weather|"
    r"dollars?|rupees?|euros?|pounds?|currency|exchange rate|convert \d|in (inr|usd|eur)|"
    r"stocks?|shares?|share price|price of|bitcoin|crypto|ethereum|sensex|nifty|"
    r"how (far|long) (is|to|does)|drive to|distance (from|to|between)|directions|"
    r"forecast|rain(ing|y)?|temperature|humid(ity)?|snow(ing)?|umbrella|"
    r"wikipedia|links?|urls?|articles?)\b|https?://|www\.|\.(com|org|net|io)\b"
)
# A greeting before a request ("Hey, how hot is it outside?", common when spoken) does
# not make it small talk: it is set aside before the request is read.
_GREETING_FIRST: Final = _words(
    r"^\W*(hi|hello|hey|ok|okay)\b(\W+(arcee|rc)\b)?(?!\W+(arcee|rc)\b)[\s,!.]+(?=\S+\s+\S)"
)
# Greetings, thanks and questions about Arcee itself need no tools at all.
_CHAT: Final = _words(
    r"^\W*(hi|hello|hey|thanks|thank you|thx|ok|okay|cool|great|bye|good (morning|night|"
    r"evening)|what can you do|who are you|how are you)\b[^?]{0,40}[.!?]*\s*$"
)
# A question about the world ("who is Jason Bourne", "how do tides work") that names
# nothing on this computer: answered from knowledge or a web search, never by
# improvising with files and shell commands (it once ran five curl scripts).
_QUESTION: Final = _words(
    r"^\W*(who|what|whats|what's|when|where|why|how|which|whose|is|are|was|were|does|did|"
    r"can|could|will|would|should)\b|"
    r"\b(tell me (about|who|what|why|how)|explain|define|meaning of)\b"
)
# Music and video already playing: pause, skip, what is this. Offered beside whatever
# else the request needs (pausing YouTube is not a browser task).
_MEDIA_REQUEST: Final = _words(
    r"\b(pause|unpause|resume|play(ing)?|next (song|track|video)|previous (song|track)|"
    r"skip|song|track|music|playing|spotify|stop the (music|song|video)|who sings)\b"
)
# The user's calendar: offered beside whatever else the request needs ("check my calendar
# and email Rahul"), never dropped for a browser request ("in Google Calendar").
_CALENDAR_REQUEST: Final = _words(
    r"\b(calendar|schedule|agenda|meetings?|appointments?|events?|busy|free (time|on|at|"
    r"this|tomorrow|today)|am i free|reschedule|book (a|an|time)|remind me on|holidays?)\b"
)
_MAIL_REQUEST: Final = _words(
    r"\b(e-?mails?|mails?|gmail|inbox|unread|drafts?|reply|forward|newsletters?|"
    r"unsubscribe|to my phone|onto my phone|on my phone|to myself|contacts?|"
    r"(his|her|their|\w+'s) (email|e-mail|address)|email address of|"
    r"attachments?|sent me|wrote to me|message from)\b"
)
# Timers and reminders (decision 0018): "set a timer", "remind me at 5", "wake me at 7".
_REMINDER_REQUEST: Final = _words(
    r"\b(timers?|remind(er|ers)?|alarms?|wake me|ping me|nudge me|countdown|stopwatch|"
    r"time (is it )?in|what time|time zone|timezone|o'?clock in|routines?|"
    r"every (day|morning|evening|night|week|weekday|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|each (day|morning|evening|week)|focus (session|mode)|"
    r"how long since|lap\b|pomodoro|"
    r"in \d+ ?(s|sec|secs|seconds?|m|min|mins|minutes?|h|hrs?|hours?)\b)"
)
# Asking to hear something (decision 0019): "read it out loud", "say that aloud".
_VOICE_REQUEST: Final = _words(
    r"\b(out ?loud|aloud|read (it |this |that |them )?(to me|out)|read me\b|"
    r"(say|speak|tell me) (it|this|that)\b.*\b(voice|loud)|by voice|in your voice|"
    r"speak (it|this|that|to me)|let me hear)"
)
_SETTINGS_REQUEST: Final = _words(
    r"\b(volume|louder|quieter|softer|mute|unmute|sound (output|device|settings)|speakers?|"
    r"headphones|headset|earbuds|buds|hdmi|audio (output|input|device)|microphone|mic|"
    r"brightness|brighter|dimmer|dim (the )?screen|dark mode|light mode|dark theme|"
    r"night ?light|wallpaper|background image|do not disturb|dnd|silence notifications|"
    r"notifications (off|on)|focus (mode|session)|wi-?fi|bluetooth|airplane|connect my|"
    r"text (size|bigger|smaller)|(bigger|larger|smaller) text|magnif\w*|zoom (in|out) on the "
    r"screen|high contrast|lock (my|the) (computer|screen|pc|laptop)|suspend|sleep|"
    r"shut ?down|power off|restart (my |the )?(computer|pc|laptop|system)|reboot|log ?out|"
    r"sign out|default (browser|app|application|mail|email|pdf|video|player)|"
    r"open (pdfs|videos|images|photos) with|updates?|upgrade)\b"
)
_CAPTURE_REQUEST: Final = _words(
    r"\b(clipboard|copy (it|this|that|my|the|them)|copied|paste|(did i|i just) copy|screen ?shots?|"
    r"record(ing)? (my |the )?(screen|audio|voice|mic|microphone|meeting|call|lecture)|"
    r"voice memo|stop recording|screencast|screen recording|still recording)\b"
)
_DOCS_REQUEST: Final = _words(
    r"\b(word (document|doc|file)|\.?docx|odt|libreoffice|writer|as a pdf|to pdf|make a pdf|"
    r"create a (document|doc|letter|report|resume|cv)|resize|convert|heic|jpe?g|png|webp|"
    r"photos? to|compress (the |this |these )?(photos?|images?)|print(er|ing)?|"
    r"receipts?|scan(ned)?)\b"
)
# Undoing what an earlier request did.
_UNDO: Final = _words(
    r"\b(undo|put (them|it|the files?) back|revert|reverse that|restore (it|them))\b"
)
_REMEMBER: Final = _words(r"\b(remember|note (that|down)|keep in mind|don't forget)\b")
# Asking about memory, not adding to it ("remember that ..." is a note).
_MEMORY: Final = _words(
    r"\b(do you remember|what do you remember|you remembered|recall|forget|memor(y|ies)|"
    r"do you know|what do you know|know about me|"
    r"what (did|have) (i|we) (do|did|done|ask|asked|work(ed)? on|been doing)|"
    r"(did|have) i ask(ed)? you|i asked you|what (was|were) (i|we) (doing|working on)|"
    r"(my|our) (history|activity) with you)\b"
)
# "Open X" may mean a file or an app: it brings in both.
_OPEN: Final = _words(r"\bopen\b")
# Only opening something ("Open Chrome.", "launch the calculator please"): one clause,
# nothing to do once it is open. Opening the app is the whole task, so the app tools
# stay even when it names the browser (they had been dropped for browser requests, and
# "Open Chrome." made up a page to navigate to: chromium.org).
_JUST_OPEN: Final = _words(
    r"^\W*(please\s+|can you\s+|could you\s+)?(open|launch|start|bring up|switch to)\s+"
    r"(up\s+)?(the\s+|my\s+)?(?!.*\b(and|then|to|with|for|in|on)\b)[\w .+'-]{1,40}?"
    r"(\s+(app|application))?(\s+(please|for me|now))?\W*$"
)
# What a bare "open the browser" names: it needs no browser tools, only the app.
_BROWSER_APP: Final = _words(r"\b(chrome|chromium|browser|google chrome)\b")
# Arranging windows across halves, screens and workspaces; the notifications missed.
_WINDOWS_REQUEST: Final = _words(
    r"\b(side by side|on the (left|right)|(left|right) (half|side)|split screen|tile|snap|"
    r"arrange|(second|other|external|another|main) (screen|monitor|display)|monitors?|"
    r"workspaces?|quarter of the screen|"
    r"notifications? (i |did i |have i )?(missed|miss|waiting|unread)|missed notifications|"
    r"what notifications|(did|has) (anyone|someone) (message|text|call|ping)(ed)? me)\b"
)
# Done to the browser's window ("make the Chrome window maximized"), nothing in a page:
# the window tools, not the browser's (a small model clicked in the page instead).
_WINDOW_ONLY: Final = _words(
    r"\b(minimi[sz](e|ed|ing)|maximi[sz](e|ed|ing)|full ?screen|resize|snap|move (it|the window))\b"
)
_IN_PAGE: Final = _words(
    r"\b(tabs?|pages?|sites?|website|links?|urls?|search|youtube|gmail|scroll|type|fill|"
    r"sign ?in|log ?in)\b|https?://|www\."
)
# In a request about the web, only words that clearly involve this computer add local
# tools ("save it to a file"); "Ubuntu" in a web search is not about this machine.
_CLEARLY_LOCAL: Final = _words(
    r"\b(save|write|files?|folders?|documents|downloads|desktop|my computer|this computer|"
    r"terminal|dev server)\b|~/"
    # Window verbs ("close chrome", "minimise it"); closing a tab stays in the browser.
    # "Make the Chrome window maximized": its window, not a page in it.
    r"|\b(minimi[sz](e|ed|ing)|maximi[sz](e|ed|ing)|full ?screen|quit|windows?)\b"
    r"|\bclose\b(?![^.,;]{0,30}\btabs?\b)"
)
_ADDRESS: Final = _words(r"https?://\S+|\bwww\.\S+|\b[\w-]+\.(com|org|net|io|in|dev|ai)\b\S*")
_NAMESPACE_GROUP: Final = {
    "fs": FILES,
    "app": APPS,
    "system": SYSTEM,
    "network": SYSTEM,
    "shell": SHELL,
    "ui": DESKTOP,
    "screen": DESKTOP,
    "kbd": DESKTOP,
    "window": DESKTOP,
    "browser": BROWSER,
    "web": WEB,
}


def filters_for(messages: Sequence[Message], *, earlier: Sequence[str] = ()) -> tuple[str, ...]:
    """Tool filters for a task, decided from the request and the tools used so far, and
    from the conversation's `earlier` requests ("and tomorrow?" after the weather)."""
    filters = _filters(messages)
    for request in earlier[-2:]:
        filters += tuple(f for f in _filters([Message("user", request)]) if f not in filters)
    return filters


def _filters(messages: Sequence[Message]) -> tuple[str, ...]:
    request = _GREETING_FIRST.sub("", messages[0].text if messages else "", count=1)
    used = {
        _NAMESPACE_GROUP[name]
        for m in messages
        for call in m.tool_calls
        if (name := call.name.split("_", 1)[0]) in _NAMESPACE_GROUP
    }
    namespaces = {c.split("_", 1)[0] for c in _called(messages)}
    # The user's own services (mail, calendar, music): a request about them is recognised,
    # and needs no local tools unless it names files ("save the email to Documents").
    services = [
        group
        for group, pattern, namespace in (
            (MEDIA, _MEDIA_REQUEST, "media"),
            (CALENDAR, _CALENDAR_REQUEST, "calendar"),
            (MAIL, _MAIL_REQUEST, "mail"),
            (REMINDERS, _REMINDER_REQUEST, "reminder"),
            (VOICE, _VOICE_REQUEST, "voice"),
            (SETTINGS, _SETTINGS_REQUEST, "settings"),
            (WINDOWS, _WINDOWS_REQUEST, "window"),
            (CAPTURE, _CAPTURE_REQUEST, "clipboard"),
            (DOCS, _DOCS_REQUEST, "doc"),
        )
        if pattern.search(request) or namespace in namespaces
    ]
    browser = BROWSER in used or bool(_BROWSER_REQUEST.search(request))
    web = WEB in used or bool(_WEB_REQUEST.search(request))
    words = _ADDRESS.sub(" ", request)  # example.com is not a file extension
    local = [g for g, pattern in _GROUPS if g in used or pattern.search(words)]
    if _OPEN.search(words):
        local += [g for g in (FILES, APPS) if g not in local]
    just_open = bool(_JUST_OPEN.search(words)) and not _ADDRESS.search(request)
    if just_open and _BROWSER_APP.search(words):
        browser = False  # "Open Chrome": the app, and nothing to do in a page
    window_only = bool(_WINDOW_ONLY.search(words)) and not _IN_PAGE.search(request)
    if window_only and _BROWSER_APP.search(words) and BROWSER not in used:
        browser = False
    if (browser or web or services) and not _CLEARLY_LOCAL.search(words):
        local = [g for g in local if g in used or (just_open and g == APPS)]
    if not local and not (browser or web or services) and not used:
        if _CHAT.search(request):
            return CONTROL
        if _QUESTION.search(request):
            web = True  # a question about the world: knowledge, then a web search
        else:
            local = [FILES, APPS, SYSTEM, SHELL]  # nothing recognised: everything local
    filters: tuple[str, ...] = tuple(f for group in local for f in group) + CONTROL
    if _UNDO.search(request) or any(c.startswith("task_") for c in _called(messages)):
        filters += ("task.*",)
    if _MEMORY.search(request) or any(c.startswith("memory_") for c in _called(messages)):
        filters += MEMORY
    elif _REMEMBER.search(request) or "control_note" in _called(messages):
        filters += NOTE
    if browser or APPS in local:
        filters += WAIT  # pages and apps take a moment to load
    if browser:
        filters += BROWSER
    if web:
        filters += WEB
    for group in services:
        filters += group
    return filters


def _called(messages: Sequence[Message]) -> set[str]:
    return {call.name for m in messages for call in m.tool_calls}
