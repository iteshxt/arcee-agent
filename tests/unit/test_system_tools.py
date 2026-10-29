from pathlib import Path
from typing import Any

import pytest

import arcee.tools.system as system
from arcee.tools.base import NoArgs, ToolContext
from arcee.tools.system import NetworkStatus, SystemStatus


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(task_id="t_1", home=tmp_path)


async def test_system_status_reads_memory_and_uptime(tmp_path: Path, ctx: ToolContext) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text(
        "MemTotal:       16777216 kB\nMemFree:         1048576 kB\nMemAvailable:    8388608 kB\n"
    )
    (proc / "uptime").write_text("183600.52 1000.0\n")

    result = await SystemStatus(proc=proc).run(NoArgs(), ctx)

    assert result.data is not None
    assert result.data["memory"] == {
        "total": "16.0 GB",
        "used": "8.0 GB",
        "available": "8.0 GB",
        "used_percent": 50,
    }
    assert result.data["uptime"] == "2 days, 03:00"
    assert set(result.data["disk"]) == {"/", "~"}
    assert result.summary.startswith("RAM 8.0 GB free of 16.0 GB; disk ")


async def test_network_status_picks_the_routed_address(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers: dict[tuple[str, ...], list[dict[str, Any]]] = {
        ("route", "get", "1.1.1.1"): [
            {"dst": "1.1.1.1", "gateway": "192.168.0.1", "dev": "wlan0", "prefsrc": "192.168.0.7"}
        ],
        ("-4", "addr", "show"): [
            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
            {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1"}]},
            {"ifname": "wlan0", "addr_info": [{"local": "192.168.0.7"}]},
            {"ifname": "veth1", "addr_info": []},
        ],
    }

    async def fake_ip(*args: str) -> list[dict[str, Any]]:
        return answers[args]

    monkeypatch.setattr(system, "_ip_json", fake_ip)

    result = await NetworkStatus().run(NoArgs(), ctx)

    assert result.data == {
        "online": True,
        "local_ip": "192.168.0.7",
        "interface": "wlan0",
        "gateway": "192.168.0.1",
        "all_interfaces": {
            "lo": ["127.0.0.1"],
            "docker0": ["172.17.0.1"],
            "wlan0": ["192.168.0.7"],
        },
    }
    assert result.summary == "Local IP 192.168.0.7 on wlan0"


async def test_network_status_offline(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing(*args: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(system, "_ip_json", nothing)

    result = await NetworkStatus().run(NoArgs(), ctx)

    assert result.data is not None
    assert (result.data["online"], result.data["local_ip"]) == (False, None)
    assert "offline" in result.summary
