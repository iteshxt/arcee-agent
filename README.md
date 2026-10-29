# Arcee

Arcee (pronounced "R-C") is a model-agnostic desktop agent harness for Ubuntu on GNOME and Wayland. It takes a voice or text request, plans it, carries it out step by step with tools (shell, files, browser, desktop GUI, web) and reports back. Any model can fill any role: a cloud API, an OpenAI-compatible endpoint or a local model through Ollama.

**Status:** early development. See [the roadmap](docs/ROADMAP.md) for progress and [the specification](docs/) for the design.

## Try it

Arcee runs on a local model through [Ollama](https://ollama.com). Install Ollama, then pull the default model (1.1 GB):

```sh
ollama pull lfm3
uv sync
uv run arcee-cli run "How much free disk space do I have?"
```

Each step is printed as it happens: the model's tool call, the result, and any guard or escalation. Read-only commands run at once; anything that changes files asks first (`Allow? [y/N]`), and destructive commands such as `rm -rf` or `sudo` are refused. Add `-v` to see the model's reasoning when it uses thinking mode. `ollama pull lfm2.5:8b` adds the slower top tier of the escalation ladder.

## Development setup

v1 targets Ubuntu 26.04 with GNOME 50. The virtualenv is built on the system Python so the apt-provided GTK and accessibility bindings stay importable.

```sh
uv venv --python /usr/bin/python3 --system-site-packages
uv sync
uv run pre-commit install
```

Run the checks:

```sh
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

Live tests talk to the real local model and are skipped by default. With Ollama running and `ollama pull lfm3` done:

```sh
uv run pytest -m live
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch and commit rules.
