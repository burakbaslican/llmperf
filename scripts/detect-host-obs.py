#!/usr/bin/env python3
"""Host gözlemi (macOS / Linux): Ollama TCP istemcileri + runner süreçleri → JSON stdout.

Docker Desktop Mac container host /proc göremez; install script bu çıktıyı
.llmperf-host-obs dosyasına yazar ve volume ile bağlar.

Not: macOS lsof NAME alanını böler; son token çoğu zaman (ESTABLISHED) olur.
İstemci eşlemesi bu yüzden satırın tamamında `->…:port` aranarak yapılır.

Uzak istemciler (LAN): yalnızca ollama tarafı görünür → peer IP ile kaydedilir.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

OLLAMA_PORT = int(os.environ.get("OLLAMA_API_PORT", "11434"))
SHA_RE = re.compile(r"sha256-([a-f0-9]{64})")
PORT_RE = re.compile(r"--port(?:=|\s+)(\d+)")
MODEL_RE = re.compile(r"--model(?:=|\s+)(\S+)")
# Yerel istemci → ollama:11434 (IPv4 / IPv6 / *)
CLIENT_TO_PORT_RE = re.compile(
    rf"->(?:\[[^\]]+\]|[\d.]+|\*):{OLLAMA_PORT}(?:\s|$|\))"
)
# Ollama sunucu tarafı: :11434 → peer (uzak uygulama)
SERVER_PEER_RE = re.compile(
    rf"(?:\[[^\]]+\]|[\d.]+|\*):{OLLAMA_PORT}->(\[?[\w.:]+\]?):(\d+)"
)
MODULE_RE = re.compile(r"(?:^|\s)-m\s+([A-Za-z_][\w.]*)")
SCRIPT_RE = re.compile(r"(?:^|[\s/])([\w.-]+)\.py\b")
CLIENT_CACHE_TTL_SEC = float(os.environ.get("LLMPERF_CLIENT_TTL_SEC", "20"))


def _run(cmd: list[str], timeout: float = 2.0) -> str:
    try:
        return subprocess.check_output(cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return ""


def _strip_ip(raw: str) -> str:
    s = (raw or "").strip().strip("[]")
    if s.startswith("::ffff:"):
        s = s[7:]
    return s


def _is_loopback(ip: str) -> bool:
    ip = _strip_ip(ip).lower()
    return ip in {"127.0.0.1", "::1", "0:0:0:0:0:0:0:1", "localhost"} or ip.startswith("127.")


def guess_agent(comm: str, cmdline: str) -> str:
    blob = f"{comm} {cmdline}"
    low = blob.lower()

    # python -m dhm_analiz → dhm_analiz
    m = MODULE_RE.search(cmdline or "")
    if m:
        mod = m.group(1)
        base = mod.split(".")[0]
        if base.lower() not in {"pip", "venv", "ensurepip", "http", "uvicorn", "gunicorn"}:
            return base

    # script.py
    sm = SCRIPT_RE.search(cmdline or "")
    if sm:
        name = sm.group(1)
        if name.lower() not in {"site", "runpy", "pytest"}:
            return name

    rules = [
        ("opencode", "opencode"),
        ("cursor helper", "cursor"),
        ("cursor", "cursor"),
        ("continue.dev", "continue"),
        ("continue", "continue"),
        ("open-webui", "open-webui"),
        ("openwebui", "open-webui"),
        ("aider", "aider"),
        ("claude", "claude"),
        ("codex", "codex"),
        ("dhm_analiz", "dhm_analiz"),
        ("dhm_", "dhm"),
        ("dhmi", "dhmi"),
        ("sqlcl", "sqlcl"),
        ("langchain", "langchain"),
        ("llmperf", "llmperf"),
        ("uvicorn", "llmperf"),
        ("python", "python"),
        ("node", "node"),
        ("java", "java"),
        ("ollama", "ollama-cli"),
    ]
    for needle, label in rules:
        if needle in low:
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


def _client_cache_path() -> Path:
    env = (os.environ.get("LLMPERF_CLIENT_CACHE") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / ".llmperf-clients-cache.json"


def _load_client_cache() -> dict[str, dict]:
    path = _client_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_client_cache(cache: dict[str, dict]) -> None:
    path = _client_cache_path()
    try:
        # inplace truncate for Docker bind safety if this file is ever mounted
        text = json.dumps(cache, ensure_ascii=False)
        if path.exists():
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
                f.write("\n")
        else:
            path.write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass


def collect_clients() -> list[dict]:
    now = time.time()
    cache = _load_client_cache()
    # drop expired
    cache = {
        k: v
        for k, v in cache.items()
        if isinstance(v, dict) and float(v.get("expires_at") or 0) > now
    }

    # ESTABLISHED + tüm durumlar (kısa istekler CLOSE_WAIT vb.)
    out = _run(["lsof", "-nP", f"-iTCP:{OLLAMA_PORT}"])
    if not out:
        out = _run(["lsof", "-nP", f"-i:{OLLAMA_PORT}"])
    seen_local: set[int] = set()
    seen_peer: set[str] = set()

    if out:
        for line in out.splitlines()[1:]:
            upper = line.upper()
            if "(LISTEN)" in upper:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            comm = parts[0]
            try:
                pid = int(parts[1])
            except ValueError:
                continue

            # Yerel istemci süreci (bağlantı yönü → :11434)
            if CLIENT_TO_PORT_RE.search(line):
                if pid in seen_local:
                    continue
                # ollama'nın kendi sunucu satırını ele
                if comm.lower().startswith("ollama") and f":{OLLAMA_PORT}->" in line.replace(" ", ""):
                    # sunucu tarafı ayrı işlenir
                    pass
                else:
                    seen_local.add(pid)
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
                    entry = {
                        "pid": pid,
                        "comm": comm,
                        "cmdline": cmdline[:200],
                        "agent": agent,
                        "cpu": round(cpu, 1),
                        "model": None,
                        "peer": "local",
                        "role": "client",
                    }
                    cache[f"pid:{pid}"] = {**entry, "expires_at": now + CLIENT_CACHE_TTL_SEC}
                    continue

            # Uzak / diğer makine istemcisi — ollama sunucu satırı
            if comm.lower().startswith("ollama") and "->" in line:
                m = SERVER_PEER_RE.search(line)
                if not m:
                    continue
                peer_ip = _strip_ip(m.group(1))
                if _is_loopback(peer_ip):
                    continue
                if peer_ip in seen_peer:
                    continue
                # CLOSED eski kalıntıları TTL cache ile sınırlı tut; ESTABLISHED öncelikli
                is_live = "ESTABLISHED" in upper
                if peer_ip in seen_peer:
                    continue
                seen_peer.add(peer_ip)
                agent = f"remote:{peer_ip}"
                entry = {
                    "pid": None,
                    "comm": "remote",
                    "cmdline": f"tcp://{peer_ip} → :{OLLAMA_PORT}",
                    "agent": agent,
                    "cpu": 0.0,
                    "model": None,
                    "peer": peer_ip,
                    "role": "remote-client",
                }
                ttl = CLIENT_CACHE_TTL_SEC if is_live else min(8.0, CLIENT_CACHE_TTL_SEC)
                cache[f"peer:{peer_ip}"] = {**entry, "expires_at": now + ttl}

    _save_client_cache(cache)

    clients: list[dict] = []
    for key, v in cache.items():
        row = {k: val for k, val in v.items() if k != "expires_at"}
        clients.append(row)
    clients.sort(key=lambda c: (0 if c.get("role") == "client" else 1, -(c.get("cpu") or 0)))
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
        model_m = MODEL_RE.search(cmd)
        model = model_m.group(1).strip("\"'") if model_m else None
        runners.append(
            {
                "pid": pid,
                "rss_bytes": rss_kb * 1024,
                "cpu_raw": round(cpu, 1),
                "cpu_pct": round(min(100.0, cpu / max(1, os.cpu_count() or 1)), 1),
                "port": int(port_m.group(1)) if port_m else None,
                "blob_sha": sha_m.group(1) if sha_m else None,
                "model": model,
                "cmdline": cmd[:300],
            }
        )
    return runners


def load_gpu_cache() -> list[dict]:
    path = (os.environ.get("LLMPERF_GPU_CACHE") or "").strip()
    if not path:
        cand = Path(__file__).resolve().parent.parent / ".llmperf-gpu.json"
        path = str(cand) if cand.is_file() else ""
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("gpus") or []
    if not isinstance(data, list):
        return []
    return [g for g in data if isinstance(g, dict)]


def main() -> int:
    payload = {
        "clients": collect_clients(),
        "runners": collect_runners(),
        "gpus": load_gpu_cache(),
        "ollama_port": OLLAMA_PORT,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
