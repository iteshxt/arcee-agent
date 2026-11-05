"""The conversation: the last few requests, what Arcee did for each, and its replies.

A request is not always complete on its own: "open Chrome", then "open YouTube" (in
the Chrome just opened); "what's the weather?", then "and tomorrow?". While the user
keeps the conversation going (the pill is on screen, or a request came in the last
IDLE_S), each new request is given the turns before it. It ends when the user closes
the pill (Esc, a click elsewhere), starts a New conversation in the app, or after
IDLE_S without a request. Each request records the conversation's `id` in History, so
the app can list conversations and take one up again (`resume`). Memory (decision
0014) is for what should last.
"""

import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

MAX_TURNS: Final = 6
IDLE_S: Final = 600.0
# Characters of each turn's reply and actions: the context stays small for small models.
REPLY_CHARS: Final = 300
DID_LINES: Final = 5
DID_CHARS: Final = 90


@dataclass(frozen=True, slots=True)
class Turn:
    request: str
    reply: str
    status: str  # done, stopped, failed...
    did: tuple[str, ...] = ()  # "app.open: Opened Google Chrome", for each step that ran


def _short(text: str, limit: int) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


@dataclass(slots=True)
class Conversation:
    clock: Callable[[], float] = time.monotonic
    turns: list[Turn] = field(default_factory=list[Turn])
    id: str = ""
    _last: float = 0.0

    def begin(self) -> str:
        """The id of the conversation the next request is in: this one, or (after an
        end or a long pause) a new one."""
        if not self.active or not self.id:
            self.turns.clear()
            self.id = f"c_{secrets.token_hex(6)}"
        return self.id

    def resume(self, conversation_id: str, turns: Sequence[Turn]) -> None:
        """Take up an earlier conversation: its last turns, as if just said."""
        self.id = conversation_id
        self.turns = list(turns)[-MAX_TURNS:]
        self._last = self.clock()

    def add(self, turn: Turn) -> None:
        if not self.active:
            self.turns.clear()  # a request after a long pause starts a new conversation
        self.turns = [*self.turns, turn][-MAX_TURNS:]
        self._last = self.clock()

    def end(self) -> None:
        self.turns.clear()
        self.id = ""
        self._last = 0.0

    @property
    def active(self) -> bool:
        return bool(self.turns) and self.clock() - self._last < IDLE_S

    def requests(self) -> Sequence[str]:
        """The earlier requests, oldest first (their tools are offered again)."""
        return [t.request for t in self.turns] if self.active else []

    def render(self) -> str:
        """The turns so far, for the planner's and the Core's prompts; "" without any."""
        if not self.active:
            return ""
        lines = [
            "This request continues a conversation. Earlier in it, oldest first (the new "
            'request may refer to these: "it", "there", "and tomorrow?", "now open '
            'YouTube"; what was opened may still be on screen):'
        ]
        for n, turn in enumerate(self.turns, 1):
            lines.append(f"{n}. The user asked: {_short(turn.request, REPLY_CHARS)}")
            lines.extend(f"   You did: {_short(did, DID_CHARS)}" for did in turn.did[-DID_LINES:])
            if turn.status != "done":
                lines.append(f"   It ended: {turn.status}")
            if turn.reply:
                lines.append(f"   You replied: {_short(turn.reply, REPLY_CHARS)}")
        lines.append(
            "The new request is the next turn. A short follow-up asks the last question again, "
            'changed as it says: after the weather, "and tomorrow?" is tomorrow\'s weather and '
            '"what about Delhi?" is Delhi\'s; after opening an app, "now open X" happens in '
            "it. Use what is already open or known; do not redo earlier turns unless asked."
        )
        return "\n".join(lines)
