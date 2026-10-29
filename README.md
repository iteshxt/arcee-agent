# Arcee

Arcee (pronounced "R-C") is a model-agnostic desktop agent harness for Ubuntu on GNOME and Wayland. It takes a voice or text request, plans it, carries it out step by step with tools (shell, files, browser, desktop GUI, web) and reports back. Any model can fill any role: a cloud API, an OpenAI-compatible endpoint or a local model through Ollama.

**Status:** early development. See [the roadmap](docs/ROADMAP.md) for progress and [the specification](docs/) for the design.

## Try it

By default Arcee thinks with `openai/gpt-oss-20b` and, for harder requests, `openai/gpt-oss-120b` on [Groq](https://console.groq.com) (decision 0008). Put a key in a git-ignored `.env` in the repository root:

```sh
echo 'GROQ_API_KEY=gsk_...' >> .env
uv sync
uv run arcee-cli run "How much free disk space do I have?"
```

To keep everything on your machine instead, install [Ollama](https://ollama.com), `ollama pull lfm3-omni` and `ollama pull lfm3`, and add `--core local`.

Each step is printed as it happens: the model's tool call, the result, and any guard or escalation. Read-only commands run at once; anything that changes files asks first (`Allow? [y/N]`), and destructive commands such as `rm -rf` or `sudo` are refused. Add `-v` to see the model's reasoning when it uses thinking mode. `uv run arcee-cli history` lists past tasks; `uv run arcee-cli eval` scores single decisions and `uv run arcee-cli eval-tasks` runs whole tasks in a throwaway home folder. `ollama pull lfm2.5:8b` adds the slower top tier of the escalation ladder.

### Your own Chrome

Arcee reads and acts in your signed-in Chrome through a small extension. Run `uv run arcee-cli browser-setup`, then load `browser-ext/` as an unpacked extension in `chrome://extensions` (Developer mode), and check with `uv run arcee-cli browser-check`. Clicking anything like Send, Delete or Pay asks you first.

### A cloud model instead of the local one

`--core groq:openai/gpt-oss-120b` (key `GROQ_API_KEY` in `.env`) or `--core openrouter:<model>` (`OPENROUTER_API_KEY`) puts a cloud model in the Core's place. What the task reads is sent to that provider.

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

Live tests talk to the real local model and are skipped by default. With Ollama running and `ollama pull lfm3-omni` done:

```sh
uv run pytest -m live
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch and commit rules.
