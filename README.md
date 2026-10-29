# Arcee

Arcee (pronounced "R-C") is a model-agnostic desktop agent harness for Ubuntu on GNOME and Wayland. It takes a voice or text request, plans it, carries it out step by step with tools (shell, files, browser, desktop GUI, web) and reports back. Any model can fill any role: a cloud API, an OpenAI-compatible endpoint or a local model through Ollama.

**Status:** early development. See [the roadmap](docs/ROADMAP.md) for progress and [the specification](docs/) for the design.

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
