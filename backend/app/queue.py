"""Worker que atiende la cola de Supabase: analiza, renderiza y sube los clips.

La interfaz escribe una fila en `clip_requests` y se olvida. Este worker, que corre dentro
del backend de la sandbox, la reclama, lanza el pipeline de siempre, va escribiendo el
progreso en esa misma fila (que es lo que la interfaz ve en directo por Realtime) y acaba
subiendo los mp4 a Storage con una fila en `clips` por cada uno.

Del lado del navegador todo es Realtime. De este lado es un sondeo corto: para saber que
hay una peticion nueva bastan un GET de una fila cada dos segundos, y a cambio no hay que
mantener un websocket de Phoenix vivo dentro del backend ni reconectarlo a mano.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import db, service
from .config import settings
from .models import ClipOut
from .providers.supabase import Supabase, SupabaseError, configured
from .utils import log

REQUESTS = "clip_requests"
CLIPS = "clips"
# Reparto del progreso que ve la interfaz: analizar es lo que se lleva el tiempo, pero
# renderizar tres clips tampoco es instantaneo.
ANALYSIS_SHARE = 0.75
# Cada cuanto se mira el estado del job local mientras corre.
WATCH_S = 2.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _worker_name() -> str:
    return f"{socket.gethostname()}/{os.getpid()}"


class QueueWorker:
    """Un solo consumidor de la cola, atado al ciclo de vida del backend."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last_error = ""

    # -------------------------------------------------------------- ciclo de vida

    async def start(self) -> None:
        if not configured():
            log.info("cola de Supabase desactivada (sin SUPABASE_URL/SERVICE_KEY)")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="supabase-queue")
        log.info("cola de Supabase activa (proyecto %s)", settings.supabase_url)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    # ------------------------------------------------------------------- el bucle

    async def _run(self) -> None:
        sb = Supabase()
        try:
            await self._requeue_orphans(sb)
            while not self._stop.is_set():
                try:
                    request = await self._claim(sb)
                except (SupabaseError, httpx.HTTPError) as exc:
                    self.last_error = str(exc)[:300]
                    log.warning("no se pudo leer la cola: %s", exc)
                    await self._sleep(min(30.0, settings.supabase_poll_s * 5))
                    continue
                if request is None:
                    await self._sleep(settings.supabase_poll_s)
                    continue
                self.last_error = ""
                await self._process(sb, request)
        except asyncio.CancelledError:
            raise
        finally:
            await sb.close()

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _requeue_orphans(self, sb: Supabase) -> None:
        """Devuelve a la cola lo que quedo a medias en un contenedor anterior.

        La sandbox es efimera: si se recicla en mitad de un analisis, la fila se queda en
        `running` para siempre y la interfaz muestra un progreso que ya no avanza. Como
        solo hay un worker, al arrancar nada puede estar legitimamente en marcha.
        """
        try:
            rows = await sb.update(
                REQUESTS,
                {
                    "status": "queued", "stage": "queued", "progress": 0,
                    "message": "Reencolada: el backend se reinicio", "job_id": None,
                    "worker": None, "claimed_at": None,
                },
                match={"status": "in.(claimed,running)"},
            )
        except (SupabaseError, httpx.HTTPError) as exc:
            log.warning("no se pudieron reencolar las peticiones a medias: %s", exc)
            return
        if rows:
            log.info("%d peticiones a medias devueltas a la cola", len(rows))

    async def _claim(self, sb: Supabase) -> dict[str, Any] | None:
        """Coge la peticion mas antigua en cola, o None si no hay ninguna libre."""
        pending = await sb.select(
            REQUESTS,
            params={
                "select": "*", "status": "eq.queued",
                "order": "created_at.asc", "limit": "1",
            },
        )
        if not pending:
            return None
        # El filtro por `status` en el PATCH es lo que hace la reclamacion segura: si
        # otro worker (o un reinicio) se la llevo entre el GET y el PATCH, no devuelve
        # fila y se vuelve a mirar.
        claimed = await sb.update(
            REQUESTS,
            {
                "status": "claimed", "claimed_at": _now(), "worker": _worker_name(),
                "message": "Cogida por el backend", "error": None,
            },
            match={"id": f"eq.{pending[0]['id']}", "status": "eq.queued"},
        )
        return claimed[0] if claimed else None

    # -------------------------------------------------------------- una peticion

    async def _process(self, sb: Supabase, request: dict[str, Any]) -> None:
        request_id = str(request["id"])
        url = str(request["url"])
        wanted = max(1, min(10, int(request.get("clips") or 1)))
        log.info("cola: peticion %s (%s, %d clips)", request_id[:8], url, wanted)
        try:
            await self._patch(sb, request_id, {
                "status": "running", "stage": "ingest", "progress": 0.01,
                "message": "Leyendo el VOD",
            })
            job_id, video = await service.submit_job(url)
            await self._patch(sb, request_id, {
                "job_id": job_id,
                "video_title": str(video.get("title") or ""),
                "video_url": str(video.get("url") or url),
                "duration_s": float(video.get("duration") or 0.0),
                "message": "Analizando",
            })
            error = await self._watch_job(sb, request_id, job_id)
            if error:
                await self._fail(sb, request_id, error)
                return
            done = await self._render_and_upload(sb, request_id, job_id, wanted)
            await self._patch(sb, request_id, {
                "status": "done", "stage": "done", "progress": 1.0,
                "clips_done": done, "finished_at": _now(),
                "message": (
                    f"{done} clips listos" if done > 1
                    else "1 clip listo" if done == 1
                    else "Sin clips: el analisis no dejo momentos"
                ),
            })
            log.info("cola: peticion %s terminada con %d clips", request_id[:8], done)
        except asyncio.CancelledError:
            # El backend se esta apagando: que vuelva a la cola en el proximo arranque.
            with contextlib.suppress(Exception):
                await self._patch(sb, request_id, {
                    "status": "queued", "stage": "queued", "progress": 0,
                    "message": "Reencolada: el backend se apago", "worker": None,
                })
            raise
        except service.ServiceError as exc:
            await self._fail(sb, request_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - la cola no se puede morir por una fila
            log.exception("cola: peticion %s fallo", request_id[:8])
            await self._fail(sb, request_id, f"{type(exc).__name__}: {exc}")

    async def _patch(self, sb: Supabase, request_id: str, patch: dict[str, Any]) -> None:
        with contextlib.suppress(SupabaseError, httpx.HTTPError):
            await sb.update(REQUESTS, patch, match={"id": f"eq.{request_id}"})

    async def _fail(self, sb: Supabase, request_id: str, reason: str) -> None:
        log.warning("cola: peticion %s en error: %s", request_id[:8], reason)
        await self._patch(sb, request_id, {
            "status": "error", "error": reason[:600], "finished_at": _now(),
            "message": "Fallo", "stage": "error",
        })

    async def _watch_job(self, sb: Supabase, request_id: str, job_id: str) -> str:
        """Refleja el progreso del job local hasta que acaba. Devuelve el error, si hubo."""
        last: tuple[str, float] = ("", -1.0)
        while not self._stop.is_set():
            row = db.row_to_dict(
                await db.fetch_one(
                    "SELECT status, stage, progress, message, error FROM jobs WHERE id=?",
                    (job_id,),
                )
            )
            if not row:
                return "El job desaparecio de la base local"
            status = str(row.get("status") or "")
            if status == "done":
                # No se refleja la etapa "done" del job: para la peticion el trabajo no
                # ha terminado hasta que los clips estan subidos.
                return ""
            if status in ("error", "cancelled", "canceled"):
                return str(row.get("error") or "El analisis se cancelo")
            stage = str(row.get("stage") or "")
            progress = float(row.get("progress") or 0.0)
            # Solo se escribe cuando hay algo nuevo que contar: el pipeline emite
            # progreso cada pocas decimas y no hace falta un UPDATE por cada una.
            if stage != last[0] or progress - last[1] >= 0.02:
                last = (stage, progress)
                await self._patch(sb, request_id, {
                    "stage": stage,
                    "progress": round(progress * ANALYSIS_SHARE, 4),
                    "message": str(row.get("message") or "Analizando"),
                })
            await self._sleep(WATCH_S)
        return "El backend se apago durante el analisis"

    async def _render_and_upload(
        self, sb: Supabase, request_id: str, job_id: str, wanted: int
    ) -> int:
        moments = [
            db.row_to_dict(r)
            for r in await db.fetch_all(
                """SELECT * FROM moments WHERE job_id=?
                   ORDER BY final_score DESC LIMIT ?""",
                (job_id, wanted),
            )
        ]
        if not moments:
            return 0
        video = db.row_to_dict(
            await db.fetch_one(
                "SELECT url, title FROM videos WHERE id=?", (moments[0]["video_id"],)
            )
        )
        done = 0
        for i, moment in enumerate(moments):
            share = ANALYSIS_SHARE + (1 - ANALYSIS_SHARE) * i / len(moments)
            await self._patch(sb, request_id, {
                "stage": "render", "progress": round(share, 4),
                "message": f"Renderizando clip {i + 1}/{len(moments)}",
            })
            clip = await service.render_moment_clip(str(moment["id"]))
            row = db.row_to_dict(
                await db.fetch_one(
                    "SELECT clip_path FROM moments WHERE id=?", (moment["id"],)
                )
            )
            path = Path(str(row.get("clip_path") or ""))
            if not path.exists():
                log.warning("el clip de %s no aparecio en disco", moment["id"])
                continue
            storage_path = f"{request_id}/{moment['id']}.mp4"
            enriched = {
                **moment,
                "video_url": video.get("url"),
                "video_title": video.get("title"),
            }
            await sb.upload(storage_path, path.read_bytes(), content_type="video/mp4")
            await sb.insert(CLIPS, _clip_row(request_id, enriched, clip, storage_path),
                            upsert_on="moment_id")
            done += 1
            await self._patch(sb, request_id, {"clips_done": done})
        return done


def _clip_row(
    request_id: str | None, moment: dict[str, Any], clip: ClipOut, storage_path: str
) -> dict[str, Any]:
    """Fila de `clips` a partir del momento local y del resultado del render."""
    return {
        "request_id": request_id,
        "moment_id": str(moment["id"]),
        "rank": int(moment.get("rank") or 0),
        "title": str(moment.get("clip_title") or moment.get("title") or ""),
        "storage_path": storage_path,
        "size_bytes": int(clip.size_bytes),
        "duration_s": float(clip.duration),
        "width": int(clip.width),
        "height": int(clip.height),
        "video_url": str(moment.get("video_url") or ""),
        "video_title": str(moment.get("video_title") or ""),
        "t_start": float(moment.get("t_start") or 0.0),
        "t_end": float(moment.get("t_end") or 0.0),
        "score": float(moment.get("final_score") or 0.0),
        "clip_score": float(moment.get("clip_score") or 0.0),
        "source": str(moment.get("source") or "signals"),
        "category": str(moment.get("category") or ""),
        "sfx": str(moment.get("sfx_fit") or "ninguno"),
        "music": str(moment.get("music") or "ninguna"),
        "transcript": str(moment.get("transcript") or ""),
        "reason": str(moment.get("description") or ""),
    }


async def publish_existing(
    sb: Supabase, moment_ids: list[str], *, request_id: str | None = None
) -> list[str]:
    """Sube clips ya renderizados en disco (los de antes de conectar Supabase)."""
    published = []
    for moment_id in moment_ids:
        moment, video = await service.load_moment(moment_id)
        path = Path(str(moment.get("clip_path") or ""))
        if not path.exists():
            log.warning("%s no tiene clip renderizado en disco", moment_id)
            continue
        clip = await service.render_moment_clip(moment_id)   # reutiliza el mp4 existente
        moment = {**moment, "video_url": video.get("url"), "video_title": video.get("title")}
        storage_path = f"{request_id or 'manual'}/{moment_id}.mp4"
        await sb.upload(storage_path, path.read_bytes(), content_type="video/mp4")
        await sb.insert(CLIPS, _clip_row(request_id, moment, clip, storage_path),
                        upsert_on="moment_id")
        published.append(moment_id)
        log.info("subido %s (%.1f MB)", moment_id, path.stat().st_size / 1048576)
    return published


worker = QueueWorker()
