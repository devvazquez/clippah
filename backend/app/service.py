"""Lo que hace el backend, sin HTTP por medio.

Encolar un analisis y renderizar un clip son operaciones del programa, no de la API: las
piden tanto las rutas de `main.py` como el worker que atiende la cola de Supabase. Viven
aqui para que las dos hagan exactamente lo mismo, incluida la deteccion de camaras y la
reutilizacion del clip ya renderizado.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import db
from .config import settings
from .events import hub
from .models import ClipOut, RenderSpec, Word
from .pipeline import frames, render, vision
from .pipeline.ingest import ProbeFailed, UnsupportedUrl, VodTooLong, probe, resolve_url
from .pipeline.orchestrator import new_id, runner
from .utils import CommandFailed, log


class ServiceError(Exception):
    """Fallo que el llamante puede contar tal cual al usuario."""


class NotFound(ServiceError):
    pass


class BadRequest(ServiceError):
    """La peticion no vale: URL no soportada, VOD demasiado largo..."""


class Unavailable(ServiceError):
    """Algo de fuera no responde: yt-dlp, el stream, ffmpeg."""


# --------------------------------------------------------------------- analisis


async def submit_job(url: str) -> tuple[str, dict[str, Any]]:
    """Da de alta un analisis y lo mete en la cola local. Devuelve (job_id, video)."""
    from .pipeline.orchestrator import _upsert_video

    try:
        resolved = resolve_url(url)
    except UnsupportedUrl as exc:
        raise BadRequest(str(exc)) from exc
    try:
        info = await probe(resolved)
    except (VodTooLong, ProbeFailed) as exc:
        raise BadRequest(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - yt-dlp puede fallar por mil motivos
        raise Unavailable(f"No se pudo leer el VOD: {exc}") from exc

    video_id = await _upsert_video(info)
    job_id = new_id("job")
    now = time.time()
    await db.execute(
        """INSERT INTO jobs (id, video_id, url, status, stage, progress, message,
               created_at, updated_at)
           VALUES (?, ?, ?, 'queued', 'queued', 0, 'En cola', ?, ?)""",
        (job_id, video_id, info.url, now, now),
    )
    await hub.publish(job_id, {"stage": "queued", "progress": 0.0, "message": "En cola"})
    await runner.submit(job_id)
    video = db.row_to_dict(await db.fetch_one("SELECT * FROM videos WHERE id=?", (video_id,)))
    return job_id, video


# ----------------------------------------------------------------------- render


def build_render_spec(moment: dict[str, Any], video: dict[str, Any]) -> RenderSpec:
    words = db.loads(moment.get("words"), []) or []
    return RenderSpec(
        moment_id=str(moment["id"]),
        source_url=str(video.get("url") or ""),
        t_start=float(moment["t_start"]),
        t_end=float(moment["t_end"]),
        aspect="9:16",
        title=str(moment["title"]),
        # Los timestamps que se guardan ya son absolutos respecto al VOD.
        captions=[
            Word(text=str(w.get("text") or ""), start=float(w.get("start") or 0.0),
                 end=float(w.get("end") or 0.0))
            for w in words
        ],
        sfx_cues=[],
    )


async def clip_source(video: dict[str, Any]) -> str:
    """Fichero local si lo hay; si no, la URL del stream (re-resolviendola si caduco)."""
    local = video.get("video_path")
    if local and Path(str(local)).exists():
        return str(local)
    url = str(video.get("stream_url") or "")
    expires = float(video.get("stream_url_expires_at") or 0.0)
    if not url or (expires and expires < time.time()):
        url = await frames._refresh_stream_url(video)
    if not url:
        raise Unavailable(
            "No hay fuente de video para renderizar: no se pudo resolver el stream"
        )
    return url


async def cam_layout(
    video: dict[str, Any], moment: dict[str, Any]
) -> dict[str, list[float]] | None:
    """Rectangulos de las webcams del VOD, detectandolos si aun no se sabian."""
    saved = db.loads(video.get("cam_layout"), None)
    if saved:
        return saved
    source = str(video.get("video_path") or "") or await clip_source(video)
    layout = await vision.probe_cam_layout(source, float(moment["t_start"]))
    if layout:
        await db.execute(
            "UPDATE videos SET cam_layout=? WHERE id=?", (db.dumps(layout), video["id"])
        )
    return layout


async def load_moment(moment_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = await db.fetch_one("SELECT * FROM moments WHERE id=?", (moment_id,))
    if row is None:
        raise NotFound("Momento no encontrado")
    moment = db.row_to_dict(row)
    video = db.row_to_dict(
        await db.fetch_one("SELECT * FROM videos WHERE id=?", (moment["video_id"],))
    )
    if not video:
        raise NotFound("Video no encontrado")
    return moment, video


async def render_moment_clip(
    moment_id: str, *, layout: str = "", focus_x: float = 0.5
) -> ClipOut:
    """Renderiza (o reutiliza) el vertical 9:16 de un momento."""
    moment, video = await load_moment(moment_id)
    out = render.clip_path(moment_id)
    # Un layout explicito siempre re-renderiza: es la forma de probar encuadres.
    if out.exists() and out.stat().st_size > 4096 and not layout:
        spec = build_render_spec(moment, video)
        cues = render.group_words(
            spec.captions, spec.t_start, spec.t_end,
            max_words=settings.render_words_per_line,
            max_chars=settings.render_chars_per_line,
        )
        return ClipOut(
            moment_id=moment_id, width=render.OUT_W, height=render.OUT_H,
            duration=round(float(moment["t_end"]) - float(moment["t_start"]), 2),
            layout=settings.render_layout, captions=len(cues),
            size_bytes=out.stat().st_size, cached=True,
            download_url=f"/api/moments/{moment_id}/clip",
        )

    spec = build_render_spec(moment, video)
    source = await clip_source(video)
    opts = render.RenderOptions(
        layout=layout,
        # El titulo quemado es el `clip_title` del LLM; sin IA no se quema nada,
        # porque seria las primeras palabras del transcript.
        title=str(moment.get("clip_title") or ""),
        show_title=bool(moment["enriched"]) and bool(moment.get("clip_title")),
        focus_x=focus_x,
        words=spec.captions,
        sfx=await render.plan_sfx(moment),
        music=str(moment.get("music") or ""),
        cam_layout=await cam_layout(video, moment),
    )
    try:
        result = await render.render_clip(source, moment, opts, out=out)
    except CommandFailed as exc:
        log.exception("render de %s fallo", moment_id)
        raise Unavailable(f"El render fallo: {exc}") from exc
    await db.execute(
        "UPDATE moments SET clip_path=? WHERE id=?", (str(result.path), moment_id)
    )
    return ClipOut(
        moment_id=moment_id, width=result.width, height=result.height,
        duration=round(result.duration, 2), layout=result.layout,
        captions=result.captions, size_bytes=result.size_bytes, cached=False,
        sfx=result.sfx, music=result.music, social=result.social,
        download_url=f"/api/moments/{moment_id}/clip",
    )
