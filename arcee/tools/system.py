"""System and network facts (spec section 11, `network.status`; `system.status`).

The model was handed raw `ip addr` output and picked the loopback address, and
reached for `df` when asked about RAM. These tools return the answer itself:
the primary local IP from the route the machine would use, memory and disk in
plain units. Everything is read-only, so both run without asking.
"""

import asyncio
import json
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any, Final

from arcee.tools.base import NoArgs, Tool, ToolContext, ToolResult, Usage

# Any public address works: `ip route get` only asks which route would be used.
PROBE_ADDRESS: Final = "1.1.1.1"


def _gb(n_bytes: float) -> str:
    return f"{n_bytes / 1024**3:.1f} GB"


# --- system.status --------------------------------------------------------------


class SystemStatus(Tool[NoArgs]):
    name = "system.status"
    description = (
        "This computer's state: memory (RAM) used and free, disk space used and free for / "
        "and home, CPU count and load, uptime, kernel and OS version."
    )
    usage = Usage(
        when="Questions about RAM, free disk space, CPU load, uptime, or the OS or kernel version.",
        avoid="The size of one folder (fs.list gives total_size) or running programs "
        "(shell.run with ps).",
        mistakes="Using disk numbers to answer a memory question, or the reverse.",
        example="system.status()",
    )
    args_model = NoArgs
    timeout_s = 5

    def __init__(self, *, proc: Path = Path("/proc")) -> None:
        self._proc = proc

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        data = await asyncio.to_thread(self._collect, ctx.home)
        memory, root = data["memory"], data["disk"]["/"]
        summary = (
            f"RAM {memory['available']} free of {memory['total']}; "
            f"disk {root['free']} free of {root['total']}"
        )
        return ToolResult.ok(summary, data=data)

    def _collect(self, home: Path) -> dict[str, Any]:
        meminfo = _meminfo(self._proc / "meminfo")
        total, available = meminfo.get("MemTotal", 0), meminfo.get("MemAvailable", 0)
        disks = {"/": _disk(Path("/")), "~": _disk(home)}
        load = os.getloadavg()
        os_release = platform.freedesktop_os_release() if Path("/etc/os-release").exists() else {}
        return {
            "memory": {
                "total": _gb(total),
                "used": _gb(total - available),
                "available": _gb(available),
                "used_percent": round(100 * (total - available) / total) if total else None,
            },
            "disk": disks,
            "cpu": {"cores": os.cpu_count(), "load_1_5_15_min": [round(x, 2) for x in load]},
            "uptime": _uptime(self._proc / "uptime"),
            "os": os_release.get("PRETTY_NAME", platform.system()),
            "kernel": platform.release(),
        }


def _meminfo(path: Path) -> dict[str, int]:
    """Bytes per /proc/meminfo field."""
    values: dict[str, int] = {}
    for line in path.read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0]) * (1024 if parts[1:] == ["kB"] else 1)
    return values


def _disk(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    return {
        "total": _gb(usage.total),
        "used": _gb(usage.used),
        "free": _gb(usage.free),
        "used_percent": round(100 * usage.used / usage.total) if usage.total else None,
    }


def _uptime(path: Path) -> str:
    seconds = int(float(path.read_text().split()[0]))
    days, rest = divmod(seconds, 86_400)
    return (f"{days} days, " if days else "") + time.strftime("%H:%M", time.gmtime(rest))


# --- network.status -------------------------------------------------------------


class NetworkStatus(Tool[NoArgs]):
    name = "network.status"
    description = (
        "Network facts: the computer's main local IP address and the interface it uses, the "
        "gateway, whether it is online, and every interface's IPv4 addresses."
    )
    usage = Usage(
        when="Questions about this computer's IP address, which network it uses, or "
        "whether it is connected.",
        avoid="Questions about other computers or websites.",
        mistakes="Answering with 127.0.0.1 (loopback) or a docker/virbr bridge address "
        "instead of `local_ip`.",
        example="network.status()",
    )
    args_model = NoArgs
    timeout_s = 5

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        route = await _ip_json("route", "get", PROBE_ADDRESS)
        addresses = await _ip_json("-4", "addr", "show")
        interfaces = {
            str(link.get("ifname")): [str(a["local"]) for a in link.get("addr_info", [])]
            for link in addresses
            if link.get("addr_info")
        }
        main = route[0] if route else {}
        data: dict[str, Any] = {
            "online": bool(main.get("gateway") or main.get("dev")),
            "local_ip": main.get("prefsrc"),
            "interface": main.get("dev"),
            "gateway": main.get("gateway"),
            "all_interfaces": interfaces,
        }
        if data["local_ip"]:
            return ToolResult.ok(f"Local IP {data['local_ip']} on {data['interface']}", data=data)
        return ToolResult.ok("No route to the internet; this computer looks offline", data=data)


async def _ip_json(*args: str) -> list[dict[str, Any]]:
    proc = await asyncio.create_subprocess_exec(
        "ip",
        "-j",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0 or not out.strip():
        return []
    return json.loads(out)
