from __future__ import annotations

import asyncio
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


NOISE_AGENTS = frozenset({"llmperf", "uvicorn", "unknown", "?", "ollama-serve"})


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

    @staticmethod
    def _parse_slots_ports() -> list[int]:
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
        try:
            async with httpx.AsyncClient(timeout=0.2) as client:
                resp = await client.get(f"http://{self._slots_host}:{port}/slots")
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def _refresh_known_slot_ports(self) -> None:
        """Docker Desktop Mac: /proc'ta runner yokken host /slots portlarını bul."""
        configured = set(self._parse_slots_ports())
        self._known_slot_ports |= configured

        # Önce bilinenleri doğrula
        alive: set[int] = set()
        if self._known_slot_ports:
            results = await asyncio.gather(
                *[self._probe_slots_port(p) for p in self._known_slot_ports]
            )
            for port, ok in zip(self._known_slot_ports, results):
                if ok:
                    alive.add(port)
            self._known_slot_ports = alive | configured

        if not settings.slots_discover:
            return

        now = time.time()
        # Tam tarama seyrek; model yüklüyken ve port yokken tetikle
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

            # Parça parça tara (UI'yi uzun süre bloklamamak için üst sınır)
            chunk = 400
            found: set[int] = set()
            for base in range(start, end + 1, chunk):
                batch = list(range(base, min(base + chunk, end + 1)))
                hits = await asyncio.gather(*[check(p) for p in batch])
                for hit in hits:
                    if hit is not None:
                        found.add(hit)
                if found:
                    break  # ilk isabet kümesi yeterli; sonraki tick bilinenleri kullanır
            self._known_slot_ports |= found | configured

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

        remaining_ports = [r.port for r in runners if r.port and r.port not in port_model]
        remaining_models = [
            (m.get("name") or m.get("model") or "")
            for m in running
            if (m.get("name") or m.get("model") or "")
            and (m.get("name") or m.get("model") or "") not in used_models
        ]
        for port, model in zip(remaining_ports, remaining_models):
            port_model[port] = model
        if (
            len(remaining_ports) == 1
            and len(running) == 1
            and remaining_ports[0] not in port_model
        ):
            only = running[0].get("name") or running[0].get("model")
            if only:
                port_model[remaining_ports[0]] = str(only)
        return port_model

    def _client_candidates(self, clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in clients:
            agent = (c.get("agent") or "").strip()
            if not agent or agent in NOISE_AGENTS:
                continue
            out.append(c)
        return out

    def _select_active_agent(self, clients: list[dict[str, Any]]) -> str:
        """Pick the single client most likely driving the current request.

        Prefers the connected client with the highest process CPU; long-lived
        `serve` daemons are de-prioritized so idle keep-alives don't steal credit.
        """
        candidates = self._client_candidates(clients)
        if not candidates:
            return "external"
        if len(candidates) == 1:
            return str(candidates[0].get("agent") or "external")

        scored: list[tuple[float, float, str]] = []
        for c in candidates:
            pid = c.get("pid")
            if not isinstance(pid, int):
                continue
            _norm, raw, _rss = self.cpu.sample(pid)
            cmdline = (c.get("cmdline") or "").lower()
            is_service = " serve" in f" {cmdline}" or cmdline.rstrip().endswith("serve")
            # One-shot clients beat idle service keep-alives
            score = raw + (0.0 if is_service else 40.0)
            scored.append((score, raw, str(c.get("agent") or "external")))
        if not scored:
            return str(candidates[0].get("agent") or "external")

        scored.sort(key=lambda x: x[0], reverse=True)
        _score, top_raw, top_agent = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if top_raw >= 2.0 or _score >= second_score + 10:
            return top_agent
        return top_agent

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
        # Docker Desktop / Mac: host süreçleri görünmez veya ad farklı → /slots port keşfi
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
            # Süreç var ama --port yoksa host port dosyasını kullan
            if need_ports and not any(r.port for r in runners):
                await self._refresh_known_slot_ports()
                synth = self._synthetic_runners_from_ports()
                if synth:
                    runners = synth

        clients = list_clients_on_port(self._ollama_port)
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
            inferring = slot_processing
            busy = (not inferring) and (r.cpu_raw >= settings.runner_cpu_active_pct)
            if inferring:
                status = "inferring"
            elif busy:
                status = "busy"
            else:
                status = "loaded"

            row = {
                "pid": r.pid or None,
                "model": model,
                "blob_sha": (r.blob_sha or "")[:12],
                "cpu_pct": r.cpu_pct,
                "cpu_raw": r.cpu_raw,
                "rss_bytes": r.rss_bytes,
                "port": r.port,
                "inferring": inferring,
                "slot_processing": slot_processing,
                "status": status,
                "live_tps": slot_meta.get("live_tps") if slot_processing else None,
                "avg_tps": slot_meta.get("avg_tps") if slot_processing else None,
                "tokens": (slot_meta.get("tokens") or 0) if slot_processing else 0,
                "prompt_tokens": slot_meta.get("prompt_tokens") or 0,
                "agent": slot_meta.get("agent") if slot_processing else None,
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

            if name not in runner_by_model:
                ttl = round(exp_ts - now, 1) if exp_ts else None
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
                        "status": "resident",
                        "expires_in_sec": ttl,
                        "size_vram": m.get("size_vram"),
                        "live_tps": None,
                        "tokens": 0,
                        "agent": None,
                    }
                )
            else:
                row = runner_by_model[name]
                row["size_vram"] = m.get("size_vram")
                if exp_ts:
                    row["expires_in_sec"] = round(exp_ts - now, 1)

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

        self.store.set_observed(deduped, clients, sample_gpus())
