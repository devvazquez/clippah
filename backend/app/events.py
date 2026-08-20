"""Bus de eventos de progreso. Se persisten para que un SSE reconectado pueda reengancharse."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from . import db
from .utils import log

QUEUE_MAX = 512


class EventHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._seq: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def _next_seq(self, job_id: str) -> int:
        if job_id not in self._seq:
            row = await db.fetch_one(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM job_events WHERE job_id=?", (job_id,)
            )
            self._seq[job_id] = int(row["s"]) if row else 0
        self._seq[job_id] += 1
        return self._seq[job_id]

    async def publish(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            seq = await self._next_seq(job_id)
        event = {**payload, "seq": seq, "ts": time.time()}
        await db.execute(
            "INSERT OR REPLACE INTO job_events (job_id, seq, payload, ts) VALUES (?, ?, ?, ?)",
            (job_id, seq, db.dumps(event), event["ts"]),
        )
        for q in list(self._subs.get(job_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - cliente muy lento
                log.warning("cola SSE llena para job %s, se descarta un evento", job_id)
        return event

    async def history(self, job_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        rows = await db.fetch_all(
            "SELECT payload FROM job_events WHERE job_id=? AND seq>? ORDER BY seq",
            (job_id, after_seq),
        )
        return [db.loads(r["payload"], {}) for r in rows]

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._subs.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(job_id)
        if not subs:
            return
        subs.discard(q)
        if not subs:
            self._subs.pop(job_id, None)

    def subscriber_count(self, job_id: str) -> int:
        return len(self._subs.get(job_id, ()))

    async def forget(self, job_id: str) -> None:
        self._seq.pop(job_id, None)
        self._subs.pop(job_id, None)
        await db.execute("DELETE FROM job_events WHERE job_id=?", (job_id,))


hub = EventHub()
