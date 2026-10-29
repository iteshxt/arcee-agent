# 0004. Plan only long or multi-phase requests

Date: 2026-10-29
Status: accepted

## Context

The spec calls the Planner once per task (section 6). With the Planner built on LFM3 1.7B in schema mode, live runs on the reference machine showed:

| Request | Without a plan | With a plan |
| --- | --- | --- |
| "Hi Arcee!" | 4 s, 1 call | 41 s; the plan was "ask the user what they want" |
| "Find the biggest file in Downloads, then tell me the free disk space" | 20-22 s, 3 calls, correct both runs | 98-137 s, 8-9 calls; split into 3-4 steps including a "determine the biggest" step with no tool work; one run answered only the last part |

A plan cost 9-22 s by itself. The small Planner splits simple work into steps the Core then has to walk through one at a time, and a stricter planner prompt (steps are tool work only; the last step answers the whole request) fixed the incomplete answer but not the over-splitting. The Core alone handles two-part requests in one conversation.

The single-decision eval suite cannot measure the Planner, so this decision rests on live runs, not on a benchmark.

## Decision

`arcee-cli run` defaults to `--plan auto`, which calls the Planner only when the request is long (30 words or more), has three or more sentences, or has two or more multi-phase connectors (then, after that, first, finally, ...). `--plan always` and `--plan never` override it. When a plan is made, everything in spec section 6 applies: plan in the Core's prompt, tools per step, replans on a stuck report.

## Consequences

- Short and two-part requests answer in seconds instead of minutes.
- Long requests still get a plan, with its cost and its over-splitting; a stronger Planner tier (LFM2.5 8B or thinking mode) may fix that.
- The heuristic is a guess. Multi-step eval tasks (whole tasks with checkable end states, not single decisions) are needed to measure when planning helps; until then, revisit when the Planner model changes.
