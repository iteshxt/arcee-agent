"""Local models: whether Ollama and the model are ready, and downloading with progress."""

import json

import httpx
import pytest

from arcee import local_models
from arcee.local_models import LocalStatus, pull, status


def tags(*names: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"models": [{"name": n} for n in names]})
    )


async def test_ready_needs_ollama_running_and_the_base_model() -> None:
    ready = await status(transport=tags("lfm3-omni:latest", "qwen3.5:2b"))
    bare = await status(transport=tags("qwen3.5:2b"))

    assert ready.ready
    assert ready.has("lfm3-omni")  # Ollama names it lfm3-omni:latest
    assert not bare.ready
    assert bare.missing == ("lfm3-omni",)


async def test_no_server_means_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_models.shutil, "which", lambda _name: None)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    found = await status(transport=httpx.MockTransport(refuse))

    assert found == LocalStatus(installed=False, running=False)
    assert not found.ready


async def test_a_download_reports_its_progress() -> None:
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling 9f8d30e9d4fd", "total": 200, "completed": 50},
        {"status": "pulling 9f8d30e9d4fd", "total": 200, "completed": 200},
        {"status": "success"},
    ]
    body = "\n".join(json.dumps(line) for line in lines)
    seen: list[tuple[str, float | None]] = []

    await pull(
        "lfm3-omni",
        lambda what, fraction: seen.append((what, fraction)),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=body)),
    )

    assert seen == [
        ("pulling manifest", None),
        ("pulling 9f8d30e9d4fd", 0.25),
        ("pulling 9f8d30e9d4fd", 1.0),
        ("success", None),
    ]


async def test_a_failed_download_says_why() -> None:
    body = json.dumps({"error": "pull model manifest: file does not exist"})
    with pytest.raises(RuntimeError, match="could not download nope: pull model manifest"):
        await pull(
            "nope",
            lambda *_: None,
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=body)),
        )
