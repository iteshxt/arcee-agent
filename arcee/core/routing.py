"""Which ladder tier a request starts on (decision 0008).

Decided in code before any model is called. Easy requests start on the small
model; requests that are long or multi-phase, that read the web or the user's
browser, or that work in desktop apps, start on the large one, because that is
where the small model measurably failed (real inbox rows, multi-step plans; in
Files it asked for confirmation five times, and told to use Calculator's
buttons it did the sum itself). Escalation still moves an easy request up when
the small model stumbles.
"""

from arcee.core.planner import needs_plan
from arcee.core.toolsets import BROWSER, DESKTOP, WEB, filters_for
from arcee.models.base import Message


def is_hard(request: str) -> bool:
    filters = filters_for([Message("user", request)])
    return needs_plan(request) or any(group in filters for group in (*BROWSER, *WEB, *DESKTOP))


def start_tier(request: str) -> int:
    """1 (the large model) for hard requests, 0 otherwise."""
    return 1 if is_hard(request) else 0
