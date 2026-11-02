# 0014. Memory in SQLite, ranked by full-text search, built after each task

Date: 2026-11-03
Status: accepted

## Context

Phase 5 gives Arcee memory: facts about the user and what past tasks did, recalled when
they matter (spec section 8). The gate: a fact from a week-old task is recalled correctly
without being restated.

The spec planned LanceDB for vectors, with ChromaDB as the alternative. Both have wheels
for Python 3.14, but they install at 416 MB and 368 MB, against an 11 MB .deb. Memory for
one person is small: tens of facts, hundreds of task summaries a year.

Embeddings need a local model, and most users run the Groq Core without Ollama; Groq has
no embedding API.

`control.note` has existed since phase 1, yet across 103 real tasks the models never
called it: nothing was ever saved. Memory left to the Core's initiative does not happen.

## Decision

1. Memory lives in `history.db` with the rest of the history: `memories` (a fact, its
   importance, where it came from, when it was last changed) and `summaries` (one per
   task). Each has an FTS5 index (Porter stemming), kept in step by triggers. No new
   dependency: SQLite 3.46 on Ubuntu 26.04 has FTS5.
2. After each finished task a digest runs in the background on the Core's fast model
   (gpt-oss-20b, or LFM3-Omni locally): a summary of at most 120 words and the durable
   facts about the user it learned, each with an importance, shown the related facts
   already saved so it can update one instead of repeating it. Small talk and tasks with
   nothing to remember add no facts.
3. Recall, at the start of a request and of each plan step: the facts most worth
   knowing (importance 0.8 and up) always, then facts and past-task summaries matching the
   request by BM25, ranked `0.6 * match + 0.25 * recency (half-life 14 days) + 0.15 *
   importance`, the current task left out, packed into about 1,500 characters (the
   Groq Core's 8,000-tokens-a-minute budget is tight). It goes into the Core's system
   prompt and the Planner's.
4. The Core can also look things up (`memory.recall`), save a fact it is told to
   remember (`control.note`), and forget one (`memory.forget`, after asking).
5. The app's Memory page lists the facts, to edit, delete or export; and can forget
   everything from one task. The app writes to `history.db` directly (WAL mode lets the
   daemon and the app share it), not through D-Bus as the spec had it.
6. Embeddings are left for later: when Ollama has an embedding model, a vector column and
   cosine similarity can join the ranking without changing anything above.

## Consequences

- Memory works on the Groq Core with no Ollama, at no extra install size, and recall
  costs a few milliseconds.
- Matching is by words: "my station" finds "home station is Abu Road", but "where I
  catch the train" does not. Facts with high importance are always included, which
  covers most of what a person tells Arcee about themselves; embeddings would cover
  the rest.
- Each task costs one more model call (about 1,500 tokens) for its digest.
- Nightly day and week summaries (spec section 8) are not built yet; with a summary per
  task and recency in the ranking they matter less until history grows large.
