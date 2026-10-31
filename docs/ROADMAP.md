# Arcee — Roadmap and Progress

This file tracks what is planned, what is being worked on and what is done. The design lives in [the specification](./Arcee%20—%20Agent%20Harness%20Specification.md); this file only tracks the build. Update it in the same commit or PR as the work it describes.

**Status legend:** ⬜ not started · 🟨 in progress · ✅ done (gate passed)

## Current status

| Phase | Name | Status | Gate | Tag |
| --- | --- | --- | --- | --- |
| 0 | Repository foundation | 🟨 | Tooling and CI green | — |
| 1 | Core loop (terminal agent) | ✅ | Valid action first try > 90% on LFM3 1.7B, 60% text-mode eval pass | `v0.1.0` |
| 2 | Browser and desktop tree | 🟨 | Web form fill + GUI file rename, zero screenshots | `v0.2.0` |
| 3 | Wayland capture and input | ⬜ | Clicks within 5 px, kill switch < 100 ms | `v0.3.0` |
| 4 | Daemon, extension, app | ⬜ | Fresh account: install, wizard, first task < 10 min | `v0.4.0` |
| 5 | Context manager and voice | ⬜ | Wake → transcript < 1.5 s; week-old fact recalled | `v0.5.0` |
| 6 | Packaging and release | ⬜ | 75% eval pass on the local preset; 20 external testers | `v1.0.0` |

**Now:** Phase 0 done except the license. Phase 1: protocol, reply parsing, adapter base, limiter and 5xx retry merged (1.1-1.4). Models are local-first (decision 0003); the Gemini adapter is parked on `feat/models-gemini`. Tool registry, Ollama adapter, gate, file/shell/control tools, the native-mode step loop and `arcee-cli` merged: `uv run arcee-cli run "..."` runs real tasks on LFM3. Eval suite (LFM3 tier 1: 92%), Planner (long requests only) and task history merged. Base model is LFM3-Omni (decision 0005): eval 85% at the switch, 89% after the harness fixes. Phase 1 done and tagged `v0.1.0` (23 tools; single-decision eval 91%, whole tasks 15/15). Phase 2: web tools, untrusted-content handling and the Chrome extension done; reading Gmail works on `--core groq:openai/gpt-oss-120b`, not yet on LFM3-Omni. Default Core is now Groq gpt-oss-20b, with 120b for hard requests (decision 0008); local models remain as `--core local`. Next: the AT-SPI desktop tree for GNOME apps (`ui.tree`, `ui.act`) and the phase 2 gate.
**Last updated:** 2026-10-29

## Target machine

v1 is built for, and every gate is measured on, the reference machine:

- Ubuntu 26.04.1 LTS, GNOME Shell 50.1 on Wayland
- Intel i7 9th gen, 16 GB RAM, NVIDIA GTX 1650 4 GB (driver 595)
- System Python 3.14 with apt `python3-gi` 3.56; `uv` 0.12; Node 24
- `xdg-desktop-portal-gnome` 50, `bubblewrap` 0.11, AppArmor user-namespace restriction on

Other Ubuntu and GNOME versions are compatibility work after v1.

## How we work

- **Branches.** `main` is always green. One branch per unit of work: `feat/<area>-<thing>`, `fix/...`, `chore/...`, `docs/...`. Merged by PR once CI passes.
- **Commits.** [Conventional Commits](https://www.conventionalcommits.org/): `feat(core): add CoreAction schema`. Small, each one passing tests.
- **Tags.** One per phase gate, as in the table above.
- **Spec drift.** When the code has to differ from the spec, the spec is updated in the same PR and the decision is recorded in `docs/decisions/`.
- **This file.** Tick tasks as they merge, move the phase status, and add a line to the work log.

---

## Phase 0 — Repository foundation

Prerequisites: none.

- [x] `pyproject.toml` managed by `uv`, virtualenv on system Python 3.14 with `--system-site-packages`; extras `[dev]`, `[cuda]`
- [x] Package root `arcee/`; subpackages from spec §16 are added with their first module rather than as empty stubs
- [x] Ruff (lint + format), Pyright, pytest + pytest-asyncio configured
- [x] pre-commit hooks (ruff, pyright, trailing whitespace, end of file)
- [x] GitHub Actions CI: lint, type check, unit tests (no GNOME needed; tests use fakes)
- [x] `.gitignore`, `README.md`
- [ ] `LICENSE` (license to be decided)
- [x] `CONTRIBUTING.md` with the branch and commit rules above
- [x] `docs/decisions/` with a short decision-record template

**Gate:** `uv run pytest`, `uv run ruff check`, `uv run pyright` and CI all pass on `main`.

## Phase 1 — Core loop (terminal agent)

Prerequisites on the machine:

- [x] Ollama installed, `lfm3-omni` (base, decision 0005) and `lfm3` (thinking tier) pulled
- [ ] `lfm2.5:8b` pulled (top of the escalation ladder)

Protocol and models

- [x] 1.1 `arcee/core/protocol.py`: Pydantic models for §7 (Planner output, Core input with `plan`, Core action, executor result, control-action args, confirmation request, replan input); JSON Schema generation tests
- [x] 1.2 `arcee/models/jsonfix.py`: strip fences and surrounding text, `json_repair`, validation-error retry message
- [x] 1.3 `arcee/models/base.py`: `ModelAdapter`, `ModelProfile`, `ModelRequest`, `ModelResponse`, plus a scripted `FakeAdapter` for tests
- [x] 1.4 `arcee/models/limiter.py`: sliding-window `rpm`, daily `rpd` counter, 429 backoff (the fallback switch is per-task state, so it moved to 1.16)
- [ ] ~~1.5 `arcee/models/gemini.py`~~: built and tested, parked unmerged on `feat/models-gemini` (decision 0003)
- [x] 1.6 `arcee/models/ollama.py`: `call_mode` native (`tools`, `tool` messages) and schema (`format`); `num_gpu`, `think` and recommended sampling per profile; `/api/embed`

Tools and safety

- [x] 1.7 `arcee/tools/base.py`, `registry.py`: `Tool` with usage card, `ToolContext`, `ToolResult` with structured `data`, registration, wire names, per-tool argument validation with hints, per-step selection, native and schema rendering
- [x] 1.8 `arcee/core/gate.py` and `arcee/core/policy.py`: tiers, shell allowlist, `never` list, stdin confirmer (`policy.toml` loading and path rules still to do)
- [x] 1.9 Control tools: `done`, `ask_user`, `replan`, `wait`, `speak` (printed until voice), `note` (memories table); `confirm` is covered by the model's `needs_confirmation` path and the gate
- [x] 1.10 File tools: `fs.list` (sort, limit, `by_type`, `total_size`), `read`, `search` (`by_folder`), `trash`, `write` (auto in `~/Arcee/`), `move`, `copy`, `open_with_default`
- [x] 1.11 Shell tools: `shell.run` (allowlist, `never` parsing including chained and substituted commands, `systemd-run` limits, secrets stripped) and `shell.script` in bubblewrap (read-only home, throwaway HOME, no network or session bus; falls back to limits only with a warning)
- [x] 1.12 App tools: `app.open`, `app.list` from the XDG `.desktop` files; also `system.status` and `network.status`

Loop and storage

- [x] 1.13 `arcee/context/store.py` (minimal): SQLite `tasks`, `steps`, `observations`; `actions.jsonl` with secret redaction; `arcee-cli history`
- [x] 1.14 `arcee/core/executor.py`: gate, run with timeouts, errors and crashes as results, record hook for the store
- [x] 1.15 `arcee/core/planner.py`: Planner in schema mode, clarify, replan, per-step tools, used for long requests only (decision 0004); prompts are Python strings for now, Jinja templates when the app needs them editable
- [ ] 1.16 Step loop in `arcee/core/agent.py` (native mode) done: history as tool messages, escalation ladder, code guards, step and time limits; still to do: Planner integration, schema-mode loop, replan, and the rest of task lifecycle, step loop, in-task history packing, escalation ladder, code guards (repeat-lookup block, 3 empty lookups → ask), stop conditions (step and time limits, identical-action loop, replan limit)
- [x] 1.17 `arcee-cli`: run a task from the terminal, confirmations and questions on stdin
- [x] 1.18 `tests/eval/cases.toml`: 54 single-decision cases in 8 scenarios and `arcee-cli eval --tier N`; per-scenario pass rate, valid first reply, latency
- [x] 1.18b `tests/eval/tasks.toml`: 15 whole tasks with checkable end states, `arcee-cli eval-tasks`
- [x] 1.19 Example library (31 examples, tag retrieval), measured on and off: it lowered both evals, so it is off by default (decision 0006)

**Gate:**

- [x] Valid action on the first try above 90% for LFM3 1.7B (native mode): 100% (108/108) on LFM3-Omni, the base model since decision 0005
- [x] 60% or more of the text-mode eval tasks pass: 15/15 whole tasks, 91% of single decisions
- [x] Tag `v0.1.0`

## Phase 2 — Browser and desktop tree

Prerequisites on the machine:

- [ ] `python3-pyatspi` installed (`sudo apt install python3-pyatspi`)
- [ ] `gsettings set org.gnome.desktop.interface toolkit-accessibility true`
- [ ] Playwright npm package (drives the installed Google Chrome; no browser download)

- [x] ~~Playwright worker~~ replaced by a Chrome extension in the user's own profile with a native messaging host (decision 0007); `arcee-cli browser-setup` and `browser-check`
- [x] DOM extraction (one element per row, main content first), grounding level 1, `browser.tabs`/`navigate`/`get_elements`/`read_text`/`click`/`fill`/`scroll`/`back`, one working tab per task
- [x] AT-SPI desktop tree, grounding level 2: `ui.windows`, `ui.tree` (elements plus the window's GTK actions), on a dedicated thread
- [x] `ui.act` using AT-SPI actions only: click, activate, select, focus, set_value, window actions; popovers and dialogs in background windows close at once, so they need `window.focus` from the extension (pulled forward from phase 4)
- [x] Planner `steps[].tools` filter applied to the Core's tool list; tool groups also chosen from the request (web-only requests get no local tools)
- [x] Step-scoped context: each plan step sees the request, the finished steps' handoff notes and its own turns; inside a step only the two latest tool results are whole, older ones shrink to their summary
- [x] Repeat guard keyed on `read_only` tools: any page-changing call (click, fill, tabs, wait) lets reads run again
- [x] Daily token limit (Groq: 200K per model, not in headers) counted per reply and corrected from 429 messages; a spent model hands the task to the other
- [x] Plan steps marked `judgement`: mechanical steps start on gpt-oss-20b, reading and deciding on the routed tier
- [x] Compact page elements (one line each) and tool results filled to the limit instead of halved
- [ ] Window API stub (until the extension exists in phase 4)
- [x] `web.search` (SearXNG or DuckDuckGo HTML), `web.fetch` (no local addresses); untrusted marking and injection detector that forces confirmation of the next action

**Gate:** a web form fill and a GUI file rename both complete using trees only, with zero screenshots sent. Tag `v0.2.0`.

## Phase 3 — Wayland capture and input

Prerequisites on the machine:

- [ ] `ydotool` installed, udev rule, user in `input` group
- [ ] Second monitor, or a virtual one

- [x] Screenshots through the Arcee shell extension (`Shell.Screenshot`), decision 0009; ScreenCast portal + PipeWire kept for sessions without the extension
- [x] Input through the extension's Clutter virtual pointer and keyboard (decision 0009); RemoteDesktop portal kept as the route without the extension
- [ ] ydotool fallback; pressed set mirrored to `$XDG_RUNTIME_DIR/arcee/pressed.json`
- [ ] `ScreenMap`: scaling, fractional scale, multi-monitor offsets
- [x] Set-of-marks as text: OCR lines numbered `t_N` with centres (`screen.read`), raw coordinates with confirmation (level 4); marks drawn on images wait for a vision Core
- [x] `screen.read`, `screen.click`, `screen.scroll`, `kbd.type`, `kbd.key`; pointer moves verified and retried. Level escalation still to do
- [x] Kill switch Super+Shift+Escape: releases input and cancels `arcee-cli run`; every input call is whole, so no watchdog is needed yet. `arceed --release-input` comes with the daemon

**Gate:** clicks land within 5 px on two monitors with fractional scaling; kill switch returns in under 100 ms. Tag `v0.3.0`.

## Phase 4 — Daemon, extension, app

- [ ] `arceed` as a `systemd --user` service (`TimeoutStopSec=2`), D-Bus service and activation
- [x] Control sessions (decision 0010): screen and input only inside a session owned by one D-Bus connection, with a glow on every monitor and a Stop pill; the user's input pauses Arcee (Resume or Stop)
- [ ] GNOME extension: indicator, dropdown, confirmation bubble, keybindings, `org.arcee.Shell`
- [ ] Real `window.*` and `app.close` tools over the extension
- [ ] GTK4 app: Models, Tools, History, Diagnostics pages; the Models page shows each cloud model's tokens and requests left (from `rate-limits.json`) and lets the user choose local or API inference
- [ ] First-run wizard
- [ ] Local dev deb (`dpkg-buildpackage`)

**Gate:** on a fresh user account on the reference machine, install the dev deb, finish the wizard and complete a first task within 10 minutes. Tag `v0.4.0`.

## Phase 5 — Context manager and voice

Before starting: check Python 3.14 wheels for faster-whisper/CTranslate2, onnxruntime and lancedb; decide LanceDB vs ChromaDB.

- [ ] Vector store and embeddings on top of the phase 1 SQLite store
- [ ] Retrieval, re-ranking and token-budget packing
- [ ] Summarizer queue and nightly timer
- [ ] Memory page in the app
- [ ] Wake-word models `hey_arcee` and `arcee_stop` (trained offline), VAD, faster-whisper, Piper
- [ ] Follow-up mode, optional per-user verifier

**Gate:** wake word to transcript in under 1.5 s; a fact from a week-old task is recalled correctly without being restated. Tag `v0.5.0`.

## Phase 6 — Packaging and release

- [ ] Release deb: AppArmor profile loaded in postinst, sandbox helper, models
- [ ] APT repository
- [ ] MCP client
- [ ] Full eval suite (40 tasks) and report
- [ ] User docs
- [ ] Extension submitted to extensions.gnome.org

**Gate:** 75% eval pass rate on the local preset; 20 external testers each complete a task. Tag `v1.0.0`.

---

## Pending decisions

| Decision | Needed by |
| --- | --- |
| License | Phase 0 |
| LanceDB or ChromaDB | Phase 5 start |
| Extension on extensions.gnome.org, or deb only | Phase 6 |
| Animated mascot in the panel | After phase 4, with a prototype |
| Multi-step confirmation batching | v1.1 |

## Work log

- **2026-10-23** — Spec reviewed. Ambiguities fixed: D-Bus-only UI events, kill-switch last resort, AppArmor sandbox helper, wake-word models, Core sees the full plan, roadmap table with gates. Retargeted v1 to the reference machine (Ubuntu 26.04, GNOME 50, Python 3.14); added D-Bus caller checks, `pressed.json` for input release, minimal store and eval tasks in phase 1, dev deb in phase 4. This roadmap added.
- **2026-10-23** — Phase 0: uv project on system Python 3.14 (decision 0001), Ruff, Pyright strict, pytest, pre-commit, GitHub Actions CI, contributing guide. Phase 1: `protocol.py`, `jsonfix.py`, adapter base with shared retry loop and `FakeAdapter`, sliding-window limiter with 429 backoff. 85 unit tests. Spec updated: no `task_id` in Planner output, `text` observation kind, adapter template method, limiter details.
- **2026-10-29** — Model selection. Gemini adapter built (5xx retry, thinking levels, live tests), then parked: the free tier's daily cap is too low for an agent. NVIDIA free endpoints (MiniMax, GLM) queued or never answered. Local benchmarks on 8 agent-decision cases: Qwen3.5 0.8B 2/8, Qwen3.5 2B 4/8 (5/8 with rules and examples), LFM3 1.7B 3/8 plain, 7/8 with rules and examples, 5/8 with native tool calling at 1.55 s. Decision 0003: local-first, LFM3 1.7B Core with an escalation ladder to thinking mode and LFM2.5 8B, native and schema call modes, code guards, usage cards and retrieved examples. Generic retry and thinking changes merged from the Gemini work.
- **2026-10-29** — 1.7 tool registry: `Tool` with usage card and typed args model, `ToolResult` with structured `data` (also added to the protocol's `ExecutorResult`), registration with wire names (`fs.list` ↔ `fs_list`) and collision checks, per-step selection with Planner filters, argument validation whose errors suggest the closest name and list the accepted arguments, rendering for native tool calls and for the schema-mode prompt. 120 unit tests.
- **2026-10-29** — First runnable agent. Ollama adapter (native tool calls and schema mode, `num_gpu`, `think`), permission policy and gate (shell classifier that refuses `sudo`, `rm -r`, disk and power commands even behind `;`, `&&` or `$(...)`), `fs.list`/`read`/`search`/`trash`, `shell.run` under `systemd-run` limits, control tools, executor, native-mode step loop with code guards and the escalation ladder, and `arcee-cli`. Manual runs on the reference machine with LFM3 1.7B: PDF count, free disk space, IP address, newest and biggest file, find-then-read, a confirmed trash of two files, and a refused `rm -rf`, each in 2-3 model calls and 6-38 s. The first biggest-file run compared sizes wrongly, so `fs.list` gained `sort` and `limit`, which made it correct. 242 unit tests.
- **2026-10-29** — Eval suite, Planner, task history. 54 held-out single-decision cases; LFM3 tier 1 went from 47/54 (87%) at baseline to 99/108 (92%) over two runs, with 100% valid first replies and 4.2 s median. Some of the gain comes from fixes the same cases revealed, so it is optimistic. Fixed along the way: `pgrep`'s exit 1 now means "nothing matched"; the provider failing to parse LFM3's own tool call (HTTP 500) no longer crashes the task; tool results capped at 4,000 characters (a 106-entry listing had slowed turns from 3 s to 25-30 s); `fs.search` counts per folder; narration replies naming tools are not answers. Usage-card wording did not change the folder-size and re-listing mistakes; those need code. The Planner works but made short tasks 5-6x slower, so it runs only for long requests (decision 0004). Every run is stored in `history.db` and `actions.jsonl`, redacted. 305 unit tests.
- **2026-10-29** — Base model switched to LFM3-Omni (decision 0005): 92/108 (85%) on the eval against LFM3's 98/108 (91%), faster median (2.7 s against 3.4 s), and image, audio and speech for later phases. The ladder is now LFM3-Omni → LFM3 thinking → LFM2.5 8B, and the Planner uses LFM3-Omni. The gap is to be closed in the harness and tracked with `arcee-cli eval`.
- **2026-10-29** — Closing the LFM3-Omni gap in code: 85% → 89% (96/108, two runs, 100% valid replies, 3.2 s median; using a result it already has went to 24/24). Its misses were picking the wrong fact from text that held the right one (loopback instead of the real IP, 2 of 3 photos, `df` for RAM), so the fix was tools that return the answer: `network.status` (main IP from the route), `system.status` (RAM, disk, CPU, OS), and `fs.list` with `by_type` counts and `total_size`. Every `rm`/`unlink` is refused with the paths to pass `fs.trash`; live, the model's next call was `fs.trash` with them. Final answers are checked for IPs, paths and file names no tool showed (grounding guard; it did not fire on the eval because the model picks real but wrong facts). `fs.search` returns an existing path instead of searching for it. Eval answers now go through the live loop's reply guards (`--raw` for the bare model). Ollama unloads the previous model when the ladder switches. 333 unit tests.
- **2026-10-29** — Phase 1 tool set complete (23 tools): `fs.write`/`move`/`copy`/`open_with_default`, `app.list`/`app.open` from `.desktop` files, `shell.script` in bubblewrap (verified: home stays read-only, no network), `control.wait`/`speak`/`note` with a memories table. With all of them, LFM3-Omni reached 91% (98/108) on the single-decision eval at a 2.0 s median, faster than before. Worked-example library built and measured: 89% with it against 91% without, whole tasks 13/15 against 15/15, slower, so it is off by default (decision 0006). Whole-task eval added: 15/15 with `--plan auto` (median 22 s), 12/15 with `--plan always` (median 65 s), which backs decision 0004. 371 unit tests.
- **2026-10-29** — Phase 1 gate passed on LFM3-Omni: 100% valid first replies (gate: 90%), whole tasks 15/15 (gate: 60%). Tagged `v0.1.0`.
- **2026-10-29** — Phase 2 started: `web.search` and `web.fetch` (25 tools), untrusted-content marking with an injection detector that forces confirmation of the next action (verified end to end in a test: a file saying "ignore previous instructions" made the following `fs.list` need approval). DuckDuckGo started answering 202 bot challenges after a few requests from this machine, so search also takes a SearXNG instance. 389 unit tests.
- **2026-10-30** — Chrome in the user's own profile: Chrome 156's accessibility tree is unusable here and automation is blocked on the default profile, so an extension plus native messaging host drives it (decision 0007). LFM3-Omni could not answer from real Gmail rows (6/6 correct on made-up rows, 0 on real ones). Cloud Core option through an OpenAI-compatible adapter (Groq, OpenRouter); `openai/gpt-oss-120b` on Groq answered the Gmail task in 6 steps. Per-minute token budgets (Groq: 7,000 of 8,000), waits shown in the CLI, intent-based tool groups and a compact tool list for capable models cut a Gmail request from 3,942 to 1,045 tokens. 422 unit tests.
- **2026-10-30** — Default Core switched to Groq (decision 0008): gpt-oss-20b for easy requests, gpt-oss-120b for long, multi-phase, browser and web requests and as the escalation tier, with the Planner on 120b. Routing is decided in code before any model call. "How much free disk space" ran end to end in 1.4 s. Local models stay as `--core local`. 429 unit tests.
- **2026-10-30** — Rate limits from Groq's own counts: every reply's `x-ratelimit-*` headers (free with the reply, no extra calls) give tokens and requests left per model; both refill continuously, so Arcee sends at once when a request fits and otherwise waits only for the shortfall (seconds, not a minute). Counts persist in `~/.local/share/arcee/rate-limits.json`; `arcee-cli limits` shows them; a spent day stops with a clear message. 440 unit tests.
- **2026-10-30** — Desktop tools: `ui.windows`, `ui.tree`, `ui.act` over AT-SPI. On Files they read 56 elements, select files, and run window actions (`view.rename`, `view.move-to-trash`, `window.close`). The GUI rename stops at the rename popover: GTK closes popovers in a window that is not active, and on Wayland Arcee cannot raise another app's window; the GNOME Shell extension's `window.focus` is needed. Also: a repeated question to the user is blocked and counts as a strike. 452 unit tests.
- **2026-10-30** — Gmail runs stalled and waited on the rate budget. A step's closing note ("opened the inbox; browser_get_elements next") tripped the narration guard, which now checks only the final answer. Every call resent the whole history, three page reads of up to 4,000 characters each by the fourth call; now each plan step starts fresh from the request and the earlier steps' notes, and older results inside a step shrink to one line. 462 unit tests.
- **2026-10-31** — A Gmail-to-Calendar run showed five problems. The repeat guard returned stale pages after clicks and searches (it cleared only after approved actions), so the model never read the email it opened; tools now declare `read_only`. Groq's 200K tokens per day, missing from its headers, ended the run with a 372 s retry; the budget now counts it and names the limit from the 429. One task used about 100K tokens on gpt-oss-120b alone; mechanical plan steps now run on 20b, and a spent model hands over. The Planner's 1,024 output tokens cut plans short (a five-step plan took 1,013 with reasoning); it now has 4,096. Page elements are one line each, and results fill the limit: 21 inbox rows instead of 12. 476 unit tests.
- **2026-10-31** — Phase 3 through the shell extension (decision 0009): screenshots, clicks, scrolls, keys and typing over `org.arcee.Shell`, the screen read as numbered OCR lines for text-only models, and a stop shortcut that cancels the task. Tested end to end in a headless GNOME Shell 50 with its own home: read a dialog, typed into it, clicked OK, got the text back. Also: cut-off model replies are retried with more room instead of being repaired into shorter plans, prompts name yesterday's date, and the request's conditions outrank the plan. 506 unit tests.
- **2026-10-31** — Gaps and the first phase 4 piece. A thin window tree now points to `screen.read`; a whole Gmail-style task test found that a new plan step's first page read counted as a repeat, so steps now start with a fresh repeat guard; two monitors checked (screenshot 2304x800, clicks on the second land). Control sessions (decision 0010): glow and Stop pill while Arcee holds the screen, the user's own mouse or keyboard pauses it, and nothing reads or drives the screen outside a session. Tested headless: refused without control, glow and pill shown, Stop ends the session and the task, a crashed holder releases it. 511 unit tests.
