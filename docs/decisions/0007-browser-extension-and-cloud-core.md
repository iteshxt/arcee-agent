# 0007. The user's own Chrome through an extension; a cloud Core as an option

Date: 2026-10-30
Status: accepted

## Context

The user wants Arcee to act in their real, signed-in Chrome (Gmail and the like). Measured on the reference machine:

- Chrome 136+ refuses automation (Playwright, remote debugging) on the default profile directory, and the profile is locked while Chrome runs.
- Chrome 156's accessibility tree is not usable here: after a restart, `chrome://accessibility`, and `--force-renderer-accessibility`, it exposed frames whose children could not be read (0 elements through `pyatspi`, `Atspi` and the Collection interface). GNOME apps (Files) work fine through the same library.
- A Chrome extension reading the DOM saw Gmail fully: 297 interactive elements including every inbox row.

With the extension working, LFM3-Omni still failed "tell me the 3 newest emails in my inbox": given three made-up inbox rows it answered 6/6, given the real, denser rows it asked to read the page again every time. `openai/gpt-oss-120b` on Groq answered the same task correctly in 6 steps (the user ran it, since it sends inbox text to Groq).

The free cloud tiers all have a binding limit. OpenRouter's free Gemma 4 31B was rate-limited upstream on every try (its paid variant worked, at about $0.20 per million input tokens). Groq's free tier allows 1,000 requests a day but only 8,000 tokens a minute, and a first Gmail run used 12.2K tokens in a minute.

## Decision

1. **Browser:** a Manifest V3 extension (fixed ID from a committed public key) in the user's Chrome, started-by-Chrome native messaging host `org.arcee.browser`, and a 0600 Unix socket in the runtime directory. No network listener. Playwright is not used.
2. **Cloud Core as an option:** an OpenAI-compatible adapter covers Groq and OpenRouter; `--core groq:<model>` replaces the local ladder. The local preset stays the default (decision 0005).
3. **Token budgets:** profiles take `tpm`; requests are admitted against a rolling minute using a size estimate, settled to the provider's count. Groq is budgeted at 7,000 of its 8,000.
4. **Smaller requests:** tool groups are chosen in code from the request (web-only requests get no local tools, and the reverse), and capable models get a compact tool list without usage cards. On gpt-oss-120b a Gmail request fell from 3,942 to 1,045 input tokens.

## Consequences

- Arcee can read and act in any site the user is signed into, with confirmation for send, delete, pay and similar clicks, and for password and card fields.
- With a cloud Core, what a task reads (inbox text, file listings) leaves the machine. Running that is the user's decision each time; Claude Code's own safety check blocked an agent-initiated run that would have sent inbox contents to Groq.
- Revisit the default once whole-task evals run on a cloud Core, and when `lfm2.5:8b` is pulled, which may read real pages well enough locally.
