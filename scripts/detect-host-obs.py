#!/usr/bin/env python3
"""Host gözlemi (macOS / Linux): Ollama TCP istemcileri + runner süreçleri → JSON stdout.

Docker Desktop Mac container host /proc göremez; install script bu çıktıyı
.llmperf-host-obs dosyasına yazar ve volume ile bağlar.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

OLLAMA_PORT = int(os.environ.get("OLLAMA_API_PORT", "11434"))
SHA_RE = re.compile(r"sha256-([a-f0-9]{64})")
PORT_RE = re.compile(r"--port(?:=|\s+)(\d+)")


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        return subprocess.check_output(cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return ""


def guess_agent(comm: str, cmdline: str) -> str:
    blob = f"{comm} {cmdline}".lower()
    rules = [
        ("opencode", "opencode"),
        ("cursor helper", "cursor"),
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
    return (comm or "unknown").strip() or "unknown"


def is_runner_cmd(cmd: str) -> bool:
    low = cmd.lower()
    if not low.strip():
        return False
    if re.search(r"\bollama(\s+|$)serve\b", low) or "ollama app" in low:
        return False
    if "llama-server" in low:
        return True
    if "ollama" in low and ("runner" in low or "/runners/" in low):
        return True
    if "ollama" in low and PORT_RE.search(cmd) and (
        "--model" in low or "gguf" in low or "sha256-" in low
    ):
        return True
    return False


def collect_clients() -> list[dict]:
    clients: list[dict] = []
    seen: set[int] = set()
    # ESTABLISHED + tüm TCP :11434 (Mac'te bazı istemciler kısa yaşar)
    out = _run(["lsof", "-nP", f"-iTCP:{OLLAMA_PORT}"])
    if not out:
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
        # LISTEN satırı (ollama) — istemci değil; yalnızca -> bağlantıları
        name = parts[-1] if parts else ""
        if "(LISTEN)" in line.upper():
            continue
        if "->" not in name:
            continue
        seen.add(pid)
        cmdline = _run(["ps", "-p", str(pid), "-o", "command="], timeout=0.5).strip() or comm
        agent = guess_agent(comm, cmdline)
        if agent in {"ollama-serve", "ollama-cli"} and "serve" in cmdline.lower():
            continue
        if "ollama serve" in cmdline.lower():
            continue
        cpu = 0.0
        cpu_out = _run(["ps", "-p", str(pid), "-o", "%cpu="], timeout=0.5).strip()
        try:
            cpu = float(cpu_out.split()[0]) if cpu_out else 0.0
        except ValueError:
            cpu = 0.0
        clients.append(
            {
                "pid": pid,
                "comm": comm,
                "cmdline": cmdline[:200],
                "agent": agent,
                "cpu": round(cpu, 1),
            }
        )
    return clients


def collect_runners() -> list[dict]:
    runners: list[dict] = []
    out = _run(["ps", "-ax", "-o", "pid=,rss=,%cpu=,command="], timeout=2.0)
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        cmd = parts[3]
        if not is_runner_cmd(cmd):
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(float(parts[1]))
            cpu = float(parts[2])
        except ValueError:
            continue
        port_m = PORT_RE.search(cmd)
        sha_m = SHA_RE.search(cmd)
        runners.append(
            {
                "pid": pid,
                "rss_bytes": rss_kb * 1024,
                "cpu_raw": round(cpu, 1),
                "cpu_pct": round(min(100.0, cpu / max(1, os.cpu_count() or 1)), 1),
                "port": int(port_m.group(1)) if port_m else None,
                "blob_sha": sha_m.group(1) if sha_m else None,
                "cmdline": cmd[:300],
            }
        )
    return runners


def main() -> int:
    payload = {
        "clients": collect_clients(),
        "runners": collect_runners(),
        "ollama_port": OLLAMA_PORT,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
