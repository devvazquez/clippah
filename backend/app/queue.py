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
from .pipeline import render
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

    def __init__(self, *, drain: bool = False) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last_error = ""
        # En modo `drain` el worker no se queda esperando trabajo nuevo: procesa lo que
        # hay y termina. Es lo que necesita un turno programado, que no puede quedarse
        # abierto para siempre.
        self._drain = drain

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

    async def wait(self) -> None:
        """Espera a que el bucle termine solo. Solo pasa en modo `drain`."""
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ------------------------------------------------------------------- el bucle

    async def _run(self) -> None:
        sb = Supabase()
        try:
            await self._requeue_orphans(sb)
            fallos = 0
            while not self._stop.is_set():
                try:
                    request = await self._claim(sb)
                except (SupabaseError, httpx.HTTPError) as exc:
                    self.last_error = str(exc)[:300]
                    log.warning("no se pudo leer la cola: %s", exc)
                    fallos += 1
                    # El backend de siempre reintenta indefinidamente, que es lo suyo
                    # cuando esta encendido. El turno programado se rinde y deja el aviso.
                    if self._drain and fallos >= 3:
                        log.error("la cola no responde: el turno programado se rinde")
                        return
                    await self._sleep(min(30.0, settings.supabase_poll_s * 5))
                    continue
                fallos = 0
                if request is not None:
                    self.last_error = ""
                    await self._process(sb, request)
                    continue
                # Sin nada nuevo que analizar, toca mirar si alguien ha corregido los
                # subtitulos de un clip ya hecho.
                clip = await self._claim_rerender(sb)
                if clip is None:
                    if self._drain:
                        log.info("cola vacia: el turno programado termina")
                        return
                    await self._sleep(settings.supabase_poll_s)
                    continue
                self.last_error = ""
                await self._rerender(sb, clip)
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
            clips = await sb.update(
                CLIPS,
                {"render_status": "rerender_queued"},
                match={"render_status": "eq.rendering"},
            )
        except (SupabaseError, httpx.HTTPError) as exc:
            log.warning("no se pudo reencolar lo que quedo a medias: %s", exc)
            return
        if rows:
            log.info("%d peticiones a medias devueltas a la cola", len(rows))
        if clips:
            log.info("%d re-renders a medias devueltos a la cola", len(clips))

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

    # ------------------------------------------------------ subtitulos corregidos

    async def _claim_rerender(self, sb: Supabase) -> dict[str, Any] | None:
        """Coge el clip mas antiguo con subtitulos pendientes de quemar."""
        pending = await sb.select(
            CLIPS,
            params={
                "select": "*", "render_status": "eq.rerender_queued",
                "order": "created_at.asc", "limit": "1",
            },
        )
        if not pending:
            return None
        claimed = await sb.update(
            CLIPS,
            {"render_status": "rendering", "render_error": None},
            match={
                "id": f"eq.{pending[0]['id']}",
                "render_status": "eq.rerender_queued",
            },
        )
        return claimed[0] if claimed else None

    async def _rerender(self, sb: Supabase, clip: dict[str, Any]) -> None:
        """Vuelve a quemar el clip con lo que ha editado la interfaz.

        Cada campo se manda solo si la interfaz lo ha tocado: `None` significa "deja lo
        que habia" y no es lo mismo que una lista vacia, que significa "ningun efecto".
        """
        clip_id = str(clip["id"])
        moment_id = str(clip["moment_id"])
        cues = clip.get("captions_edited")
        if cues is None:
            cues = clip.get("captions") or []
        sfx = clip.get("sfx_edited")
        music = clip.get("music_edited")
        titulo = clip.get("title_edited")
        log.info(
            "re-render de %s (%d frases, %s efectos, musica %r, titulo %r)",
            moment_id, len(cues), "auto" if sfx is None else len(sfx),
            music if music is not None else "sin cambios",
            titulo if titulo is not None else "sin cambios",
        )
        try:
            # En un contenedor recien clonado la base local esta vacia (no se versiona),
            # asi que el momento se reconstruye desde la ficha que se guardo al publicar.
            await service.ensure_moment(moment_id, clip.get("render_spec"))
            result = await service.render_moment_clip(
                moment_id, cues=cues, sfx=sfx, music=music, title=titulo
            )
            path = Path(render.clip_path(moment_id))
            if not path.exists():
                raise RuntimeError("el clip no aparecio en disco")
            # Ruta nueva en cada version: una URL firmada apunta a un objeto concreto, y
            # reemplazarlo por debajo deja a los navegadores sirviendo el mp4 viejo de su
            # cache. Cambiando de ruta, lo que se ve es siempre lo ultimo.
            version = int(clip.get("version") or 1) + 1
            old_path = str(clip.get("storage_path") or "")
            folder = old_path.rsplit("/", 1)[0] if "/" in old_path else "manual"
            new_path = f"{folder}/{moment_id}-v{version}.mp4"
            await sb.upload(new_path, path.read_bytes(), content_type="video/mp4")
            patch: dict[str, Any] = {
                "storage_path": new_path,
                "size_bytes": result.size_bytes,
                "duration_s": result.duration,
                "captions": [c.model_dump() for c in result.cues],
                "sfx_cues": [c.model_dump() for c in result.sfx_cues],
                "captions_edited": None,
                "sfx_edited": None,
                "music_edited": None,
                "title_edited": None,
                "version": version,
                "render_status": "ready",
                "render_error": None,
            }
            # `title`/`sfx`/`music` describen lo que lleva puesto el clip: si la edicion
            # los cambio, la fila tiene que contarlo, o la interfaz seguiria mostrando la
            # eleccion del modelo.
            if titulo is not None:
                patch["title"] = titulo.strip()
            if sfx is not None:
                patch["sfx"] = "manual" if sfx else "ninguno"
            if music is not None:
                patch["music"] = music or "ninguna"
            await sb.update(CLIPS, patch, match={"id": f"eq.{clip_id}"})
            if old_path and old_path != new_path:
                await sb.delete(old_path)
            log.info("re-render de %s listo (v%d)", moment_id, version)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await sb.update(CLIPS, {"render_status": "rerender_queued"},
                                match={"id": f"eq.{clip_id}"})
            raise
        except Exception as exc:  # noqa: BLE001 - un clip roto no puede parar la cola
            log.exception("re-render de %s fallo", moment_id)
            with contextlib.suppress(SupabaseError, httpx.HTTPError):
                await sb.update(CLIPS, {
                    "render_status": "error",
                    "render_error": f"{type(exc).__name__}: {exc}"[:600],
                }, match={"id": f"eq.{clip_id}"})

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
        # La fila entera del video, no solo el titulo: la ficha de re-render necesita el
        # recorte de las camaras y de que VOD sale.
        video = db.row_to_dict(
            await db.fetch_one(
                "SELECT * FROM videos WHERE id=?", (moments[0]["video_id"],)
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
                "video_date": video.get("upload_date"),
            }
            await sb.upload(storage_path, path.read_bytes(), content_type="video/mp4")
            poster = await upload_poster(sb, request_id, moment)
            await insert_clip(
                sb,
                _clip_row(request_id, enriched, clip, storage_path, poster,
                          service.rerender_spec(moment, video)),
            )
            done += 1
            await self._patch(sb, request_id, {"clips_done": done})
        return done


async def insert_clip(sb: Supabase, row: dict[str, Any]) -> None:
    """Escribe la fila del clip, aunque el esquema de Supabase se haya quedado atras.

    `render_spec` es una columna nueva: si el proyecto no la tiene todavia, PostgREST
    rechaza el insert entero y el clip no se publicaria. Antes que perder el clip, se
    sube sin la ficha y se avisa en el log: lo unico que se pierde es poder rehacerlo
    desde otra maquina.
    """
    try:
        await sb.insert(CLIPS, row, upsert_on="moment_id")
    except SupabaseError as exc:
        if "render_spec" not in str(exc):
            raise
        log.warning(
            "el proyecto de Supabase no tiene la columna render_spec: aplica "
            "supabase/schema.sql. El clip se sube sin la ficha de re-render."
        )
        await sb.insert(CLIPS, {k: v for k, v in row.items() if k != "render_spec"},
                        upsert_on="moment_id")


async def upload_poster(
    sb: Supabase, folder: str | None, moment: dict[str, Any]
) -> str | None:
    """Sube la miniatura del momento al lado del mp4: es la portada de las tarjetas.

    Se nombra por el momento y no por el mp4, que cambia de nombre en cada re-render: la
    portada es la misma foto siempre, y asi no se acumulan jpg huerfanos.
    """
    thumb = Path(str(moment.get("thumb_path") or ""))
    if not thumb.exists():
        log.warning("%s no tiene miniatura en disco", moment["id"])
        return None
    poster = f"{folder or 'manual'}/{moment['id']}.jpg"
    await sb.upload(poster, thumb.read_bytes(), content_type="image/jpeg")
    return poster


def _clip_row(
    request_id: str | None,
    moment: dict[str, Any],
    clip: ClipOut,
    storage_path: str,
    poster_path: str | None = None,
    spec: dict[str, Any] | None = None,
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
        "video_date": str(moment.get("video_date") or "") or None,
        "poster_path": poster_path,
        # La ficha para volver a quemarlo en una maquina que no hizo el analisis.
        "render_spec": spec,
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
        # Lo que lleva puesto ahora mismo: es lo que edita la interfaz.
        "captions": [c.model_dump() for c in clip.cues],
        "sfx_cues": [c.model_dump() for c in clip.sfx_cues],
        "render_status": "ready",
        "render_error": None,
    }


async def publish_existing(
    sb: Supabase, moment_ids: list[str], *, request_id: str | None = None
) -> list[str]:
    """Sube clips ya renderizados en disco (los de antes de conectar Supabase).

    Si el clip ya estaba publicado, se respeta su carpeta y se sube como version nueva:
    cambiar la ruta a mano dejaria la fila apuntando a un objeto que no existe, y
    reescribir el mismo objeto haria que los navegadores siguieran con el mp4 viejo.
    """
    published = []
    for moment_id in moment_ids:
        moment, video = await service.load_moment(moment_id)
        path = Path(str(moment.get("clip_path") or ""))
        if not path.exists():
            log.warning("%s no tiene clip renderizado en disco", moment_id)
            continue
        clip = await service.render_moment_clip(moment_id)   # reutiliza el mp4 existente
        moment = {
            **moment,
            "video_url": video.get("url"),
            "video_title": video.get("title"),
            "video_date": video.get("upload_date"),
        }

        existing = await sb.select(CLIPS, params={
            "select": "id,storage_path,version,request_id",
            "moment_id": f"eq.{moment_id}", "limit": "1",
        })
        if existing:
            old_path = str(existing[0].get("storage_path") or "")
            folder = old_path.rsplit("/", 1)[0] if "/" in old_path else "manual"
            version = int(existing[0].get("version") or 1) + 1
            storage_path = f"{folder}/{moment_id}-v{version}.mp4"
            keep_request = existing[0].get("request_id") or request_id
        else:
            old_path, version, keep_request = "", 1, request_id
            folder = request_id or "manual"
            storage_path = f"{folder}/{moment_id}.mp4"

        await sb.upload(storage_path, path.read_bytes(), content_type="video/mp4")
        poster = await upload_poster(sb, folder, moment)
        row = _clip_row(keep_request, moment, clip, storage_path, poster,
                        service.rerender_spec(moment, video))
        row["version"] = version
        await insert_clip(sb, row)
        if old_path and old_path != storage_path:
            await sb.delete(old_path)
        published.append(moment_id)
        log.info("subido %s v%d (%.1f MB)", moment_id, version, path.stat().st_size / 1048576)
    return published


worker = QueueWorker()
