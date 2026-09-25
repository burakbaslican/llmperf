from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from .metrics import MetricsStore, metrics_from_ollama_final
from .ollama_client import ollama
from .passive import PassiveObserver

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
store = MetricsStore(history_size=settings.history_size)
observer = PassiveObserver(store)
clients: set[WebSocket] = set()
_poll_task: asyncio.Task | None = None
_bench_lock = asyncio.Lock()


class BenchRequest(BaseModel):
    model: str
    prompt: str = Field(
        default=(
            "Transformer modellerinin token üretme biçimini kısaca teknik olarak açıkla, "
            "ardından yerel LLM çıkarımı için 5 pratik ipucu listele."
        ),
        min_length=1,
    )
    num_predict: int = Field(default=128, ge=1, le=4096)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


async def broadcast(force: bool = True) -> None:
    if not force and not store.should_broadcast(settings.broadcast_min_interval_ms):
        return
    if force:
        store.force_broadcast_mark()
    message = json.dumps(store.snapshot())
    dead: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(message)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def refresh_ollama_state() -> None:
    health = await ollama.health()
    store.ollama_status = health
    if not health.get("ok"):
        store.models = []
        store.running = []
        store.set_observed([], [], [])
        return
    try:
        store.models = await ollama.list_models()
        store.running = await ollama.running_models()
    except Exception as exc:  # noqa: BLE001
        store.ollama_status = {
            "ok": False,
            "url": health.get("url"),
            "error": str(exc),
        }
        return
    try:
        await observer.tick(store.running, store.models)
    except Exception as exc:  # noqa: BLE001
        # Observer hatası Ollama'yı offline göstermesin (Mac /slots parse vb.)
        store.ollama_status = {
            **health,
            "observer_error": str(exc),
        }


async def poll_loop() -> None:
    while True:
        await refresh_ollama_state()
        await broadcast(True)
        await asyncio.sleep(settings.poll_interval_sec)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _poll_task
    await refresh_ollama_state()
    _poll_task = asyncio.create_task(poll_loop())
    yield
    if _poll_task:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="LLMPerf", version="1.2.4", lifespan=lifespan)


@app.get("/api/health")
async def api_health():
    await refresh_ollama_state()
    return {
        "app": "ok",
        "mode": "passive",
        "ollama": store.ollama_status,
        "active_observed": sum(1 for o in store.observed if o.get("inferring")),
        "clients": len(store.clients),
    }


@app.get("/api/snapshot")
async def api_snapshot():
    await refresh_ollama_state()
    return store.snapshot()


@app.get("/api/models")
async def api_models():
    try:
        models = await ollama.list_models()
        store.models = models
        return {"models": models}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/observed")
async def api_observed():
    return {
        "observed": store.observed,
        "clients": store.clients,
        "activity": list(store.activity),
    }


@app.post("/api/benchmark")
async def api_benchmark(req: BenchRequest):
    if _bench_lock.locked():
        raise HTTPException(status_code=409, detail="A benchmark is already running")

    async with _bench_lock:
        return await _run_benchmark(req)


def _chunk_text(chunk: dict[str, Any]) -> str:
    """Ollama generate/chat stream parçasından metin çıkar (MLX uyumlu)."""
    if not isinstance(chunk, dict):
        return ""
    text = chunk.get("response")
    if isinstance(text, str) and text:
        return text
    msg = chunk.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content:
            return content
    for key in ("content", "text", "output"):
        val = chunk.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


async def _run_benchmark(req: BenchRequest) -> dict[str, Any]:
    started = time.perf_counter()
    first_token_at: float | None = None
    token_count = 0
    response_text: list[str] = []
    final: dict[str, Any] | None = None

    sid = store.start_session(
        model=req.model,
        agent="llmperf",
        client="dashboard",
        endpoint="/api/benchmark",
        source="benchmark",
        prompt_preview=req.prompt,
    )
    await broadcast(True)

    try:
        async for raw in ollama.generate_stream(
            model=req.model,
            prompt=req.prompt,
            options={
                "num_predict": req.num_predict,
                "temperature": req.temperature,
            },
        ):
            chunk = json.loads(raw)
            piece = _chunk_text(chunk)
            if piece:
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                token_count += 1
                response_text.append(piece)

            now = time.perf_counter()
            elapsed_ms = (now - started) * 1000
            gen_elapsed = (now - first_token_at) if first_token_at else 0
            live_tps = (
                round(token_count / gen_elapsed, 2) if first_token_at and gen_elapsed > 0 else None
            )
            full = "".join(response_text)
            store.update_session(
                sid,
                tokens=token_count,
                elapsed_ms=round(elapsed_ms, 1),
                live_tps=live_tps,
                partial=full,
            )
            await broadcast(False)

            if chunk.get("done"):
                final = chunk
                # Bazı modeller metni yalnızca son chunk'ta bırakır
                tail = _chunk_text(chunk)
                if tail and (not response_text or response_text[-1] != tail):
                    # son chunk'ta full text gelebilir; tekrar ekleme
                    if not full and tail:
                        response_text.append(tail)
                        full = tail
                        store.update_session(sid, partial=full, tokens=max(token_count, 1))
                break
    except Exception as exc:  # noqa: BLE001
        store.finish_session(sid, error=str(exc))
        await broadcast(True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    wall_ms = (time.perf_counter() - started) * 1000
    ttft_ms = ((first_token_at - started) * 1000) if first_token_at else None
    metrics = metrics_from_ollama_final(
        req.model,
        final or {},
        wall_ms=round(wall_ms, 2),
        ttft_ms=round(ttft_ms, 2) if ttft_ms is not None else None,
        source="benchmark",
        agent="llmperf",
        client="dashboard",
        endpoint="/api/benchmark",
        session_id=sid,
    )
    if metrics.completion_tokens == 0 and token_count:
        metrics.completion_tokens = token_count
        metrics.total_tokens = metrics.prompt_tokens + token_count

    full_response = "".join(response_text)
    store.finish_session(sid, metrics)
    await refresh_ollama_state()
    # refresh _sync_live çağırır; cevabı live'da tut (WS UI'yi boşaltmasın)
    store.live["last_response"] = full_response
    store.live["last_response_model"] = req.model
    await broadcast(True)
    return {"metrics": metrics.to_dict(), "response": full_response}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps(store.snapshot()))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
