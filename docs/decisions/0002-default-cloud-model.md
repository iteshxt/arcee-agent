# 0002. Gemini 3.5 Flash-Lite as the default free cloud model

Date: 2026-10-29
Status: accepted (revised the same day: first draft chose Gemma 4 26B A4B)

## Context

The harness needs a free hosted model for the Planner and Core that is fast, follows the JSON protocol reliably and makes sound agent decisions. One free Gemini API key gives access to the Gemini Flash family and to Gemma 4 (`gemma-4-26b-a4b-it`, `gemma-4-31b-it`) through the same `generateContent` endpoint and the same adapter.

Gemma 4 was tried first. Probing showed:

- About half of requests failed with HTTP 500/503 and succeeded on retry, on both `generateContent` and the newer Interactions API (which failed more often, with 503s).
- Latency for identical requests ranged from 2.5 s to over 60 s.
- Thinking is on unless `thinkingLevel` is set; a three-word reply spent 570 thinking tokens.

We then measured four free models on the same key with the flattened `CoreAction` schema and thinking off (2026-10-29):

| Model | Valid JSON, first try | Correct decisions (8 agent cases) | Median | Max |
|---|---|---|---|---|
| gemini-3.5-flash-lite | 6/6 | 7/8 | 1.6 s | 2.8 s |
| gemini-3.5-flash | 6/6 | 6/8 | 9.6 s | 128.5 s |
| gemini-3.1-flash-lite | 6/6 | 6/8 | 2.7 s | 7.1 s |
| gemma-4-26b-a4b-it | 5/6 | 6/8 | 6.0 s | 50.0 s |

The decision cases cover looking before deleting, running a command instead of guessing, answering from an observation, giving up after three failed searches, and ignoring an instruction planted in a file. `gemini-3.8-flash` rejects `thinkingLevel: minimal` and returned "quota exceeded" on this key, so it has no usable free quota.

## Decision

- Planner and Core default to `gemini-3.5-flash-lite`, thinking on for the Planner and off for the Core.
- Fallback chain on the same key: `gemini-3.1-flash-lite`, then `gemma-4-26b-a4b-it`.
- `ModelProfile.thinking` is a provider-neutral flag; the Gemini adapter maps it to `thinkingLevel` (`high` / `minimal`).
- HTTP 5xx and dropped connections are retried with the same backoff as 429; a 180 s read timeout is not retried.
- Local models through Ollama are deferred.

## Consequences

- One adapter and one key cover every default and fallback model; switching is configuration only.
- Free daily quotas per model are not published reliably (third-party reports range from about 20/day to 1,500/day). The real `rpd` values must be read from AI Studio's rate-limit page and set in each profile; the fallback chain covers running out mid-task.
- Groq (OpenAI-compatible, fast, free per-model limits) is the candidate second provider if Google's free quotas prove too small; it needs the `openai_compat` adapter and a separate key.
