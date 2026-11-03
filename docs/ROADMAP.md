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
| 5 | Context manager and voice | 🟨 | Wake → transcript < 1.5 s; week-old fact recalled | `v0.5.0` |
| 6 | Packaging and release | ⬜ | 75% eval pass on the local preset; 20 external testers | `v1.0.0` |

**Now:** Phases 0-1 done (license still to choose). Phase 2: built; the web form gate still has to pass on a model with tokens left (on LFM3-Omni it skipped a field; a refused submit is now reported). Phase 3: passed, `v0.3.0`. Phase 4: everything built, including the .deb (GitHub pre-release `v0.3.1`) and guided setup for the GNOME extension and for local models; the gate is a fresh account installing and finishing a task in 10 minutes. Phase 5: memory built and its gate's memory half passed (a fact told in passing, aged 8 days, answered unprompted); voice (wake word, local speech) is next. Still open: the phase 2 and 4 gates.
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

Before starting: check Python 3.14 wheels for faster-whisper/CTranslate2 and onnxruntime. LanceDB vs ChromaDB: neither (decision 0014): both have 3.14 wheels but install at 416 and 368 MB.

- [x] Memory store on the phase 1 SQLite store (decision 0014): `memories` and `summaries` with FTS5 indexes, an in-place upgrade of older history.db files; embeddings deferred (an optional vector column when Ollama has an embedding model)
- [x] Recall: important facts always, then BM25 matches over facts and past tasks, ranked 0.6 match / 0.25 recency (14-day half-life) / 0.15 importance, packed into 1,500 characters, into the Planner's and the Core's prompts per request and per plan step; `memory.recall` and `memory.forget` (asks first) for the Core
- [x] Digest after every task, in the background, on the Core's cheap model: a summary and up to five lasting facts, updating known facts instead of repeating them
- [ ] Nightly day and week summaries
- [x] Memory page: each fact with its source and date, edit, forget with Undo, tell Arcee something, export JSON, and a switch to stop learning
- [x] Voice in two tiers (decision 0015): "hey Arcee" and "Arcee stop" heard on this computer by sherpa-onnx keyword spotting (no training), Silero VAD cutting out the request, which alone goes to Groq's Whisper; the 7 MB of models downloaded from the app. A local speech-to-text model (Moonshine) was built, measured and dropped
- [x] "Hey Arcee" trained on the user's voice (wake_match): three samples and an ordinary sentence, matched by dynamic time warping on log-mel frames alongside the spotter; from the Voice settings (opened by itself when the wake word is first turned on) or `arcee-cli voice-train`
- [ ] The voice gate measured with the user's own voice (end of speech to transcript under 1.5 s; Groq answered in 0.4-0.8 s for 2.6-3.5 s requests, once hung for 20 s, now retried after 8 s)
- [ ] Spoken replies: sherpa-onnx 2.0.1 has no Piper frontend (espeak-ng), and no older release supports Python 3.14; speech-dispatcher or another route
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
- **2026-11-03** — Memory (phase 5, decision 0014). Across 103 real tasks the Core had never saved a note, so memory is made after each task: a digest on the cheap model writes a summary and the lasting facts it showed. history.db gained `summaries`, FTS5 indexes and an in-place upgrade; recall ranks facts and past tasks into the Planner's and the Core's prompts; `memory.recall` and `memory.forget`; a Memory page with a switch to stop learning. End to end on Groq in a throwaway data folder: "my home station is Abu Road (ABR)" said in passing became three facts and a summary; aged 8 days, "which station should I book my train to Delhi from?" got "Abu Road (ABR)" in one call, and a third request added no duplicate facts.
- **2026-11-03** — Past tasks learned on LFM3-Omni: its 93 summaries were good, its facts mostly the tasks' own results at high importance, so they were removed and local models now write summaries only. Voice (decision 0015): sherpa-onnx (43 MB) over faster-whisper, openWakeWord and Piper (427, 277, 166 MB); Moonshine base beat Whisper base.en three to one on speed at equal accuracy; the wake-word listener ran end to end on recorded speech (request cut out by the VAD, transcribed in 0.34 s). The Models page's Voice section downloads the models and turns on "Hey Arcee".
- **2026-11-03** — Voice in two tiers, at the user's word: the wake word here, the request on Groq (Moonshine built, measured, dropped). Listening cost 4% of a core, 3.5% of it the spotter decoding a quiet room; the VAD (0.7%) now wakes the spotter only on speech, with 0.8 s of pre-roll: 0.8-1.1% idle. The wake word shows a cue: the active ring, from the app's loop as 30 frames (GNOME 50's shell has no sprite-sheet animation), at the top centre, swelling with the voice, gone once Arcee has the words. The headless test caught an option GNOME 50 rejects (`affectsInputRegion`), which would have stopped the whole extension loading.
- **2026-11-03** — Two real failures. "Go to Cron." (Whisper's Chrome) listed files: the Core now hears when a request was spoken and that a name matching nothing is likely a mishearing (live: spoken, it looked up the Chrome app; typed, it searched for crontab). "How did Kim Kardashian become popular" made 13 failed lookups (DuckDuckGo's 202 bot check after a burst, Wikipedia's 403, connections that never opened): search now falls back to Wikipedia's own search API, retries a connection that never opened once, and after two failed lookups in a task the web tools tell the Core to answer from what it knows; Wikipedia gets a User-Agent that names Arcee.
- **2026-11-03** — News. "Tell me the latest news" failed on DuckDuckGo's 202. `web.news` reads Google News' RSS (top stories, or a topic's; the edition from the system language, India's here): live, two steps and 2.2 s to five headlines with their sources. Bing's RSS and Mojeek were tried as general fallbacks and dropped (Bing ranked badly and once returned porn for a celebrity question; Mojeek blocks bots). General web search still leans on DuckDuckGo; a SearXNG instance or a Brave Search key would make it dependable.
- **2026-11-03** — "Hey Arcee" never woke Arcee for the user. `arcee-cli voice-test` records 10 s and shows what each tier hears: Whisper heard "Hey Arcee" every time, the spotter 2 of 4 with any spelling, the same at every threshold, so sensitivity was not the problem. The user's built-in mic clips (peaks at full scale) over a steady −33 dB hiss, and "Arcee" is not in the 3.3M English model's training; on its own clean test clips the spotter found every keyword. So the wake word is trained to the user's voice, as phone assistants do (their idea): three "Hey Arcee"s and an ordinary sentence, kept as their sound (24 log-mel bands per 20 ms; no audio), matched by subsequence dynamic time warping in plain Python, only while the VAD hears speech (3% of a core for features, 4% for matching, while talking). On the user's recording, each held-out sample matched at 0.10-0.14 against a threshold of about 0.15, and nothing else came under 0.29. arceed's `HoldWake` stops the listener while it is trained and takes up the result without a restart. 668 unit tests.
- **2026-11-03** — Voice, at the user's word. Training is part of first-run setup: a "Hey Arcee" page turns the wake word on (7 MB, asked first) and trains it in place; Voice settings trains it again or forgets it. The match now keeps room for the longest sample, so "hey… Arcee" said with a pause matches as well as run together (it was cut at 1.6 s). The top-centre cue is 30% smaller (67 px) and follows a spoken request: what was heard, each step, then the reply, so the panel need not be opened; the Stop pill sits below it, and a reply on the cue gets no notification banner (GNOME put it on top of the cue). The cue takes no clicks and hides for Arcee's screenshots. Tested in a headless GNOME Shell 50, wake to reply.
- **2026-11-03** — The cue made slim, at the user's word ("too big… very inconspicuous, but still noticeable"): a 30 px ring in a pill 36 px tall just below the top bar (it was 79 px), 9 pt text, a lighter background; a reply longer than a line wraps to a second. The Stop pill below it is slimmed to match.
- **2026-11-03** — "What time is it?" and "What's the weather like?" were answered "I don't have access" by LFM3-Omni (the user's Core is Local): it had the time in its prompt and no weather tool. The prompt now says to answer time and date questions from it; `web.weather` (wttr.in, which places the user from the network with no key, and Open-Meteo for a named place when wttr.in fails) gives now, today and tomorrow, by name; weather words and "will…?" questions bring the web tools; a greeting before a request ("Hey, how hot is it outside?") no longer makes it small talk; a reply that gives up or asks where the user is before any lookup is sent back to the tools with the exact call. On LFM3-Omni: time and day in one call, weather in 8-11 s (one nudge), 8 of 8 right.
- **2026-11-03** — One pill, and stopping. The control session's own pill is gone: Arcee's pill at the top centre takes a Stop button (and Resume, in the user's blue, when they take the screen back), and shows whenever Arcee holds the screen, typed requests included. Esc stops everything while the pill shows: a request being heard is dropped before it reaches Groq, one being transcribed or run is cancelled (arceed's Cancel now does all three). "Arcee stop" was being heard as a wake word and its words run as a request ("Arcee stopped.", "Toa Parsi."): a request that is only "stop", "cancel" or "never mind" now stops what runs, one made while Arcee is busy says so instead of being dropped silently, and training asks for "Arcee stop" twice, matched like the wake word and kept apart from it. The end of a request was already a pause of 0.7 s (the log showed 0.7-5.4 s requests cut right).
- **2026-11-04** — The pill, from real use. It went away while a request was still running ("Do I have anything on my calendar for tomorrow?": the local model planned for 45 s without a step, and the pill only waited 10 s), so the user thought Arcee had stopped; it now stays for as long as the request runs. A wrapped reply spilled out of it (the label became wrapping after the layout had sized it for one line): the reply has its own wrapping label, sized to its text, up to 560 px wide and 700 characters. Every reply now goes on the pill, typed or spoken, unless the panel menu or the app is in front, and stays long enough to read (8-45 s) until Esc or a click anywhere puts it away; no notification banner. The pill is 15% bigger (35 px ring, 10.5 pt).
- **2026-11-04** — "Open Chrome." went to chromium.org: not a default anywhere, the model made the address up, because a request naming Chrome was offered only browser tools (the app tools are dropped from browser requests), and navigating was its only way to "open" anything. A request that only opens something now keeps the app tools, and one opening the browser itself gets no browser tools; the Core is told that opening an app is the whole task when nothing else is asked, and browser.navigate's card says so. Held-out eval cases "open-only": 6/6 on LFM3-Omni, "Open Chrome." 4/4 on Groq (app.open, no page). A task catalogue (164 tasks in 18 categories, the tools each needs and what is missing) is kept locally in docs/tasks/, not committed.
- **2026-11-04** — Replies are formatted. The models answer in Markdown, shown raw in the pill, the panel and the app ("- **bin** – ~8 GB" on one line). arcee/markdown.py turns it into Pango markup (headings, bold, italic, code, lists, quotes, links, simple tables), for the app's History and Agent pages; the extension's markdown.js does the same for the pill and the panel, and a test runs both on the same replies (node) and checks they agree and that the markup parses. Seen in that reply too: "which folders take up most space" listed / and read the 8 GB swapfile as every folder's size; logged in the task catalogue (FILE, fs.usage), with event-driven waits, background steps and Arcee's own timers (AGENT) at the user's word.
- **2026-11-04** — Three from the task catalogue. `fs.usage` (du on one filesystem, folders with their contents, largest first): "which folders take up most space" now gives the real top five instead of the swapfile's size for every folder. `media.control` and `media.now_playing` over MPRIS: pause, next, what's playing, for Spotify, Chrome or VLC with no screen (eval 9/9 on LFM3-Omni and Groq). A conversation (decision 0016): the pill stays after a reply, "hey Arcee" goes on, Esc or a click closes it; arceed gives each request the turns before it and their tools. "And tomorrow?", "What about in Delhi?" and "Now open YouTube in it" were right on both models.
