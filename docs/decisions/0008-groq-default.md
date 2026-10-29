# 0008. Groq gpt-oss is the default: 20b for easy requests, 120b for hard ones

Date: 2026-10-30
Status: accepted (supersedes the default in 0005; local models stay available)

## Context

LFM3-Omni reached 91% on single decisions and 15/15 whole local tasks, but could not answer from real web pages: given the user's actual Gmail rows it asked to read the page again every time. `openai/gpt-oss-120b` on Groq read the same inbox correctly. A first run of the new default (`gpt-oss-20b`, "how much free disk space") took 1.4 s end to end. Groq's free tier allows 1,000 requests a day and 8,000 tokens a minute per model; Arcee budgets 7,000 per model and keeps requests near 1,000-1,500 tokens (decision 0007).

## Decision

- The default Core ladder is `openai/gpt-oss-20b` then `openai/gpt-oss-120b`, both on Groq; the Planner is `gpt-oss-120b`, sharing that model's token budget.
- Code routes each request before any model call (`arcee/core/routing.py`): long or multi-phase requests, and anything involving the browser or the web, start on 120b; the rest start on 20b. Escalation still moves a struggling 20b step up, and a successful step returns to the task's starting tier.
- The local Ollama ladder (LFM3-Omni, LFM3 thinking, LFM2.5 8B) stays in the harness as `--core local`; the app will offer the choice of inference later.

## Consequences

- Faster and more capable on real pages; needs `GROQ_API_KEY` and a network connection.
- What a task reads goes to Groq, which ends the "nothing leaves the machine" default. Users who need that run `--core local`.
- Evals now spend Groq quota; a full single-decision run is about 54 requests and roughly 60-80K tokens.
