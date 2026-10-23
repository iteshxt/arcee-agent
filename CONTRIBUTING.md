# Contributing

## Setup

See the README for the virtualenv setup. Install the git hooks once with `uv run pre-commit install`; they run Ruff and Pyright on every commit.

## Branches

- `main` is always green: every commit on it passes lint, type checks and tests.
- One branch per unit of work, named by type: `feat/<area>-<thing>`, `fix/<thing>`, `chore/<thing>`, `docs/<thing>`, `test/<thing>`.
- A branch is merged into `main` with a merge commit (`--no-ff`, or a GitHub PR) once CI passes, so each unit stays visible in the history.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, imperative mood, at most 72 characters in the summary.

- Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `chore`, `ci`, `build`.
- Scopes follow the package layout: `core`, `models`, `tools`, `context`, `platform`, `voice`, `app`, `extension`, `spec`.
- Each commit builds and passes tests on its own. A body explains why when the summary cannot.

## Code

- Python 3.14, type-checked with Pyright in strict mode for `arcee/`.
- Pydantic v2 models at every stage boundary; no untyped dicts crossing modules.
- Nothing blocking on the daemon's event loop: file I/O, subprocess waits and heavy CPU work go to a thread or child process.
- Tests sit next to their layer in `tests/unit` or `tests/integration`. Agent logic is tested against fakes (`FakeAdapter`, `FakePlatform`), never a live model or desktop.

## Spec and roadmap

- When code has to differ from the specification, update the spec in the same branch and add a record in `docs/decisions/`.
- Tick the matching task in `docs/ROADMAP.md` in the same branch as the work.
