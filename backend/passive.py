from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from .config import settings
from .metrics import MetricsStore, RunMetrics
from .observer import (
    CpuTracker,
    ProcSample,
    blob_sha_from_modelfile,
    build_digest_index,
    list_clients_on_port,
    list_llama_runners,
    load_host_obs,
    parse_expires_at,
    sample_gpus,
)
from .ollama_client import ollama


class _SlotTrack:
    __slots__ = (
        "processing",
        "last_decoded",
        "last_ts",
        "start_ts",
        "start_decoded",
        "peak_decoded",
        "inst_tps",
        "avg_tps",
        "prompt_tokens",
        "task_id",
        "model",
        "agent",
    )

    def __init__(self) -> None:
        self.processing = False
        self.last_decoded = 0
        self.last_ts = 0.0
        self.start_ts = 0.0
        self.start_decoded = 0
        self.peak_decoded = 0
        self.inst_tps: float | None = None
        self.avg_tps: float | None = None
        self.prompt_tokens = 0
        self.task_id: Any = None
        self.model = "unknown"
        self.agent = "external"


NOISE_AGENTS = frozenset({"llmperf", "uvicorn", "unknown", "?", "ollama-serve", "ollama-runner"})
GENERIC_AGENTS = frozenset({"python", "node", "java", "external", "remote"})
CLIENT_TTL_SEC = 20.0


class PassiveObserver:
    """Watch Ollama without proxying: /api/ps + llama-server /slots + TCP clients."""

    def __init__(self, store: MetricsStore) -> None:
        self.store = store
        self.cpu = CpuTracker()
        self._ollama_port = self._port_from_url(settings.ollama_base_url)
        self._slots_host = self._slots_host_from_settings()
        self._blob_to_model: dict[str, str] = {}
        self._blob_cache_ts = 0.0
        self._last_inf: dict[str, float] = {}
        self._slots: dict[int, _SlotTrack] = {}  # port -> track
        self._known_slot_ports: set[int] = set(self._parse_slots_ports())
        self._last_slots_scan = 0.0
        self._scan_lock = asyncio.Lock()
        self._model_pulse_until: dict[str, float] = {}
        self._client_ttl: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _port_from_url(url: str) -> int:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.port or 11434

    @staticmethod
    def _slots_host_from_settings() -> str:
        from urllib.parse import urlparse

        if settings.slots_host:
            return settings.slots_host
        host = urlparse(settings.ollama_base_url).hostname
        return host or "127.0.0.1"

    def _parse_slots_ports(self) -> list[int]:
        ports: list[int] = []
        raw_parts: list[str] = []
        if settings.slots_ports:
            raw_parts.extend(settings.slots_ports.split(","))
        path = (settings.slots_ports_file or "").strip()
        if path:
            try:
                text = open(path, encoding="utf-8").read()  # noqa: SIM115
                raw_parts.extend(text.replace("\n", ",").split(","))
            except OSError:
                pass
        for part in raw_parts:
            part = part.strip()
            if not part:
                continue
            try:
                port = int(part)
            except ValueError:
                continue
            # Ollama API portu /slots değildir
            if port == self._ollama_port or port in (80, 443, 8080):
                continue
            ports.append(port)
        # unique preserve order
        seen: set[int] = set()
        out: list[int] = []
        for p in ports:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    async def _probe_slots_port(self, port: int) -> bool:
        """Yalnızca gerçek llama.cpp /slots JSON'u kabul et (sahte 200'leri ele)."""
        slot = await self._fetch_slots(port)
        if not slot:
            return False
        return any(
            k in slot
            for k in (
                "is_processing",
                "processing",
                "next_token",
                "n_decoded",
                "n_prompt_tokens",
                "id_task",
                "id_slot",
            )
        )

    @staticmethod
    def _is_embedding_model(name: str) -> bool:
        low = (name or "").lower()
        needles = (
            "embed",
            "bge-",
            "e5-",
            "gte-",
            "minilm",
            "nomic-embed",
            "mxbai-embed",
            "snowflake-arctic-embed",
        )
        return any(n in low for n in needles)

    def _generative_running(self, running: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in running:
            name = m.get("name") or m.get("model") or ""
            if name and not self._is_embedding_model(name):
                out.append(m)
        return out or list(running)

    async def _refresh_known_slot_ports(self) -> None:
        """Docker Desktop Mac: /proc'ta runner yokken host /slots portlarını bul."""
        configured = set(self._parse_slots_ports())
        to_check = set(self._known_slot_ports) | configured

        alive: set[int] = set()
        if to_check:
            results = await asyncio.gather(
                *[self._probe_slots_port(p) for p in to_check]
            )
            for port, ok in zip(to_check, results):
                if ok:
                    alive.add(port)
        # Probe başarısız portları tutma (sahte 200 / ölü port)
        self._known_slot_ports = alive

        if not settings.slots_discover:
            return

        now = time.time()
        if self._known_slot_ports and now - self._last_slots_scan < 30:
            return
        if now - self._last_slots_scan < 8:
            return

        async with self._scan_lock:
            if time.time() - self._last_slots_scan < 8:
                return
            self._last_slots_scan = time.time()
            start = max(1, settings.slots_scan_start)
            end = min(65535, settings.slots_scan_end)
            if end < start:
                return
            sem = asyncio.Semaphore(64)

            async def check(p: int) -> int | None:
                async with sem:
                    return p if await self._probe_slots_port(p) else None

            chunk = 400
            found: set[int] = set()
            for base in range(start, end + 1, chunk):
                batch = list(range(base, min(base + chunk, end + 1)))
                hits = await asyncio.gather(*[check(p) for p in batch])
                for hit in hits:
                    if hit is not None:
                        found.add(hit)
                if found:
                    break
            self._known_slot_ports |= found

    def _synthetic_runners_from_ports(self) -> list[ProcSample]:
        runners: list[ProcSample] = []
        for port in sorted(self._known_slot_ports):
            runners.append(
                ProcSample(
                    pid=0,
                    name="llama-server",
                    cmdline=f"llama-server --port {port}",
                    cpu_pct=0.0,
                    rss_bytes=0,
                    blob_sha=None,
                    port=port,
                    cpu_raw=0.0,
                )
            )
        return runners

    async def refresh_blob_index(self, catalog: list[dict[str, Any]]) -> None:
        now = time.time()
        if self._blob_to_model and now - self._blob_cache_ts < 60:
            return
        index: dict[str, str] = {}
        for m in catalog:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            try:
                info = await ollama.show(name)
            except Exception:  # noqa: BLE001
                continue
            sha = blob_sha_from_modelfile(info.get("modelfile") or "")
            if sha:
                index[sha] = name
        if index:
            self._blob_to_model = index
            self._blob_cache_ts = now

    async def _fetch_slots(self, port: int) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=0.4) as client:
                resp = await client.get(
                    f"http://{self._slots_host}:{port}/slots"
                )
                resp.raise_for_status()
                return self._normalize_slot_payload(resp.json())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_slot_payload(data: Any) -> dict[str, Any] | None:
        """llama.cpp /slots: liste veya tek nesne; sarmal {slots:[...]} olabilir."""
        slot: Any = None
        if isinstance(data, list):
            if not data:
                return None
            slot = data[0]
        elif isinstance(data, dict):
            if isinstance(data.get("slots"), list) and data["slots"]:
                slot = data["slots"][0]
            else:
                slot = data
        return slot if isinstance(slot, dict) else None

    @staticmethod
    def _slot_n_decoded(slot: dict[str, Any]) -> int:
        nt = slot.get("next_token")
        if isinstance(nt, list) and nt:
            nt = nt[0]
        if isinstance(nt, dict):
            try:
                return int(nt.get("n_decoded") or 0)
            except (TypeError, ValueError):
                return 0
        for key in ("n_decoded", "decoded_tokens", "n_tokens_decoded"):
            if key in slot:
                try:
                    return int(slot.get(key) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    @staticmethod
    def _slot_is_processing(slot: dict[str, Any]) -> bool:
        for key in ("is_processing", "processing", "busy"):
            if key in slot:
                return bool(slot.get(key))
        return False

    @staticmethod
    def _slot_prompt_tokens(slot: dict[str, Any]) -> int:
        for key in ("n_prompt_tokens", "prompt_tokens", "n_prompt"):
            if key in slot:
                try:
                    return int(slot.get(key) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    @staticmethod
    def _slot_model_hint(slot: dict[str, Any]) -> str | None:
        for key in ("model", "model_name", "name"):
            val = slot.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        task = slot.get("task")
        if isinstance(task, dict):
            for key in ("model", "model_name"):
                val = task.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return None

    def _assign_models_to_runners(
        self,
        runners: list[ProcSample],
        running: list[dict[str, Any]],
        digest_index: dict[str, str],
        slot_hints: dict[int, str | None],
    ) -> dict[int, str]:
        """port → model. blob_sha yoksa (Mac) /api/ps ile eşle."""
        port_model: dict[int, str] = {}
        used_models: set[str] = set()

        for r in runners:
            if not r.port:
                continue
            cmdline = r.cmdline or ""
            # Önce blob sha → katalog adı (llama-server --model /…/sha256-…)
            sha = r.blob_sha or None
            if not sha:
                sm = re.search(r"sha256-([a-f0-9]{64})", cmdline)
                sha = sm.group(1) if sm else None
            if sha and sha in digest_index:
                port_model[r.port] = digest_index[sha]
                used_models.add(digest_index[sha])
                continue
            # Mac Ollama runner: --model dhmi-fast:latest (dosya yolu değilse)
            m = re.search(r"--model(?:=|\s+)(\S+)", cmdline)
            if m:
                raw = m.group(1).strip("\"'")
                if raw and not raw.startswith("/") and "sha256-" not in raw:
                    port_model[r.port] = raw
                    used_models.add(raw)
                    continue
                sm2 = re.search(r"sha256-([a-f0-9]{64})", raw)
                if sm2 and sm2.group(1) in digest_index:
                    port_model[r.port] = digest_index[sm2.group(1)]
                    used_models.add(digest_index[sm2.group(1)])
                    continue
            name = digest_index.get(r.blob_sha or "", "")
            if name:
                port_model[r.port] = name
                used_models.add(name)
                continue
            hint = slot_hints.get(r.port)
            if hint:
                match = next(
                    (
                        (m.get("name") or m.get("model") or "")
                        for m in running
                        if (m.get("name") or m.get("model") or "") == hint
                        or hint in (m.get("name") or m.get("model") or "")
                    ),
                    hint,
                )
                if match:
                    port_model[r.port] = match
                    used_models.add(match)

        # Eşlemede embedding modellerini sona bırak (bge-* vb.)
        gen = self._generative_running(running)
        pool = gen + [m for m in running if m not in gen]

        remaining_ports = [r.port for r in runners if r.port and r.port not in port_model]
        remaining_models = [
            (m.get("name") or m.get("model") or "")
            for m in pool
            if (m.get("name") or m.get("model") or "")
            and (m.get("name") or m.get("model") or "") not in used_models
        ]
        for port, model in zip(remaining_ports, remaining_models):
            port_model[port] = model
        if (
            len(remaining_ports) == 1
            and len(gen) == 1
            and remaining_ports[0] not in port_model
        ):
            only = gen[0].get("name") or gen[0].get("model")
            if only:
                port_model[remaining_ports[0]] = str(only)
        elif (
            len(remaining_ports) == 1
            and len(running) >= 1
            and remaining_ports[0] not in port_model
        ):
            # Tek port → ilk üretken (yoksa ilk) model
            pick = (gen[0] if gen else running[0])
            only = pick.get("name") or pick.get("model")
            if only:
                port_model[remaining_ports[0]] = str(only)
        return port_model

    def _merge_clients(
        self,
        host: list[dict[str, Any]],
        local: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        now = time.time()
        by_key: dict[str, dict[str, Any]] = {}
        for c in local + host:
            pid = c.get("pid")
            peer = c.get("peer")
            if pid is not None:
                key = f"pid:{pid}"
            elif peer and peer != "local":
                key = f"peer:{peer}"
            else:
                key = f"agent:{c.get('agent')}:{c.get('comm')}"
            prev = by_key.get(key)
            if not prev or float(c.get("cpu") or 0) >= float(prev.get("cpu") or 0):
                by_key[key] = c
            self._client_ttl[key] = (now + CLIENT_TTL_SEC, dict(by_key[key]))

        # TTL: kısa HTTP bağlantıları üretim boyunca görünsün
        alive: dict[str, dict[str, Any]] = {}
        for key, (exp, row) in list(self._client_ttl.items()):
            if exp < now:
                self._client_ttl.pop(key, None)
                continue
            fresh = by_key.get(key)
            alive[key] = fresh if fresh is not None else row
        return list(alive.values())

    def _client_candidates(self, clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in clients:
            agent = (c.get("agent") or "").strip()
            if not agent or agent in NOISE_AGENTS:
                continue
            out.append(c)
        return out

    def _enrich_clients_with_models(
        self,
        clients: list[dict[str, Any]],
        observed: list[dict[str, Any]],
        runners: list[ProcSample],
        port_models: dict[int, str],
    ) -> list[dict[str, Any]]:
        """TCP/remote istemcilere işlem yapılan model adlarını bağla."""
        active_models: list[str] = []
        for o in observed:
            model = (o.get("model") or "").strip()
            if not model or model == "unknown":
                continue
            if model.startswith("/") or ("sha256-" in model and "/" in model):
                continue
            if o.get("inferring") or o.get("status") in {"busy", "generating"}:
                if model not in active_models:
                    active_models.append(model)

        model_label = ", ".join(active_models) if active_models else None
        out: list[dict[str, Any]] = []
        for c in clients:
            row = dict(c)
            if not row.get("model") and model_label:
                row["model"] = model_label
            out.append(row)

        have_models = any((c.get("model") or "").strip() for c in out)
        if out and have_models and not active_models:
            return out

        seen_models = {
            (c.get("model") or "").strip()
            for c in out
            if (c.get("model") or "").strip() and "," not in str(c.get("model"))
        }
        for r in runners:
            model = ""
            if r.port and r.port in port_models:
                model = port_models[r.port]
            if not model:
                m = re.search(r"--model(?:=|\s+)(\S+)", r.cmdline or "")
                raw = m.group(1).strip("\"'") if m else ""
                if raw and not raw.startswith("/") and "sha256-" not in raw:
                    model = raw
            if not model or model in seen_models:
                continue
            if model.startswith("/") or model.startswith("sha256-"):
                continue
            busy = (
                model in active_models
                or r.cpu_raw >= settings.runner_cpu_active_pct
                or r.cpu_pct >= 3.0
            )
            out.append(
                {
                    "pid": r.pid,
                    "comm": "ollama",
                    "cmdline": (r.cmdline or "")[:200],
                    "agent": "ollama-runner",
                    "cpu": r.cpu_raw,
                    "model": model,
                    "role": "runner",
                    "busy": busy,
                }
            )
            seen_models.add(model)
        return out

    @staticmethod
    def _agent_specificity(agent: str) -> int:
        a = (agent or "").strip()
        if not a or a in NOISE_AGENTS:
            return -10
        if a.startswith("remote:"):
            return 40
        if a in GENERIC_AGENTS:
            return 10
        return 80

    def _select_active_agent(self, clients: list[dict[str, Any]]) -> str:
        """Pick the single client most likely driving the current request.

        Prefers named app modules (dhm_analiz, …) over generic python/node,
        and remote LAN peers over idle dashboard connections.
        """
        candidates = self._client_candidates(clients)
        if not candidates:
            return "external"

        external = [
            c
            for c in candidates
            if (c.get("agent") or "") not in {"llmperf", "uvicorn"}
        ]
        pool = external or candidates

        if len(pool) == 1:
            return str(pool[0].get("agent") or "external")

        scored: list[tuple[float, float, str]] = []
        for c in pool:
            pid = c.get("pid")
            file_cpu = float(c.get("cpu") or 0.0)
            raw = file_cpu
            if isinstance(pid, int) and pid > 0 and file_cpu <= 0:
                _norm, raw, _rss = self.cpu.sample(pid)
            cmdline = (c.get("cmdline") or "").lower()
            is_service = " serve" in f" {cmdline}" or cmdline.rstrip().endswith("serve")
            agent = str(c.get("agent") or "external")
            score = raw + (0.0 if is_service else 40.0) + self._agent_specificity(agent)
            if agent in {"llmperf", "uvicorn"}:
                score -= 50.0
            # Aktif remote bağlantı
            if (c.get("role") or "") == "remote-client":
                score += 25.0
            scored.append((score, raw, agent))
        if not scored:
            return str(pool[0].get("agent") or "external")

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][2]

    async def _update_slot_metrics(
        self,
        *,
        port: int,
        model: str,
        clients: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Sample llama-server /slots and derive live tok/s from n_decoded."""
        slot = await self._fetch_slots(port)
        track = self._slots.get(port) or _SlotTrack()
        self._slots[port] = track
        track.model = model

        meta: dict[str, Any] = {
            "live_tps": None,
            "avg_tps": None,
            "tokens": 0,
            "prompt_tokens": 0,
            "slot_processing": False,
            "agent": None,
            "model_hint": None,
        }
        if not slot:
            return meta

        decoded = self._slot_n_decoded(slot)
        processing = self._slot_is_processing(slot)
        prompt_tokens = self._slot_prompt_tokens(slot)
        task_id = slot.get("id_task")
        now = time.time()

        # Boş/idle slot'larda is_processing true gelebilir (Mac/MLX sahte pozitif)
        if processing and decoded <= 0 and prompt_tokens <= 0:
            processing = False

        meta["prompt_tokens"] = prompt_tokens
        meta["slot_processing"] = processing
        meta["model_hint"] = self._slot_model_hint(slot)

        # New task / generation start — freeze agent for this run only
        if processing and (
            not track.processing
            or (task_id is not None and task_id != track.task_id)
            or decoded < track.last_decoded
        ):
            track.processing = True
            track.task_id = task_id
            track.start_ts = now
            track.start_decoded = decoded
            track.peak_decoded = decoded
            track.last_decoded = decoded
            track.last_ts = now
            track.inst_tps = None
            track.avg_tps = None
            track.agent = self._select_active_agent(clients)
            self.store.push_activity(
                {
                    "type": "infer",
                    "model": model,
                    "agent": track.agent,
                    "source": "observed",
                    "tokens": decoded,
                }
            )

        if processing:
            dt = now - track.last_ts if track.last_ts else 0
            if dt > 0 and decoded >= track.last_decoded:
                delta = decoded - track.last_decoded
                if delta > 0:
                    track.inst_tps = round(delta / dt, 2)
            elapsed = now - track.start_ts if track.start_ts else 0
            produced = decoded - track.start_decoded
            if elapsed > 0.05 and produced >= 0:
                track.avg_tps = round(produced / elapsed, 2)
            track.peak_decoded = max(track.peak_decoded, decoded)
            track.last_decoded = decoded
            track.last_ts = now
            track.prompt_tokens = prompt_tokens
            meta.update(
                {
                    "live_tps": track.inst_tps or track.avg_tps,
                    "avg_tps": track.avg_tps,
                    "tokens": produced if produced > 0 else decoded,
                    "agent": track.agent,
                }
            )

        # Generation finished
        if track.processing and not processing:
            produced = max(track.peak_decoded - track.start_decoded, track.peak_decoded, 0)
            elapsed = (track.last_ts - track.start_ts) if track.start_ts else 0
            tps = round(produced / elapsed, 2) if elapsed > 0.05 and produced > 0 else track.avg_tps
            if produced > 0 and tps:
                metrics = RunMetrics(
                    model=model,
                    prompt_tokens=track.prompt_tokens,
                    completion_tokens=produced,
                    total_tokens=track.prompt_tokens + produced,
                    completion_tps=tps,
                    wall_ms=round(elapsed * 1000, 2) if elapsed else None,
                    source="observed",
                    agent=track.agent or "external",
                    client="slots",
                    endpoint="/slots",
                )
                self.store.add_observed_run(metrics)
                self.store.push_activity(
                    {
                        "type": "finish",
                        "model": model,
                        "agent": track.agent,
                        "source": "observed",
                        "completion_tps": tps,
                        "completion_tokens": produced,
                    }
                )
            track.processing = False
            track.inst_tps = None
            track.avg_tps = None
            track.last_decoded = 0
            track.peak_decoded = 0
            track.agent = "external"

        track.processing = processing
        if processing:
            meta["agent"] = track.agent
        return meta

    async def tick(self, running: list[dict[str, Any]], catalog: list[dict[str, Any]]) -> None:
        await self.refresh_blob_index(catalog)

        digest_index = build_digest_index(catalog)
        digest_index.update(build_digest_index(running))
        digest_index.update(self._blob_to_model)

        runners = list_llama_runners(self.cpu)
        host_runners, host_clients, host_gpus = load_host_obs()
        # Mac Docker: host dosyasındaki runner'lar öncelikli
        if host_runners:
            runners = host_runners

        need_ports = (
            settings.slots_discover
            or bool(settings.slots_ports)
            or bool(settings.slots_ports_file)
            or bool(self._known_slot_ports)
        )
        if not runners and need_ports:
            await self._refresh_known_slot_ports()
            runners = self._synthetic_runners_from_ports()
        elif runners:
            for r in runners:
                if r.port:
                    self._known_slot_ports.add(r.port)
            if need_ports and not any(r.port for r in runners):
                await self._refresh_known_slot_ports()
                synth = self._synthetic_runners_from_ports()
                if synth:
                    # Host runner CPU bilgisini koru; porta sahip synth ile birleştir
                    by_port = {r.port: r for r in runners if r.port}
                    merged: list[ProcSample] = []
                    for s in synth:
                        base = by_port.get(s.port)
                        if base:
                            merged.append(
                                ProcSample(
                                    pid=base.pid,
                                    name=base.name,
                                    cmdline=base.cmdline,
                                    cpu_pct=base.cpu_pct,
                                    rss_bytes=base.rss_bytes,
                                    blob_sha=base.blob_sha,
                                    port=s.port,
                                    cpu_raw=base.cpu_raw,
                                )
                            )
                        else:
                            merged.append(s)
                    runners = merged or synth

        # İstemciler: host dosyası (Docker Mac) + yerel tarama birleşimi
        local_clients = list_clients_on_port(self._ollama_port)
        # Docker Mac: container içindeki llmperf→host.docker.internal bağlantısı
        # gerçek host uygulamalarını ezmesin
        if settings.host_obs_file:
            local_clients = [
                c
                for c in local_clients
                if (c.get("agent") or "") not in NOISE_AGENTS
                and (c.get("agent") or "") not in GENERIC_AGENTS
            ]
            if host_clients:
                clients = self._merge_clients(host_clients, local_clients)
            else:
                clients = self._merge_clients([], local_clients)
        else:
            clients = self._merge_clients(host_clients, local_clients)
        event_agent = self._select_active_agent(clients)

        # Önce slot'lardan model ipucu topla
        slot_hints: dict[int, str | None] = {}
        for r in runners:
            if not r.port:
                continue
            try:
                slot = await self._fetch_slots(r.port)
                slot_hints[r.port] = self._slot_model_hint(slot) if slot else None
            except Exception:  # noqa: BLE001
                slot_hints[r.port] = None

        port_models = self._assign_models_to_runners(
            runners, running, digest_index, slot_hints
        )

        observed: list[dict[str, Any]] = []
        runner_by_model: dict[str, dict[str, Any]] = {}

        for r in runners:
            model = port_models.get(r.port or -1) or digest_index.get(
                r.blob_sha or "", "unknown"
            )
            slot_meta: dict[str, Any] = {}
            if r.port:
                try:
                    slot_meta = await self._update_slot_metrics(
                        port=r.port, model=model, clients=clients
                    )
                except Exception:  # noqa: BLE001
                    slot_meta = {}
                if model == "unknown" and slot_meta.get("model_hint"):
                    model = str(slot_meta["model_hint"])

            slot_processing = bool(slot_meta.get("slot_processing"))
            has_signal = bool(
                slot_meta.get("live_tps")
                or slot_meta.get("avg_tps")
                or (slot_meta.get("tokens") or 0) > 0
                or (slot_meta.get("prompt_tokens") or 0) > 0
            )
            # MLX / ollama runner: /slots yok → CPU veya GPU ile canlı üretim
            cpu_busy = r.cpu_raw >= settings.runner_cpu_active_pct
            cpu_hot = r.cpu_raw >= settings.runner_cpu_infer_pct
            pulsing = time.time() < self._model_pulse_until.get(model, 0)
            gpu_util = 0.0
            if host_gpus:
                try:
                    gpu_util = max(float(g.get("util_pct") or 0.0) for g in host_gpus)
                except (TypeError, ValueError):
                    gpu_util = 0.0
            gpu_hot = gpu_util >= 35.0
            slots_inferring = slot_processing and has_signal
            # Apple Silicon MLX: CPU düşük kalır, GPU yükselir
            cpu_inferring = (
                not slots_inferring
                and not self._is_embedding_model(model)
                and (
                    cpu_hot
                    or (pulsing and (cpu_busy or gpu_hot))
                    or (gpu_hot and cpu_busy)
                    or (gpu_hot and r.cpu_raw >= 3.0)
                )
            )
            inferring = slots_inferring or cpu_inferring
            busy = (not inferring) and (cpu_busy or (gpu_hot and pulsing))
            if inferring:
                status = "inferring"
            elif busy:
                status = "busy"
            else:
                status = "loaded"

            # Anlamsız synthetic unknown satırını atla — /api/ps resident kartları yeterli
            if (
                (not model or model == "unknown")
                and not inferring
                and not busy
                and (not r.pid or r.pid == 0)
            ):
                continue

            row_agent = None
            if inferring or busy:
                row_agent = slot_meta.get("agent") or event_agent
                if row_agent in {None, "", "external"}:
                    # Hâlâ isimsizse ilk anlamlı istemci ajanını kullan
                    for c in clients:
                        a = (c.get("agent") or "").strip()
                        if a and a not in NOISE_AGENTS:
                            row_agent = a
                            break
                if row_agent == "external":
                    row_agent = event_agent if event_agent != "external" else row_agent

            live_tps = slot_meta.get("live_tps") if slots_inferring else None
            avg_tps = slot_meta.get("avg_tps") if slots_inferring else None
            tokens = (slot_meta.get("tokens") or 0) if slots_inferring else 0

            row = {
                "pid": r.pid if r.pid else None,
                "model": model,
                "blob_sha": (r.blob_sha or "")[:12],
                "cpu_pct": r.cpu_pct,
                "cpu_raw": r.cpu_raw,
                "rss_bytes": r.rss_bytes,
                "port": r.port,
                "inferring": inferring,
                "slot_processing": bool(inferring),
                "status": status,
                "live_tps": live_tps,
                "avg_tps": avg_tps,
                "tokens": tokens,
                "prompt_tokens": slot_meta.get("prompt_tokens") or 0,
                "agent": row_agent,
                "metric_source": (
                    "slots"
                    if slots_inferring
                    else ("gpu" if cpu_inferring and gpu_hot and not cpu_hot else ("cpu" if cpu_inferring else None))
                ),
                "gpu_util_pct": round(gpu_util, 1) if gpu_hot or inferring else None,
            }
            observed.append(row)
            if model and model != "unknown":
                runner_by_model[model] = row

        now = time.time()
        current_names: set[str] = set()
        for m in running:
            name = m.get("name") or m.get("model") or ""
            if not name:
                continue
            current_names.add(name)
            exp_ts = parse_expires_at(m.get("expires_at"))
            prev_exp = self.store._prev_expires.get(name)

            if name not in self.store._prev_running_names:
                self.store.push_activity(
                    {
                        "type": "load",
                        "model": name,
                        "agent": event_agent,
                        "source": "observed",
                    }
                )

            if (
                prev_exp is not None
                and exp_ts is not None
                and exp_ts > prev_exp + settings.expires_skew_sec
            ):
                self._model_pulse_until[name] = now + settings.model_pulse_sec
                self.store.push_activity(
                    {
                        "type": "use",
                        "model": name,
                        "agent": event_agent,
                        "source": "observed",
                        "note": "keep_alive refreshed",
                    }
                )

            if exp_ts is not None:
                self.store._prev_expires[name] = exp_ts

            pulsing = now < self._model_pulse_until.get(name, 0)

            if name not in runner_by_model:
                ttl = round(exp_ts - now, 1) if exp_ts else None
                # MLX / slots yok: expires nabzı veya bağlı dış ajan → busy
                status = "busy" if pulsing else "resident"
                observed.append(
                    {
                        "pid": None,
                        "model": name,
                        "blob_sha": (m.get("digest") or "").replace("sha256:", "")[:12],
                        "cpu_pct": 0.0,
                        "rss_bytes": m.get("size_vram") or m.get("size") or 0,
                        "port": None,
                        "inferring": False,
                        "slot_processing": False,
                        "status": status,
                        "expires_in_sec": ttl,
                        "size_vram": m.get("size_vram"),
                        "live_tps": None,
                        "tokens": 0,
                        "agent": event_agent if pulsing else None,
                    }
                )
            else:
                row = runner_by_model[name]
                row["size_vram"] = m.get("size_vram")
                if exp_ts:
                    row["expires_in_sec"] = round(exp_ts - now, 1)
                # Nabız varken resident/loaded'ı busy yap (slots yoksa)
                if pulsing and row.get("status") in {"resident", "loaded"} and not row.get(
                    "inferring"
                ):
                    row["status"] = "busy"
                    if not row.get("agent"):
                        row["agent"] = event_agent

        for gone in self.store._prev_running_names - current_names:
            self.store.push_activity(
                {
                    "type": "unload",
                    "model": gone,
                    "agent": event_agent,
                    "source": "observed",
                }
            )
            self.store._prev_expires.pop(gone, None)

        self.store._prev_running_names = current_names
        observed.sort(
            key=lambda o: (
                not o.get("inferring"),
                -(o.get("live_tps") or 0),
                -(o.get("cpu_pct") or 0),
            )
        )
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in observed:
            name = row.get("model") or ""
            if row.get("status") == "resident" and name in runner_by_model:
                continue
            key = f"{name}:{row.get('pid')}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        clients = self._enrich_clients_with_models(clients, deduped, runners, port_models)
        gpus = host_gpus or sample_gpus()
        self.store.set_observed(deduped, clients, gpus)
