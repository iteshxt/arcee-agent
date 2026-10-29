# 0005. LFM3-Omni is the base model

Date: 2026-10-29
Status: accepted (supersedes the tier-1 choice in 0003)

## Context

Decision 0003 made LFM3 1.7B the default Core. LFM3-Omni is the same 1.7B model with image, audio and speech input and output, which the screenshot grounding (phase 3) and voice pipeline (phase 5) need. On the 54-case eval suite, two runs each on the reference machine:

| | LFM3 | LFM3-Omni |
| --- | --- | --- |
| Correct | 98/108 (91%) | 92/108 (85%) |
| Valid first reply | 100% | 100% |
| Median / max per decision | 3.4 s / 8.5 s | 2.7 s / 18.5 s |
| Size | 1.1 GB | 2.0 GB |
| Thinking mode | yes | no |

LFM3's misses are cautious (one more listing before acting). LFM3-Omni's are wrong answers with the fact in context (an IP address, a count), a search where a sort was needed, and once `rm -f` instead of `fs.trash` (the gate would have asked first).

## Decision

LFM3-Omni is the base model: tier 1 of the Core ladder and the Planner (schema mode). The ladder becomes LFM3-Omni → LFM3 with thinking (LFM3-Omni has no thinking mode) → LFM2.5 8B. The 6-point gap is to be closed in the harness: code guards, computed tool facts, context assembly with retrieved examples, and tighter step handling.

## Consequences

- One model serves text, screenshots and voice, so later phases need no extra resident model.
- It starts behind LFM3 on tool decisions, and its failures are wrong answers rather than extra steps, so guards and the confirmation gate matter more. Every harness change is measured with `arcee-cli eval` against the 85% starting point.
- Escalating to tier 2 swaps in a second model (LFM3, 1.1 GB); next to the desktop and LFM3-Omni this may not fit in 4 GB, so Ollama may unload one, costing a few seconds.
- Revisit if the gap does not close, or if an LFM3-Omni release adds thinking.
