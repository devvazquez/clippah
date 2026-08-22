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
from .models import CaptionCue, ClipOut, RenderSpec, SfxCue, Word
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


def rerender_spec(moment: dict[str, Any], video: dict[str, Any]) -> dict[str, Any]:
    """Todo lo que hace falta para volver a quemar este clip en una maquina limpia.

    Se guarda en Supabase junto al clip porque la base local no viaja: esta en
    `backend/data/`, que no se versiona. Sin esto, un re-render pedido desde la interfaz
    solo funciona en la maquina que hizo el analisis, y el turno programado (que arranca
    en un contenedor recien clonado) no puede atenderlo.

    Son los campos de las dos filas que el render lee, no las filas enteras: ni el chat,
    ni las senales, ni el transcript hacen falta para volver a montar el mp4.
    """
    return {
        "video": {
            "id": str(video["id"]),
            "platform": str(video.get("platform") or "twitch"),
            "ext_id": str(video.get("ext_id") or ""),
            "url": str(video.get("url") or ""),
            "title": str(video.get("title") or ""),
            "duration": float(video.get("duration") or 0.0),
            "upload_date": str(video.get("upload_date") or ""),
            # El recorte de las camaras: volver a detectarlo cuesta minuto y medio de
            # video y puede salir distinto, asi que se lleva medido.
            "cam_layout": video.get("cam_layout"),
        },
        "moment": {
            "id": str(moment["id"]),
            "video_id": str(moment["video_id"]),
            "t_start": float(moment["t_start"]),
            "t_end": float(moment["t_end"]),
            "t_peak": float(moment.get("t_peak") or moment["t_start"]),
            "title": str(moment.get("title") or ""),
            "clip_title": str(moment.get("clip_title") or ""),
            "enriched": int(moment.get("enriched") or 0),
            "music": str(moment.get("music") or "ninguna"),
            "sfx_fit": str(moment.get("sfx_fit") or "ninguno"),
            "rank": int(moment.get("rank") or 0),
        },
    }


async def ensure_moment(moment_id: str, spec: dict[str, Any] | None) -> None:
    """Se asegura de que el momento y su video estan en la base local.

    En la maquina que hizo el analisis ya estan. En un contenedor nuevo la base viene
    vacia, y entonces se reconstruyen desde la ficha que se guardo en Supabase: con eso
    el render sigue el mismo camino de siempre, sin ramas paralelas.
    """
    if await db.fetch_one("SELECT id FROM moments WHERE id=?", (moment_id,)):
        return
    if not spec or not spec.get("moment") or not spec.get("video"):
        raise NotFound(
            "Momento no encontrado y el clip no trae la ficha de render: solo se puede "
            "rehacer en la maquina que hizo el analisis"
        )
    v, m = dict(spec["video"]), dict(spec["moment"])
    now = time.time()
    await db.execute(
        """INSERT OR REPLACE INTO videos
               (id, platform, ext_id, url, title, duration, upload_date, cam_layout,
                created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (v["id"], v.get("platform") or "twitch", v.get("ext_id") or "", v.get("url") or "",
         v.get("title") or "", float(v.get("duration") or 0.0), v.get("upload_date") or "",
         v.get("cam_layout"), now),
    )
    await db.execute(
        """INSERT OR REPLACE INTO moments
               (id, job_id, video_id, t_start, t_end, t_peak, title, clip_title,
                enriched, music, sfx_fit, rank, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (m["id"], f"spec-{m['id']}", m["video_id"], float(m["t_start"]), float(m["t_end"]),
         float(m.get("t_peak") or m["t_start"]), m.get("title") or "",
         m.get("clip_title") or "", int(m.get("enriched") or 0),
         m.get("music") or "ninguna", m.get("sfx_fit") or "ninguno",
         int(m.get("rank") or 0), now),
    )
    log.info("momento %s reconstruido desde la ficha de render", moment_id)


def _cues(raw: list[dict[str, Any]]) -> list[render.SubtitleCue]:
    """Frases guardadas -> frases de render, saneando tiempos y texto."""
    out = []
    for c in raw:
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, float(c.get("start") or 0.0))
        end = max(start + 0.2, float(c.get("end") or 0.0))
        out.append(render.SubtitleCue(start=start, end=end, text=text))
    return sorted(out, key=lambda c: c.start)


def _sfx(raw: list[dict[str, Any]]) -> list[SfxCue]:
    """Efectos guardados -> efectos de render, con solo los ficheros que existen."""
    out = []
    for c in raw:
        name = str(c.get("name") or "").strip()
        if not name or not (settings.sfx_dir / name).exists():
            log.warning("efecto desconocido, se ignora: %r", name)
            continue
        gain = c.get("gain_db")
        out.append(SfxCue(
            t=max(0.0, float(c.get("t") or 0.0)),
            name=name,
            gain_db=float(gain) if gain is not None else settings.render_sfx_boom_db,
        ))
    return sorted(out, key=lambda c: c.t)


async def render_moment_clip(
    moment_id: str,
    *,
    layout: str = "",
    focus_x: float = 0.5,
    cues: list[dict[str, Any]] | None = None,
    sfx: list[dict[str, Any]] | None = None,
    music: str | None = None,
) -> ClipOut:
    """Renderiza (o reutiliza) el vertical 9:16 de un momento.

    `cues`, `sfx` y `music` son las ediciones que llegan de la interfaz: sustituyen a lo
    que eligio el modelo y fuerzan el re-render aunque el clip ya estuviera en disco. Una
    lista de efectos vacia significa "ninguno", que no es lo mismo que no tocarlos.
    """
    moment, video = await load_moment(moment_id)
    out = render.clip_path(moment_id)
    edited = cues is not None or sfx is not None or music is not None
    # Un layout explicito o una edicion siempre re-renderizan.
    if out.exists() and out.stat().st_size > 4096 and not layout and not edited:
        spec = build_render_spec(moment, video)
        cached_cues = render.group_words(
            spec.captions, spec.t_start, spec.t_end,
            max_words=settings.render_words_per_line,
            max_chars=settings.render_chars_per_line,
        )
        return ClipOut(
            moment_id=moment_id, width=render.OUT_W, height=render.OUT_H,
            duration=round(float(moment["t_end"]) - float(moment["t_start"]), 2),
            layout=settings.render_layout, captions=len(cached_cues),
            size_bytes=out.stat().st_size, cached=True,
            download_url=f"/api/moments/{moment_id}/clip",
            cues=[
                CaptionCue(text=c.text, start=round(c.start, 3), end=round(c.end, 3))
                for c in cached_cues
            ],
            # Lo que suena en el mp4 que ya esta en disco: sale de lo mismo que salio
            # entonces (el modo elegido por el scorer y el pico), asi que reconstruirlo
            # es fiel y evita que la interfaz muestre "sin efectos" en un clip que si
            # los lleva.
            sfx_cues=await render.plan_sfx(moment),
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
        cues=_cues(cues) if cues is not None else None,
        sfx=_sfx(sfx) if sfx is not None else await render.plan_sfx(moment),
        music=str(moment.get("music") or "") if music is None else music,
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
        cues=[
            CaptionCue(text=c.text, start=round(c.start, 3), end=round(c.end, 3))
            for c in result.cues
        ],
        sfx_cues=list(result.sfx_cues),
    )
