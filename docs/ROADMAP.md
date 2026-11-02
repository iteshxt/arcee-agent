# Arcee — Roadmap and Progress

This file tracks what is planned, what is being worked on and what is done. The design lives in [the specification](./Arcee%20—%20Agent%20Harness%20Specification.md); this file only tracks the build. Update it in the same commit or PR as the work it describes.

**Status legend:** ⬜ not started · 🟨 in progress · ✅ done (gate passed)

## Current status

| Phase | Name | Status | Gate | Tag |
| --- | --- | --- | --- | --- |
| 0 | Repository foundation | 🟨 | Tooling and CI green | — |
| 1 | Core loop (terminal agent) | ✅ | Valid action first try > 90% on LFM3 1.7B, 60% text-mode eval pass | `v0.1.0` |
| 2 | Browser and desktop tree | 🟨 | Web form fill + GUI file rename, zero screenshots | `v0.2.0` |
| 3 | Wayland capture and input | ✅ | Clicks within 5 px, kill switch < 100 ms | `v0.3.0` |
| 4 | Daemon, extension, app | 🟨 | Fresh account: install, wizard, first task < 10 min | `v0.4.0` |
| 5 | Context manager and voice | ⬜ | Wake → transcript < 1.5 s; week-old fact recalled | `v0.5.0` |
| 6 | Packaging and release | ⬜ | 75% eval pass on the local preset; 20 external testers | `v1.0.0` |

**Now:** Phases 0-1 done (license still to choose). Phase 2: built; the web form gate still has to pass on a model with tokens left (on LFM3-Omni it skipped a field; a refused submit is now reported). Phase 3: passed, `v0.3.0`. Phase 4: everything built, including the .deb (GitHub pre-release `v0.3.1`) and guided setup for the GNOME extension and for local models; the gate is a fresh account installing and finishing a task in 10 minutes. Next: the phase 2 and 4 gates, then phase 5.
**Last updated:** 2026-11-02

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
- [x] ~~Window API stub~~: the extension's `window.list`/`window.act` came first (phase 4)
- [x] `web.search` (SearXNG or DuckDuckGo HTML), `web.fetch` (no local addresses); untrusted marking and injection detector that forces confirmation of the next action

**Gate:** a web form fill and a GUI file rename both complete using trees only, with zero screenshots sent. Tag `v0.2.0`.

## Phase 3 — Wayland capture and input

Prerequisites on the machine:

- [x] ~~`ydotool`~~: input goes through the extension (decision 0009)
- [x] Second monitor: two virtual monitors in a headless GNOME Shell

- [x] Screenshots through the Arcee shell extension (`Shell.Screenshot`), decision 0009; ScreenCast portal + PipeWire kept for sessions without the extension
- [x] Input through the extension's Clutter virtual pointer and keyboard (decision 0009); RemoteDesktop portal kept as the route without the extension
- [x] ~~ydotool fallback~~: not needed with the extension's virtual devices, which release what they press on stop
- [x] Scaling: screenshots cover the whole stage at the highest monitor scale, so one image-to-stage ratio maps every monitor; checked at 100-200% and mixed scales (no `ScreenMap` needed)
- [x] Set-of-marks as text: OCR lines numbered `t_N` with centres (`screen.read`), raw coordinates with confirmation (level 4); `screen.find` and `screen.look` with qwen3.8-27b by grid cells (decision 0013); pointer glides to targets
- [x] `screen.read`, `screen.click`, `screen.scroll`, `kbd.type`, `kbd.key`; pointer moves verified and retried. Level escalation still to do
- [x] Kill switch Super+Shift+Escape: releases input and cancels `arcee-cli run`; every input call is whole, so no watchdog is needed yet. `arceed --release-input` comes with the daemon

**Gate:** clicks land within 5 px on two monitors with fractional scaling; kill switch returns in under 100 ms. Tag `v0.3.0`.

**Gate result (2026-11-02, headless GNOME Shell 50, two virtual monitors):** 60 clicks through Arcee's screenshot-to-stage path at 100/100, 125/100, 166/125, 100/133 and 200/100 % scales: largest miss 1.41 px. Super+Shift+Escape to the request stopped in arceed: 4-6 ms over 10 tries (median 4), control released every time. Passed.

## Phase 4 — Daemon, extension, app

- [x] `arceed` as a `systemd --user` service (`TimeoutStopSec=2`), D-Bus service `me.iteshxt.Arcee.Agent` with activation (`arcee-cli daemon-install`); runs one request at a time, asks questions and confirmations over D-Bus
- [x] Control sessions (decision 0010): screen and input only inside a session owned by one D-Bus connection, with a glow on every monitor and a Stop pill; the user's input pauses Arcee (Resume or Stop)
- [x] GNOME extension: indicator and menu (ask, live progress, Stop, questions and confirmations with buttons, recent tasks, today's tokens, Quit), stop keybinding, glow and Stop pill, `me.iteshxt.Arcee.Shell`. Mic with a live waveform: pw-record, transcribed by Groq `whisper-large-v3-turbo` on its own key (`GROQ_API_KEY_TTS`)
- [x] `window.list`, `window.act` (focus, minimize, maximize, restore, close) and `app.close` over the extension, inside control sessions; closing asks first, and windows GNOME will not minimize or maximize are reported, not pretended
- [x] GTK4 app (`arcee-app`, libadwaita): Models (Groq or local, today's tokens and requests per model, live), History (tasks and their steps), Permissions (ask before everything, or always before chosen actions), Diagnostics (every part with its fix). Opened from the panel's Open Arcee or the app grid; settings in `~/.config/arcee/settings.json`, read by arceed and arcee-cli
- [x] First-run setup in the app: welcome (by Itesh Tomar), Groq keys into the keyring, desktop control, background service and Chrome each installed with a button (no sudo); Diagnostics has Fix buttons for the same installers
- [x] Guided setup on a fresh install: setup says what the GNOME extension still needs (log out and back in, then Arcee turns it on once by itself), Diagnostics reads GNOME's own state of it, and choosing Local (Ollama) installs Ollama (in a terminal, with the user's password) and downloads the models (with their size, after a yes)
- [x] Local dev deb: `sh packaging/build-deb.sh` (dpkg-deb, no debhelper or sudo), system Python with vendored dependencies; extension, service, D-Bus activation, launcher and Chrome host for every user

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
- [ ] MCP client
- [ ] Full eval suite (40 tasks) and report
- [ ] User docs
- [ ] Extension submitted to extensions.gnome.org
- [ ] Chrome extension on the Chrome Web Store, once it stops changing; setup's Chrome step then opens its store page
- [ ] `sudo apt install` from an APT repository (until then: the .deb on GitHub releases)
- [ ] Links to the documentation site (iteshxt.me) in About, setup and help text

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
- **2026-10-31** — Phase 3 through the shell extension (decision 0009): screenshots, clicks, scrolls, keys and typing over `me.iteshxt.Arcee.Shell`, the screen read as numbered OCR lines for text-only models, and a stop shortcut that cancels the task. Tested end to end in a headless GNOME Shell 50 with its own home: read a dialog, typed into it, clicked OK, got the text back. Also: cut-off model replies are retried with more room instead of being repaired into shorter plans, prompts name yesterday's date, and the request's conditions outrank the plan. 506 unit tests.
- **2026-10-31** — Gaps and the first phase 4 piece. A thin window tree now points to `screen.read`; a whole Gmail-style task test found that a new plan step's first page read counted as a repeat, so steps now start with a fresh repeat guard; two monitors checked (screenshot 2304x800, clicks on the second land). Control sessions (decision 0010): glow and Stop pill while Arcee holds the screen, the user's own mouse or keyboard pauses it, and nothing reads or drives the screen outside a session. Tested headless: refused without control, glow and pill shown, Stop ends the session and the task, a crashed holder releases it. 511 unit tests.
- **2026-10-31** — Phase 4: `arceed` serves `me.iteshxt.Arcee.Agent` (dbus-fast) on a shared `TaskRunner` that `arcee-cli run` now uses too; the panel menu asks, shows progress, answers confirmations and shows the reply, tested end to end in a headless GNOME Shell. Every visible action takes control (glow, pause), Chrome is raised on Wayland and started when closed, apps already open are focused. OCR now reads the screen whole and in bands (the top bar's black strip hid a dark menu from Tesseract) and merges them. 524 unit tests.
- **2026-11-01** — Names under the developer's domain (decision 0011): extension `arcee@iteshxt.me`, app `me.iteshxt.Arcee`, D-Bus `me.iteshxt.Arcee.Shell` and `.Agent`, Chrome host `me.iteshxt.arcee.browser`; credit to Itesh Tomar (iteshxt.me) in metadata, README and the app's About. Installers moved to `arcee/install.py` and clean up the old names.
- **2026-11-01** — Models take turns (decision 0012): gpt-oss-20b, qwen3.8-27b and gpt-oss-120b each have their own Groq allowance; a call goes to the first that need not wait, every request starts on the 20b-led rank, and 120b leads the escalation rank.
- **2026-11-01** — The app gains keys (GNOME Keyring), a Whisper usage bar, an all-of-these permission switch, first-run setup screens and Fix buttons in Diagnostics, all on `arcee/install.py`. Tested in a headless GNOME Shell: every setup step, saving a permission set, the keys and the About dialog.
- **2026-11-01** — Vision (decision 0013): `screen.find` locates icons and buttons with qwen3.8-27b in two grid rounds and a local snap (9 px and 21 px from the centre on a test desktop); `screen.look` answers questions about the screen. The pointer glides to its target.
- **2026-11-01** — The app's first page is Agent: a sunrise particle ring that breathes, follows the voice and swirls while Arcee works, a mic and a type button, and the conversation with questions answered in place. The panel menu is redone: ask, status card with Stop, question card, Recent folded to three with More in Arcee (opens History), today's budget as bars, Open Arcee and Quit. arceed runs what it hears itself; keys that are set show as set. Windows are raised with a fresh timestamp, so GNOME's focus-stealing prevention no longer keeps Chrome behind.
- **2026-11-01** — Arcee says what it is doing: arceed sends a `Step` line ("Opening Google Chrome", "Searching the web for james bond", "Thinking…"), shown on the pill in place of "Arcee is working", in the panel and under the app's ring. The glow is sunrise orange while Arcee works and blue while the user has control. The Agent page's mic becomes Stop while Arcee works, a New button clears the conversation, and unanswered questions close when the request ends. The Chrome extension now connects at browser start (`runtime.onStartup`): when Arcee started Chrome, the bridge had stayed down and the request gave up after 30 s. After every login the extension had to be reloaded by hand for the same reason; an alarm every 30 s now also wakes the worker to reconnect if the bridge drops (extension 0.3.2).
- **2026-11-02** — Phase gates. Phase 3 passed (clicks within 1.41 px at mixed fractional scales; stop in 4-6 ms) after a fix found on the way: arceed never heard the shell's Stopped signal, so the stop shortcut and the pill's Stop took the glow away while the request went on. Phase 2's GUI rename, seven runs in a headless session, zero screenshots in all but one, found and fixed: `ui.act` ran an element's first action for names it lacked (set-text ran activate and kept the old name); window actions asked on an item now select it first; apps are found by their .desktop names ("Files"); Groq's 400 "Parsing failed" now asks the model again instead of ending the task. Still open: the models type new names on the keyboard, and Files selects the name without its extension, so name.txt becomes name.txt.txt before they repair it. `browser.fill` now chooses dropdown options and ticks boxes (extension 0.3.3). Dev deb: `packaging/build-deb.sh`.
- **2026-11-02** — A fresh device. The app froze and lagged there: the Agent page's ring drew 1,100 points one fill at a time, 60 times a second, even when idle (a fifth of a fast core); now four fills per ribbon at 12-30 frames a second (3-4% idle), and saving a key no longer waits on the keyring's first-use password prompt on the GTK thread. The app logs errors and hard crashes to ~/.local/share/arcee/logs. Setup now says exactly what the GNOME extension needs, and Arcee turns it on once after the login that loads it. Choosing Local (Ollama) checks for Ollama and LFM3-Omni and sets up what is missing, asking first. Every process re-reads the shared rate limits (the panel had shown full bars for models arcee-cli had used up), with a refresh button in the panel and the app. A slow GNOME Shell (Chrome cold-starting) no longer fails a request. Release assets: `packaging/build-release.sh` and `publish-release.sh`; `v0.3.1` is a GitHub pre-release.
- **2026-11-02** — Arcee's look. The Agent page's ring is no longer drawn live: `tools/make_art.py` renders two loops once (idle breathing, active pulsing; 410 and 520 KB GIFs of amber particle ribbons round a wireframe globe, after a reference the user chose), and the app decodes each once and shows its frames in turn, only while on screen: 1% of a core when idle, down from 20%. The same ring is the icon everywhere: the app (particle ring at 64 px and up, a simpler vector ring below), the top bar, the Chrome extension (0.3.5) and the package's icon theme. Then, at the user's word: the centre is a geodesic sphere inside a scalloped ring, as in the reference (not a latitude-longitude globe), and the active loop swings half as far, one wave, about 3 s a loop. A checkout installs the icon into the user's icon theme like the .deb; `arcee-cli browser-reload` reloads the Chrome extension (0.3.6+).
- **2026-11-02** — Light style. The panel menu reads its own background and switches to dark ink on a light one (it was white on white). The ring is transparent, in a dark-window and a light-window look (animated WebP with alpha, 370-470 KB each), switching with the window's style. `tests/desktop/startup.sh` starts the app under nine bad cases (fresh, corrupt files, unwritable data, every page, the packaged files): all open in about a second and keep answering. Release 0.3.4.
- **2026-11-03** — The app icon has the geodesic sphere too (one split of the icosahedron, so it reads at 64 px); every size is cut from the 256 px picture, which keeps it amber. Release 0.3.5.
