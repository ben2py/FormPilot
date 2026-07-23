from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InteractionRequest:
    id: str
    kind: str  # confirm | pause | missing
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)


class InteractionBridge:
    """Bridge agent confirm/pause/missing prompts to a web frontend."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def emit(self, event_type: str, **payload: Any) -> None:
        await self.events.put({"type": event_type, "ts": time.time(), **payload})

    async def _ask(self, kind: str, payload: dict[str, Any], *, timeout: float = 3600) -> Any:
        req_id = secrets.token_urlsafe(12)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        async with self._lock:
            self._pending[req_id] = future
        await self.emit("interaction", id=req_id, kind=kind, payload=payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            await self.emit("interaction_timeout", id=req_id, kind=kind)
            if kind == "confirm":
                return False
            if kind == "missing":
                return {}
            return ""
        finally:
            async with self._lock:
                self._pending.pop(req_id, None)

    async def respond(self, req_id: str, answer: Any) -> bool:
        async with self._lock:
            future = self._pending.get(req_id)
            if future is None or future.done():
                return False
            future.set_result(answer)
        await self.emit("interaction_resolved", id=req_id)
        return True

    async def confirm(self, message: str) -> bool:
        result = await self._ask("confirm", {"summary": message})
        return bool(result)

    async def pause(self, message: str) -> str:
        result = await self._ask("pause", {"message": message})
        if isinstance(result, dict):
            return str(result.get("guidance") or "").strip()
        return str(result or "").strip()

    async def missing(self, fields: list[dict[str, Any]]) -> dict[str, str]:
        result = await self._ask("missing", {"fields": fields})
        if not isinstance(result, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in result.items():
            text = str(value or "").strip()
            if text:
                out[str(key)] = text
        return out
