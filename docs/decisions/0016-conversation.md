# 0016. A conversation lasts as long as the pill

Date: 2026-11-04
Status: accepted

## Context

Every request ran alone. "Open Chrome", then "open YouTube", opened YouTube without
knowing Chrome was just opened; "what's the weather?", then "and tomorrow?", was read
as a question about the date. Spoken use made it worse: the pill went away after each
reply, so nothing on screen said a conversation was going on.

## Decision

At the user's word (2026-11-04):

- **The pill is the conversation.** After a reply it stays on screen with the reply and
  a hint ("Say “Hey Arcee” to go on · Esc to close"). Listening stops; "hey Arcee"
  starts the next turn. Esc, or a click anywhere else, closes the pill and ends the
  conversation (`EndConversation` on arceed); so does the app's New button.
- **arceed keeps the turns** (`arcee/context/conversation.py`): each request, what was
  done (each step's tool and summary), how it ended, and the reply; the last 6.
- **A new request gets them** in the planner's and the Core's prompts, with a note that
  a short follow-up asks the last question again, changed as it says, and that what was
  opened may still be on screen. The earlier requests' tools are offered again, so
  "and tomorrow?" has the weather tool.
- **Typed requests join** the conversation that is open, from the panel or the app.
- **It ends by itself** after 10 minutes without a request, in case the pill went away
  without saying so (a restart).
- **Nothing is stored**: the turns live in arceed's memory only. What should last is
  memory's job (decision 0014).

## Consequences

- Follow-ups work: on LFM3-Omni and on Groq, "what's the weather?", "and tomorrow?",
  "what about Delhi?", and "open Chrome", "now open YouTube in it", were all right.
- Prompts grow by up to about 2,000 characters in a conversation; each turn is cut to
  300 characters of request and reply and 5 actions.
- A wake word that hears nothing, or an error, no longer closes the pill: it shows the
  message, then the last reply again.
