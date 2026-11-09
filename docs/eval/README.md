# Evals

Two suites measure Arcee's Core. Both run offline against whichever models you choose.

## Whole tasks: `arcee-cli eval-tasks`

44 requests a person would make (`tests/eval/tasks.toml`), each run end to end
exactly as `arcee-cli run` would: Planner when the request calls for it, the
escalation ladder, guards, the gate, and **every built-in tool**, offered by the same
per-request groups as in real use.

- Each task runs in a throwaway home folder and a throwaway History, so files,
  reminders, routines and memory are real but nothing of yours is touched.
- Tools that would reach the network, the desktop, Chrome or your accounts
  (settings, music, WhatsApp, mail, calendar, windows, the web…) are **stubs**:
  the model sees the same name, card, arguments and permission tier, but the run
  returns the task's canned answer. A run is repeatable and changes nothing.
- A task passes when the end state is right: files that exist, are gone or contain
  text; what the reply says; whether Arcee asked; which tools it called (and did
  not call), with which arguments.

```bash
uv run arcee-cli eval-tasks --core local --report docs/eval/$(date +%F)-local.md
```

```bash
uv run arcee-cli eval-tasks --core groq --only trains
```

`--only` takes part of a task id; `--plan always|never` forces or skips the Planner.
The phase 6 gate is 75% on the local preset.

## Single decisions: `arcee-cli eval`

`tests/eval/cases.toml`: one model turn each, checked for the tool and arguments it
chooses. Faster; it cannot see the Planner or multi-step behaviour.

## Reports

Reports land here, one per run (`<date>-<core>.md`): totals, a table by category,
and every task with its tools and problems.
