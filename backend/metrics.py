from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any


NS = 1_000_000_000


def tokens_per_second(count: int | None, duration_ns: int | None) -> float | None:
    if not count or not duration_ns or duration_ns <= 0:
        return None
    return round(count / (duration_ns / NS), 2)


def ns_to_ms(duration_ns: int | None) -> float | None:
    if duration_ns is None:
        return None
    return round(duration_ns / 1_000_000, 2)


@dataclass
class RunMetrics:
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tps: float | None = None
    completion_tps: float | None = None
    total_duration_ms: float | None = None
    load_duration_ms: float | None = None
    prompt_eval_ms: float | None = None
    eval_ms: float | None = None
    ttft_ms: float | None = None
    wall_ms: float | None = None
    done: bool = False
    timestamp: float = field(default_factory=time.time)
    error: str | None = None
    source: str = "benchmark"  # benchmark | observed
    agent: str = "llmperf"
    client: str | None = None
    endpoint: str | None = None
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def metrics_from_ollama_final(
    model: str,
    data: dict[str, Any],
    *,
    wall_ms: float | None = None,
    ttft_ms: float | None = None,
    source: str = "benchmark",
    agent: str = "llmperf",
    client: str | None = None,
    endpoint: str | None = None,
    session_id: str | None = None,
) -> RunMetrics:
    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    completion_tokens = int(data.get("eval_count") or 0)
    return RunMetrics(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tps=tokens_per_second(prompt_tokens, data.get("prompt_eval_duration")),
        completion_tps=tokens_per_second(completion_tokens, data.get("eval_duration")),
        total_duration_ms=ns_to_ms(data.get("total_duration")),
        load_duration_ms=ns_to_ms(data.get("load_duration")),
        prompt_eval_ms=ns_to_ms(data.get("prompt_eval_duration")),
        eval_ms=ns_to_ms(data.get("eval_duration")),
        ttft_ms=ttft_ms,
        wall_ms=wall_ms,
        done=bool(data.get("done", True)),
        source=source,
        agent=agent,
        client=client,
        endpoint=endpoint,
        session_id=session_id,
    )


class MetricsStore:
    """In-memory state for passive observation + optional benchmark runs."""

    def __init__(self, history_size: int = 120) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self.activity: deque[dict[str, Any]] = deque(maxlen=100)
        self.sessions: dict[str, dict[str, Any]] = {}  # benchmark only
        self.observed: list[dict[str, Any]] = []  # live runners / models
        self.clients: list[dict[str, Any]] = []
        self.gpus: list[dict[str, Any]] = []
        self.live: dict[str, Any] = {
            "status": "idle",
            "model": None,
            "tokens": 0,
            "elapsed_ms": 0,
            "live_tps": None,
            "total_tps": None,
            "agent_tps": {},
            "partial": "",
            "active_count": 0,
            "mode": "passive",
        }
        self.throughput: dict[str, Any] = {
            "total_tps": None,
            "agent_tps": {},
            "active_tokens": 0,
        }
        self.ollama_status: dict[str, Any] = {"ok": False}
        self.models: list[dict[str, Any]] = []
        self.running: list[dict[str, Any]] = []
        self._last_broadcast = 0.0
        self._prev_expires: dict[str, float] = {}
        self._prev_running_names: set[str] = set()

    def push_activity(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        self.activity.appendleft(event)

    def start_session(
        self,
        *,
        model: str,
        agent: str,
        client: str | None,
        endpoint: str,
        source: str = "benchmark",
        prompt_preview: str = "",
    ) -> str:
        import uuid

        sid = uuid.uuid4().hex[:12]
        now = time.time()
        self.sessions[sid] = {
            "id": sid,
            "model": model,
            "agent": agent,
            "client": client,
            "endpoint": endpoint,
            "source": source,
            "status": "running",
            "tokens": 0,
            "live_tps": None,
            "elapsed_ms": 0,
            "partial": "",
            "prompt_preview": prompt_preview[:160],
            "started_at": now,
            "updated_at": now,
        }
        self.push_activity(
            {
                "type": "start",
                "session_id": sid,
                "model": model,
                "agent": agent,
                "endpoint": endpoint,
                "source": source,
            }
        )
        self._sync_live()
        return sid

    def update_session(self, sid: str, **kwargs: Any) -> None:
        sess = self.sessions.get(sid)
        if not sess:
            return
        sess.update(kwargs)
        sess["updated_at"] = time.time()
        self._sync_live()

    def finish_session(
        self,
        sid: str,
        metrics: RunMetrics | None = None,
        *,
        error: str | None = None,
    ) -> None:
        sess = self.sessions.pop(sid, None)
        if metrics:
            self.history.appendleft(metrics.to_dict())
            self.live["last_run"] = metrics.to_dict()
        self.push_activity(
            {
                "type": "error" if error else "finish",
                "session_id": sid,
                "model": (metrics.model if metrics else (sess or {}).get("model")),
                "agent": (metrics.agent if metrics else (sess or {}).get("agent")),
                "endpoint": (sess or {}).get("endpoint"),
                "completion_tps": metrics.completion_tps if metrics else None,
                "completion_tokens": metrics.completion_tokens if metrics else None,
                "error": error,
                "source": (metrics.source if metrics else (sess or {}).get("source")),
            }
        )
        self._sync_live()

    def add_observed_run(self, metrics: RunMetrics) -> None:
        self.history.appendleft(metrics.to_dict())
        self.live["last_run"] = metrics.to_dict()

    def set_observed(
        self,
        observed: list[dict[str, Any]],
        clients: list[dict[str, Any]],
        gpus: list[dict[str, Any]] | None = None,
    ) -> None:
        self.observed = observed
        self.clients = clients
        if gpus is not None:
            self.gpus = gpus
        self._sync_live()

    def _sync_live(self) -> None:
        # Only runners that are actually generating contribute to ajan tok/s
        active_obs = [
            o
            for o in self.observed
            if o.get("slot_processing") and (o.get("avg_tps") or o.get("live_tps"))
        ]
        bench = list(self.sessions.values())
        active_count = len(active_obs) + len(bench)
        self.live["active_count"] = active_count
        self.live["mode"] = "passive"

        agent_tps: dict[str, float] = {}
        total = 0.0
        active_tokens = 0

        for o in active_obs:
            tps = o.get("avg_tps") or o.get("live_tps")
            if not tps or float(tps) <= 0:
                continue
            agent = (o.get("agent") or "").strip()
            # Reject joined multi-agent labels like "opencode, python"
            if not agent or "," in agent or agent in {"?", "unknown"}:
                agent = "external"
            agent_tps[agent] = round(agent_tps.get(agent, 0.0) + float(tps), 2)
            total += float(tps)
            active_tokens += int(o.get("tokens") or 0)

        for s in bench:
            tps = s.get("live_tps")
            if tps and float(tps) > 0:
                agent = (s.get("agent") or "llmperf").strip()
                if "," in agent:
                    agent = "llmperf"
                agent_tps[agent] = round(agent_tps.get(agent, 0.0) + float(tps), 2)
                total += float(tps)
            active_tokens += int(s.get("tokens") or 0)

        total_tps = round(total, 2) if total > 0 else None
        self.throughput = {
            "total_tps": total_tps,
            "agent_tps": agent_tps,
            "active_tokens": active_tokens,
        }
        self.live["total_tps"] = total_tps
        self.live["agent_tps"] = agent_tps

        if bench:
            best = max(bench, key=lambda s: s.get("live_tps") or 0)
            self.live.update(
                {
                    "status": "running",
                    "model": best.get("model"),
                    "tokens": active_tokens,
                    "elapsed_ms": best.get("elapsed_ms"),
                    "live_tps": best.get("live_tps"),
                    "partial": best.get("partial") or "",
                    "agent": best.get("agent"),
                    "active_agents": sorted(agent_tps.keys()),
                }
            )
            return

        if active_obs:
            best = max(
                active_obs,
                key=lambda o: (o.get("avg_tps") or o.get("live_tps") or 0, o.get("cpu_pct") or 0),
            )
            self.live.update(
                {
                    "status": "running",
                    "model": best.get("model"),
                    "tokens": active_tokens,
                    "elapsed_ms": None,
                    "live_tps": best.get("avg_tps") or best.get("live_tps"),
                    "partial": (
                        f"{best.get('tokens') or 0} tok · "
                        f"{best.get('avg_tps') or best.get('live_tps') or '—'} t/s · "
                        f"{best.get('agent') or 'external'}"
                    ),
                    "agent": best.get("agent") or "external",
                    "active_agents": sorted(agent_tps.keys()),
                }
            )
            return

        self.live["status"] = "idle"
        self.live["live_tps"] = None
        self.live["tokens"] = 0
        self.live["active_agents"] = []

    def should_broadcast(self, min_interval_ms: float) -> bool:
        now = time.perf_counter()
        if (now - self._last_broadcast) * 1000 < min_interval_ms:
            return False
        self._last_broadcast = now
        return True

    def force_broadcast_mark(self) -> None:
        self._last_broadcast = time.perf_counter()

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": "1.2.5",
            "ollama": self.ollama_status,
            "models": self.models,
            "running": self.running,
            "live": self.live,
            "sessions": sorted(
                self.sessions.values(),
                key=lambda s: s.get("started_at") or 0,
                reverse=True,
            ),
            "observed": self.observed,
            "clients": self.clients,
            "gpus": self.gpus,
            "throughput": self.throughput,
            "activity": list(self.activity),
            "history": list(self.history),
            "mode": "passive",
            "ts": time.time(),
        }
