from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import settings

SHA_RE = re.compile(r"sha256-([a-f0-9]{64})")
IS_DARWIN = platform.system() == "Darwin"


def proc_path(*parts: str) -> Path:
    root = Path(settings.proc_root or "/proc")
    return root.joinpath(*parts)


@dataclass
class ProcSample:
    pid: int
    name: str
    cmdline: str
    cpu_pct: float  # 0–100 of whole host
    rss_bytes: int
    blob_sha: str | None
    port: int | None
    cpu_raw: float = 0.0  # multicore % (can be >100)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _parse_port_from_cmdline(cmd: str) -> int | None:
    m = re.search(r"--port\s+(\d+)", cmd)
    return int(m.group(1)) if m else None


def _blob_sha_from_cmdline(cmd: str) -> str | None:
    m = SHA_RE.search(cmd)
    return m.group(1) if m else None


class CpuTracker:
    """Process CPU% — Linux /proc or macOS ps."""

    def __init__(self) -> None:
        self._prev: dict[int, tuple[float, float]] = {}
        self.n_cpus = max(1, os.cpu_count() or 1)
        self._clk = 100
        if not IS_DARWIN:
            try:
                self._clk = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            except (ValueError, AttributeError, OSError):
                self._clk = 100

    def sample(self, pid: int) -> tuple[float, float, int]:
        if IS_DARWIN:
            return self._sample_darwin(pid)
        return self._sample_linux(pid)

    def _sample_darwin(self, pid: int) -> tuple[float, float, int]:
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "%cpu=,rss="],
                text=True,
                timeout=0.5,
            ).strip()
        except (subprocess.SubprocessError, OSError):
            return 0.0, 0.0, 0
        if not out:
            return 0.0, 0.0, 0
        parts = out.split()
        try:
            raw = float(parts[0])
            rss = int(float(parts[1])) * 1024  # rss in KB on macOS
        except (ValueError, IndexError):
            return 0.0, 0.0, 0
        norm = min(100.0, round(raw / self.n_cpus, 1))
        return norm, round(raw, 1), rss

    def _sample_linux(self, pid: int) -> tuple[float, float, int]:
        stat = _read_text(proc_path(str(pid), "stat"))
        status = _read_text(proc_path(str(pid), "status"))
        if not stat:
            self._prev.pop(pid, None)
            return 0.0, 0.0, 0

        try:
            rparen = stat.rfind(")")
            fields = stat[rparen + 2 :].split()
            utime = float(fields[11])
            stime = float(fields[12])
            total = (utime + stime) / self._clk
        except (IndexError, ValueError):
            return 0.0, 0.0, 0

        rss = 0
        if status:
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    try:
                        rss = int(line.split()[1]) * 1024
                    except (IndexError, ValueError):
                        rss = 0
                    break

        now = time.perf_counter()
        prev = self._prev.get(pid)
        self._prev[pid] = (now, total)
        if not prev:
            return 0.0, 0.0, rss
        dt = now - prev[0]
        if dt <= 0:
            return 0.0, 0.0, rss
        raw = max(0.0, (total - prev[1]) / dt * 100.0)
        norm = min(100.0, round(raw / self.n_cpus, 1))
        return norm, round(raw, 1), rss


def list_llama_runners(cpu: CpuTracker) -> list[ProcSample]:
    if IS_DARWIN:
        return _list_llama_runners_darwin(cpu)
    return _list_llama_runners_linux(cpu)


def _list_llama_runners_linux(cpu: CpuTracker) -> list[ProcSample]:
    runners: list[ProcSample] = []
    proc = Path(settings.proc_root or "/proc")
    if not proc.exists():
        return runners
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline_raw = _read_text(entry / "cmdline")
        if not cmdline_raw:
            continue
        cmd = cmdline_raw.replace("\x00", " ").strip()
        if "llama-server" not in cmd:
            continue
        name = "llama-server"
        comm = _read_text(entry / "comm")
        if comm:
            name = comm.strip()
        cpu_pct, cpu_raw, rss = cpu.sample(pid)
        runners.append(
            ProcSample(
                pid=pid,
                name=name,
                cmdline=cmd[:300],
                cpu_pct=cpu_pct,
                rss_bytes=rss,
                blob_sha=_blob_sha_from_cmdline(cmd),
                port=_parse_port_from_cmdline(cmd),
                cpu_raw=cpu_raw,
            )
        )
    return runners


def _list_llama_runners_darwin(cpu: CpuTracker) -> list[ProcSample]:
    runners: list[ProcSample] = []
    try:
        out = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=,rss=,command="],
            text=True,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return runners
    for line in out.splitlines():
        line = line.strip()
        if "llama-server" not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(float(parts[1]))
        except ValueError:
            continue
        cmd = parts[2]
        cpu_pct, cpu_raw, _ = cpu.sample(pid)
        runners.append(
            ProcSample(
                pid=pid,
                name="llama-server",
                cmdline=cmd[:300],
                cpu_pct=cpu_pct,
                rss_bytes=rss_kb * 1024,
                blob_sha=_blob_sha_from_cmdline(cmd),
                port=_parse_port_from_cmdline(cmd),
                cpu_raw=cpu_raw,
            )
        )
    return runners


def list_clients_on_port(port: int = 11434) -> list[dict[str, Any]]:
    if IS_DARWIN:
        return _list_clients_darwin(port)
    return _list_clients_linux(port)


def _list_clients_darwin(port: int) -> list[dict[str, Any]]:
    clients: list[dict[str, Any]] = []
    seen: set[int] = set()
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
            text=True,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return clients
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        comm = parts[0]
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        cmdline = ""
        try:
            cmdline = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="],
                text=True,
                timeout=0.5,
            ).strip()
        except (subprocess.SubprocessError, OSError):
            cmdline = comm
        agent = guess_agent_from_process(comm, cmdline)
        if agent == "ollama-serve" or "ollama serve" in cmdline:
            continue
        clients.append(
            {
                "pid": pid,
                "comm": comm,
                "cmdline": cmdline[:200],
                "agent": agent,
            }
        )
    return clients


def _list_clients_linux(port: int) -> list[dict[str, Any]]:
    targets = {port}
    inodes: set[str] = set()

    for netfile in (proc_path("net", "tcp"), proc_path("net", "tcp6")):
        text = _read_text(netfile)
        if not text:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            local = parts[1]
            rem = parts[2]
            inode = parts[9]
            try:
                local_port = int(local.rsplit(":", 1)[1], 16)
                rem_port = int(rem.rsplit(":", 1)[1], 16)
            except ValueError:
                continue
            if local_port in targets or rem_port in targets:
                if inode != "0":
                    inodes.add(inode)

    if not inodes:
        return []

    clients: list[dict[str, Any]] = []
    seen_pids: set[int] = set()
    proc_root = Path(settings.proc_root or "/proc")
    if not proc_root.exists():
        return []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        fd_dir = entry / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if not target.startswith("socket:["):
                    continue
                inode = target[8:-1]
                if inode not in inodes:
                    continue
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)
                cmdline = (_read_text(entry / "cmdline") or "").replace("\x00", " ").strip()
                comm = (_read_text(entry / "comm") or "").strip() or "unknown"
                agent = guess_agent_from_process(comm, cmdline)
                if agent == "ollama-serve" or "ollama serve" in cmdline:
                    continue
                clients.append(
                    {
                        "pid": pid,
                        "comm": comm,
                        "cmdline": cmdline[:200],
                        "agent": agent,
                    }
                )
                break
        except OSError:
            continue
    return clients


def guess_agent_from_process(comm: str, cmdline: str) -> str:
    blob = f"{comm} {cmdline}".lower()
    rules = [
        ("opencode", "opencode"),
        ("cursor", "cursor"),
        ("continue", "continue"),
        ("open-webui", "open-webui"),
        ("openwebui", "open-webui"),
        ("aider", "aider"),
        ("claude", "claude"),
        ("codex", "codex"),
        ("llmperf", "llmperf"),
        ("uvicorn", "llmperf"),
        ("python", "python"),
        ("node", "node"),
        ("ollama", "ollama-cli"),
    ]
    for needle, label in rules:
        if needle in blob:
            return label
    return comm or "unknown"


def parse_expires_at(value: str | None) -> float | None:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        if "." in cleaned:
            head, rest = cleaned.split(".", 1)
            frac = ""
            tz = ""
            for i, ch in enumerate(rest):
                if ch.isdigit():
                    frac += ch
                else:
                    tz = rest[i:]
                    break
            frac = (frac + "000000")[:6]
            cleaned = f"{head}.{frac}{tz}"
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return None


def build_digest_index(models: list[dict[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for m in models:
        name = m.get("name") or m.get("model") or ""
        digest = (m.get("digest") or "").replace("sha256:", "")
        if digest and name:
            index[digest] = name
    return index


def blob_sha_from_modelfile(modelfile: str) -> str | None:
    m = SHA_RE.search(modelfile or "")
    return m.group(1) if m else None


_VENDOR = {
    "0x1002": "AMD",
    "0x10de": "NVIDIA",
    "0x8086": "Intel",
}


def sample_gpus() -> list[dict[str, Any]]:
    if IS_DARWIN:
        return _sample_apple_gpu()
    gpus = _sample_nvidia()
    if gpus:
        return gpus
    return _sample_drm_gpus()


def _sample_apple_gpu() -> list[dict[str, Any]]:
    """macOS: chip bilgisini göster (Metal util için root gerekir)."""
    name = "Apple Silicon"
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            text=True,
            timeout=1.0,
        ).strip()
        if out:
            name = out
    except (subprocess.SubprocessError, OSError):
        pass
    return [
        {
            "index": 0,
            "name": name,
            "vendor": "Apple",
            "util_pct": 0.0,
            "mem_util_pct": None,
            "mem_used": None,
            "mem_total": None,
            "source": "darwin",
        }
    ]


def _sample_nvidia() -> list[dict[str, Any]]:
    import shutil

    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=1.5,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    rows: list[dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            mem_used = float(parts[4]) * 1024 * 1024
            mem_total = float(parts[5]) * 1024 * 1024
            rows.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "vendor": "NVIDIA",
                    "util_pct": float(parts[2]),
                    "mem_util_pct": float(parts[3]),
                    "mem_used": int(mem_used),
                    "mem_total": int(mem_total),
                    "source": "nvidia-smi",
                }
            )
        except ValueError:
            continue
    return rows


def _sample_drm_gpus() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return rows
    seen: set[str] = set()
    for card in sorted(drm.glob("card[0-9]")):
        if "-" in card.name:
            continue
        device = card / "device"
        busy_path = device / "gpu_busy_percent"
        if not busy_path.exists():
            continue
        key = str(device.resolve()) if device.exists() else str(card)
        if key in seen:
            continue
        seen.add(key)
        busy = _read_text(busy_path)
        vendor_id = (_read_text(device / "vendor") or "").strip().lower()
        used = _read_text(device / "mem_info_vram_used")
        total = _read_text(device / "mem_info_vram_total")
        try:
            util = float(busy.strip()) if busy else 0.0
        except ValueError:
            util = 0.0
        mem_used = int(used.strip()) if used and used.strip().isdigit() else None
        mem_total = int(total.strip()) if total and total.strip().isdigit() else None
        mem_util = None
        if mem_used is not None and mem_total:
            mem_util = round(mem_used / mem_total * 100, 1)
        rows.append(
            {
                "index": int(card.name.replace("card", "")),
                "name": f"{_VENDOR.get(vendor_id, 'GPU')} {card.name}",
                "vendor": _VENDOR.get(vendor_id, vendor_id or "unknown"),
                "util_pct": min(100.0, max(0.0, util)),
                "mem_util_pct": mem_util,
                "mem_used": mem_used,
                "mem_total": mem_total,
                "source": "sysfs",
            }
        )
    rows.sort(key=lambda g: (0 if g.get("mem_total") else 1, g.get("index", 0)))
    return rows
