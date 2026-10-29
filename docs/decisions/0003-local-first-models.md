# 0003. Local-first models, native tool calling and code-side guards

Date: 2026-10-29
Status: accepted

## Context

The spec assumed free cloud models for the Planner and Core. In practice the free Gemini tier allows too few requests a day to run an agent: each task makes 5 to 15 model calls. NVIDIA's free endpoints (MiniMax M3.1 Flash, GLM 5.3 Flash) queued for 2 minutes or never answered. The Gemini adapter and the cloud benchmark (decision 0002) are kept on the unmerged branch `feat/models-gemini`.

Local models on the reference machine (GTX 1650, 4 GB; the desktop itself uses 0.8 to 2 GB) were measured on the same 8 agent-decision cases. Thinking was off, every layer was on the GPU, and the median time is per decision:

| Model | Prompt | Correct | Median |
| --- | --- | --- | --- |
| Qwen3.5 0.8B | plain JSON | 2/8 | 0.8 s |
| Qwen3.5 2B (Q8) | plain JSON | 4/8 | 2.9 s |
| Qwen3.5 2B | JSON + rules + examples | 5/8 | 2.1 s |
| LFM3 1.7B (Q4, 1.1 GiB) | plain JSON | 3/8 | 1.4 s |
| LFM3 1.7B | JSON + rules + examples | 7/8 | 1.9 s |
| LFM3 1.7B | native tool calls | 5/8 | 1.55 s |
| LFM3 1.7B | native tool calls, thinking on | 6/8 | 4.5 s |
| Gemini 3.5 Flash-Lite (cloud, for reference) | plain JSON | 7/8 | 1.6 s |

The rules and examples were written after seeing failures on these same cases, so 7/8 is optimistic. Eight cases also cannot separate the two call modes once context is added. Three findings hold regardless:

1. **What the model sees matters more than which model it is.** LFM3 went from 3/8 to 7/8 with better context alone.
2. **Small models copy examples instead of applying them.** Qwen answered "2 PDFs" because an example said 2. Examples must be close to the current situation or left out.
3. **The remaining failures are mechanical.** The models keep searching after three misses, list again instead of acting, count wrong, or pick the wrong tool. Code can catch all of these; prompting cannot be relied on to.

LFM3 is trained for native tool calling: tool schemas go through its chat template, calls come back as `<tool_call>` XML, and results return as `tool` messages. Ollama ships a parser for it. Liquid recommends temperature 0.1, top_k 50 and repetition penalty 1.1, and says disabling thinking "can be inconsistent" on reasoning-heavy tasks.

## Decision

1. **Local-first.** The default preset runs on the reference machine with no cloud model. Cloud adapters remain optional for users who bring a key.
2. **Model ladder for the Core**, climbed only on failures the harness can detect:

   | Tier | Model |
   | --- | --- |
   | 1 | LFM3 1.7B, thinking off |
   | 2 | The same model with thinking on (no model swap) |
   | 3 | LFM2.5 8B (partly on CPU, seconds per step) |

   The triggers are output still invalid after retries, tool arguments rejected, a loop detected, a step failing repeatedly, or the model reporting low `confidence`. A confidently wrong action looks like success and cannot trigger escalation; the permission gate and confirmations cover that case.
3. **Two call modes behind the adapter.** The harness keeps one canonical `Action`. A profile's `call_mode` decides how it travels:
   - `native`: tool schemas go in the provider's `tools` field, results return as `tool` messages, and a plain-text reply means `control.done`.
   - `schema`: one JSON object constrained by a schema, as before.

   LFM3 uses `native`. The eval suite decides the default per model.
4. **Code guards before prompting.** Tools return computed facts (counts, matches), not raw text. The registry validates arguments per tool and returns errors the model can fix. Each step is offered only the tools that fit it. The orchestrator enforces rules such as "after 3 empty searches, ask the user" and loop detection.
5. **Context assembly.** Every tool carries a usage card (when to use it, when not to, common mistakes, a short example). Only the cards for the tools offered this step are sent. A library of verified situation-to-action examples is searched per step, and the 2 or 3 nearest are included, never weak matches. The library is built after the eval suite, which keeps its test cases out of the library.

## Consequences

- No API quota, no network dependency and no data leaving the machine on the default preset.
- The adapter base class grows a tool-call path beside the schema path, and the eval suite has to cover both modes.
- The spec's earlier rule, "Arcee never uses a provider's native function-calling", is reversed. Tool names are mapped for providers that forbid dots (`fs.list` ↔ `fs_list`).
- LFM2.5 uses a different tool-call format from LFM3. Whether Ollama parses it is checked when the model is pulled.
- An 8B model does not fit in 4 GB, so tier 3 is slow and swaps models instead of keeping both loaded.
- The phase 1 gate moves from "JSON validity on Gemini" to "valid actions on the local preset".
- Revisit once the eval suite has about 50 cases: the call-mode default, whether tier 2 earns its latency, and how much the example library helps.
