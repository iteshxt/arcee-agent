# 0006. Worked examples are off by default

Date: 2026-10-29
Status: accepted

## Context

Decision 0003 planned a library of worked examples, with the closest few put into the Core's prompt each step. It was built (31 hand-written examples, BM25 retrieval over per-example intent tags, nothing shown below a score threshold) and measured on LFM3-Omni with all 23 tools, on and off:

| | Examples off | Examples on |
| --- | --- | --- |
| Single-decision eval (54 cases x2) | 98/108 (91%), median 2.0 s, p90 2.9 s | 96/108 (89%), median 3.1 s, p90 12.0 s |
| Whole tasks (15) | 15/15, median 22 s | 13/15, median 33 s |

The examples fixed none of the habits they targeted (installed versus running, sort for "newest", trash versus move). They broke two whole tasks: per-folder counting, and asking the user when a file cannot be found. And they made one decision case worse: an example about ignoring loopback addresses contains IP addresses of its own. The longer prompt also slowed every turn.

Retrieval quality was a problem too: matching on situation text picked examples by folder names rather than intent, which is why tags were added. Tagging fixed the top match but not the outcome.

## Decision

The library ships but is off by default; `--examples` turns it on for `run`, `eval` and `eval-tasks`. The harness keeps closing gaps with tools that return the answer and with guards, which measured better.

## Consequences

- Prompts stay short and turns fast.
- The library, its retrieval and the leakage test remain, so the experiment can be repeated cheaply.
- Revisit when embedding retrieval exists (phase 5), when the base model changes, or with a library grown from verified real runs rather than hand-written cases.
