"""Getting the local models ready: is Ollama installed and running, are the models there.

Choosing Local (Ollama) in the app used to assume all of that. Now the app checks,
and with the user's yes downloads the models through Ollama's own API (no root
needed). Installing Ollama itself needs root, so the app shows Ollama's official
install command and opens a terminal for it.
"""

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from arcee.models.ollama import DEFAULT_HOST
from arcee.models.presets import LFM3_OMNI, LFM3_THINKING

INSTALL_COMMAND: Final = "curl -fsSL https://ollama.com/install.sh | sh"
REGISTRY: Final = "https://registry.ollama.ai/v2/library"
# What local mode needs, and what helps: the base model, then the thinking rung it
# escalates to. (LFM2.5 8B, the third rung, is left to people who want it.)
NEEDED: Final = (LFM3_OMNI.model,)
HELPFUL: Final = (LFM3_THINKING.model,)


@dataclass(frozen=True, slots=True)
class LocalStatus:
    installed: bool
    running: bool
    models: frozenset[str] = field(default_factory=frozenset[str])

    def has(self, model: str) -> bool:
        """`lfm3` matches `lfm3:latest`, as Ollama names it."""
        return model in self.models or f"{model}:latest" in self.models

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(m for m in NEEDED if not self.has(m))

    @property
    def ready(self) -> bool:
        return self.running and not self.missing


async def status(
    host: str = DEFAULT_HOST, transport: httpx.AsyncBaseTransport | None = None
) -> LocalStatus:
    installed = shutil.which("ollama") is not None
    try:
        async with httpx.AsyncClient(base_url=host, timeout=3, transport=transport) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
    except httpx.HTTPError:
        # A server can run without the binary on PATH (a snap, a container): judge by
        # the server, not the file.
        return LocalStatus(installed, running=False)
    names = frozenset(str(m.get("name", "")) for m in response.json().get("models", []))
    return LocalStatus(installed=True, running=True, models=names)


async def download_size(model: str) -> int | None:
    """Bytes `ollama pull model` downloads, from the registry; None if unknown."""
    name, _, tag = model.partition(":")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{REGISTRY}/{name}/manifests/{tag or 'latest'}",
                headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            )
            response.raise_for_status()
            layers: list[dict[str, Any]] = response.json().get("layers", [])
    except httpx.HTTPError, ValueError:
        return None
    return sum(int(layer.get("size", 0)) for layer in layers) or None


async def pull(
    model: str,
    on_progress: Callable[[str, float | None], None],
    host: str = DEFAULT_HOST,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Download `model` through Ollama, reporting (status, fraction done or None)."""
    timeout = httpx.Timeout(30, read=None)  # a layer can take minutes between lines
    async with (
        httpx.AsyncClient(base_url=host, timeout=timeout, transport=transport) as client,
        client.stream("POST", "/api/pull", json={"model": model, "stream": True}) as response,
    ):
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            event: dict[str, Any] = json.loads(line)
            if error := event.get("error"):
                raise RuntimeError(f"Ollama could not download {model}: {error}")
            total, done = event.get("total"), event.get("completed")
            fraction = done / total if total and done is not None else None
            on_progress(str(event.get("status", "")), fraction)


def gigabytes(size: int | None) -> str:
    return f"{size / 1e9:.1f} GB" if size else "a few GB"
