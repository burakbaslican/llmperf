from __future__ import annotations

from typing import Any

import httpx

from .config import settings


class OllamaClient:
    def __init__(self, base_url: str | None = None, timeout: float = 300.0) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.timeout = timeout

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            resp = await client.get(path)
            resp.raise_for_status()
            return resp.json()

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
                resp = await client.get("/")
                return {
                    "ok": resp.status_code == 200,
                    "url": self.base_url,
                    "status_code": resp.status_code,
                }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": self.base_url, "error": str(exc)}

    async def list_models(self) -> list[dict[str, Any]]:
        data = await self._get("/api/tags")
        return data.get("models", [])

    async def running_models(self) -> list[dict[str, Any]]:
        data = await self._get("/api/ps")
        return data.get("models", [])

    async def show(self, name: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            resp = await client.post("/api/show", json={"name": name})
            resp.raise_for_status()
            return resp.json()

    async def generate_stream(
        self,
        model: str,
        prompt: str,
        options: dict[str, Any] | None = None,
    ):
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": True,
        }
        if options:
            payload["options"] = options

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            async with client.stream("POST", "/api/generate", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line:
                        yield line


ollama = OllamaClient()
