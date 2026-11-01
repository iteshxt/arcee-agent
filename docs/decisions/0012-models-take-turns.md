# 0012. Three Groq models take turns; every request starts on gpt-oss-20b

Date: 2026-11-01
Status: accepted (refines 0008)

## Context

Decision 0008 sent easy requests to gpt-oss-20b and hard ones (long, browser, web, and
later desktop apps) to gpt-oss-120b. In use, 20b handled most requests, and the waits
came from rate limits, not from the models failing: a busy minute on one model made the
user wait while the others sat idle. Groq gives each model its own allowance: 8,000
tokens a minute, 1,000 requests and 200,000 tokens a day for gpt-oss-20b, gpt-oss-120b
and qwen/qwen3.8-27b alike. qwen3.8-27b calls tools correctly on Groq (checked with a
canned request).

## Decision

1. Every request starts on the first rank of the ladder, led by gpt-oss-20b. The request
   routing of 0008 is removed.
2. A rank holds several models in order of preference (`RotatingAdapter`): rank 1 is
   20b, qwen3.8-27b, 120b; rank 2 (escalation, after failures) is 120b, qwen3.8-27b,
   20b; the Planner prefers 120b, then qwen, then 20b. Each call goes to the first model
   whose rate budget can take it within 2 s; if every one would wait, to the one that
   frees up soonest. A model whose day is spent is skipped.
3. Events, budgets and the app name the model that actually answered.

## Consequences

- Most requests never wait on a rate limit, and a day's work spreads over three
  allowances instead of one.
- Requests that 0008 sent straight to 120b (Gmail rows, GUI work) now start on 20b and
  reach 120b only by escalation. If 20b proves weak there again, start those on rank 2.
- Consecutive calls of one task can go to different models; each sees the same
  conversation, so this is safe, but their styles may differ.
