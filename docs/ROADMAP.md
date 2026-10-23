# Arcee — Roadmap and Progress

This file tracks what is planned, what is being worked on and what is done. The design lives in [the specification](./Arcee%20—%20Agent%20Harness%20Specification.md); this file only tracks the build. Update it in the same commit or PR as the work it describes.

**Status legend:** ⬜ not started · 🟨 in progress · ✅ done (gate passed)

## Current status

| Phase | Name | Status | Gate | Tag |
| --- | --- | --- | --- | --- |
| 0 | Repository foundation | 🟨 | Tooling and CI green | — |
| 1 | Core loop (terminal agent) | ⬜ | JSON first-try > 90%, 60% text-mode eval pass | `v0.1.0` |
| 2 | Browser and desktop tree | ⬜ | Web form fill + GUI file rename, zero screenshots | `v0.2.0` |
| 3 | Wayland capture and input | ⬜ | Clicks within 5 px, kill switch < 100 ms | `v0.3.0` |
| 4 | Daemon, extension, app | ⬜ | Fresh account: install, wizard, first task < 10 min | `v0.4.0` |
| 5 | Context manager and voice | ⬜ | Wake → transcript < 1.5 s; week-old fact recalled | `v0.5.0` |
| 6 | Packaging and release | ⬜ | 75% eval pass on free preset; 20 external testers | `v1.0.0` |

**Now:** Phase 0 tooling in place (license pending); Phase 1 started.
**Last updated:** 2026-10-23

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

- [ ] Gemini API key available as an environment variable (never committed)
- [ ] Ollama installed, `qwen3:4b` pulled (thinking disabled for JSON output)

Protocol and models

- [x] 1.1 `arcee/core/protocol.py`: Pydantic models for §7 (Planner output, Core input with `plan`, Core action, executor result, control-action args, confirmation request, replan input); JSON Schema generation tests
- [x] 1.2 `arcee/models/jsonfix.py`: strip fences and surrounding text, `json_repair`, validation-error retry message
- [x] 1.3 `arcee/models/base.py`: `ModelAdapter`, `ModelProfile`, `ModelRequest`, `ModelResponse`, plus a scripted `FakeAdapter` for tests
- [ ] 1.4 `arcee/models/limiter.py`: token bucket from `rpm`, daily `rpd` counter, 429 backoff, fallback switch
- [ ] 1.5 `arcee/models/gemini.py`: REST client with `responseSchema`
- [ ] 1.6 `arcee/models/ollama.py`: `/api/chat` with `format` schema, `/api/embed`

Tools and safety

- [ ] 1.7 `arcee/tools/base.py`, `registry.py`: `Tool`, `ToolContext`, `ToolResult`, discovery, prompt rendering
- [ ] 1.8 `arcee/core/gate.py` and `config/policy.toml` defaults: tiers, shell allowlist, `never` list, path rules, stdin confirmer
- [ ] 1.9 Control tools: `control.done`, `ask_user`, `confirm`, `replan`, `wait`, `speak` (prints in CLI), `note`
- [ ] 1.10 File tools: `fs.list`, `read`, `write`, `search`, `move`, `copy`, `trash`, `open_with_default`
- [ ] 1.11 Shell tools: `shell.run` (allowlist and `never` parsing, `systemd-run` limits), `shell.script` via the sandbox helper; dev script that installs the AppArmor profile; fallback to resource limits only when isolation is off
- [ ] 1.12 App tools: `app.open`, `app.list` (`Gio.DesktopAppInfo`)

Loop and storage

- [ ] 1.13 `arcee/context/store.py` (minimal): SQLite `tasks`, `steps`, `observations`; `actions.jsonl` with secret redaction
- [ ] 1.14 `arcee/core/executor.py`: validate, gate, run with timeouts, truncate output, store
- [ ] 1.15 Prompt templates `planner.j2`, `core.j2`, `tools.j2`; `arcee/core/planner.py`
- [ ] 1.16 `arcee/core/orchestrator.py`: task lifecycle, step loop, in-task history packing, stop conditions (step and time limits, identical-action loop, replan limit)
- [ ] 1.17 `arcee-cli`: run a task from the terminal, confirmations and questions on stdin
- [ ] 1.18 `tests/eval/`: 10-15 text-mode tasks with machine-checkable criteria, and the `arcee eval` runner

**Gate:**

- [ ] JSON first-try validity above 90% on Gemini Flash
- [ ] JSON first-try validity above 90% on Qwen 3 4B (Ollama)
- [ ] 60% or more of the text-mode eval tasks pass
- [ ] Tag `v0.1.0`

## Phase 2 — Browser and desktop tree

Prerequisites on the machine:

- [ ] `python3-pyatspi` installed
- [ ] `gsettings set org.gnome.desktop.interface toolkit-accessibility true`

- [ ] Playwright worker (`browser-worker/`, Node, JSON-RPC over stdio), dedicated profile
- [ ] DOM tree extraction, grounding level 1, and the `browser.*` tools
- [ ] AT-SPI desktop tree, grounding level 2, `ui.tree`
- [ ] `ui.act` using AT-SPI actions only (pointer fallback comes in phase 3)
- [ ] Planner `steps[].tools` filter applied to the Core's tool list
- [ ] Window API stub (until the extension exists in phase 4)
- [ ] `web.search`, `web.fetch`; untrusted-content wrapper and injection detector

**Gate:** a web form fill and a GUI file rename both complete using trees only, with zero screenshots sent. Tag `v0.2.0`.

## Phase 3 — Wayland capture and input

Prerequisites on the machine:

- [ ] `ydotool` installed, udev rule, user in `input` group
- [ ] Second monitor, or a virtual one

- [ ] ScreenCast portal + PipeWire frame grab, restore token in the keyring
- [ ] RemoteDesktop portal input on the same session
- [ ] ydotool fallback; pressed set mirrored to `$XDG_RUNTIME_DIR/arcee/pressed.json`
- [ ] `ScreenMap`: scaling, fractional scale, multi-monitor offsets
- [ ] Set-of-marks (level 3), raw coordinates (level 4), `screen.ocr_find`, text-match targets
- [ ] `mouse.*`, `kbd.*`, `screen.*` tools; click verification and level escalation
- [ ] Kill switch, `release_all`, watchdog thread, `arceed --release-input`

**Gate:** clicks land within 5 px on two monitors with fractional scaling; kill switch returns in under 100 ms. Tag `v0.3.0`.

## Phase 4 — Daemon, extension, app

- [ ] `arceed` as a `systemd --user` service (`TimeoutStopSec=2`), D-Bus service and activation
- [ ] D-Bus caller checks on state-changing methods
- [ ] GNOME extension: indicator, dropdown, confirmation bubble, keybindings, `org.arcee.Shell`
- [ ] Real `window.*` and `app.close` tools over the extension
- [ ] GTK4 app: Models, Tools, History, Diagnostics pages
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

**Gate:** 75% eval pass rate on the free preset; 20 external testers each complete a task. Tag `v1.0.0`.

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
