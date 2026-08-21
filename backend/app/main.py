"""FastAPI: rutas /api, SSE de progreso y stub de render."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from . import db
from .config import settings
from .events import hub
from .models import (
    HealthOut,
    JobCreate,
    JobCreated,
    JobList,
    JobListItem,
    JobOut,
    MomentOut,
    MomentSignals,
    ProviderHealth,
    RenderSpec,
    VideoOut,
    Word,
)
from .pipeline import frames
from .pipeline.ingest import ProbeFailed, UnsupportedUrl, VodTooLong, probe, resolve_url
from .pipeline.orchestrator import new_id, runner
from .providers.gemini import GeminiScorer
from .providers.groq import GroqTranscriber
from .utils import have_faster_whisper, have_ffmpeg, have_ytdlp, log

SSE_HEARTBEAT_S = 15.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    settings.ensure_dirs()
    await db.connect()
    await runner.start()
    log.info("clipper backend listo (data_dir=%s)", settings.data_dir.resolve())
    try:
        yield
    finally:
        await runner.stop()
        await db.close()


app = FastAPI(title="clipper", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")


# ------------------------------------------------------------------ serializacion


def _video_out(row: dict[str, Any] | None) -> VideoOut | None:
    if not row:
        return None
    return VideoOut(
        id=str(row["id"]),
        platform=row["platform"],
        ext_id=str(row["ext_id"]),
        url=str(row["url"]),
        title=str(row.get("title") or ""),
        duration=float(row.get("duration") or 0.0),
        uploader=str(row.get("uploader") or ""),
        upload_date=str(row.get("upload_date") or ""),
        thumbnail=str(row.get("thumbnail") or ""),
    )


def _moment_out(row: dict[str, Any]) -> MomentOut:
    return MomentOut(
        id=str(row["id"]),
        video_id=str(row["video_id"]),
        t_start=float(row["t_start"]),
        t_end=float(row["t_end"]),
        t_peak=float(row["t_peak"]),
        duration=round(float(row["t_end"]) - float(row["t_start"]), 2),
        title=str(row["title"]),
        description=str(row["description"]),
        category=row["category"],
        final_score=float(row["final_score"]),
        signals=MomentSignals(
            chat_z=round(float(row["chat_z"]), 2),
            audio_z=round(float(row["audio_z"]), 2),
            unique_users=int(row["unique_users"]),
            msg_count=int(row["msg_count"]),
            combo=bool(row["combo"]),
        ),
        transcript=str(row["transcript"] or ""),
        enriched=bool(row["enriched"]),
        thumbnail_url=f"/api/moments/{row['id']}/thumbnail",
        source=row.get("source") or "signals",
        vision_note=str(row.get("vision_note") or ""),
        hook=str(row.get("hook") or ""),
    )


async def _job_out(job: dict[str, Any]) -> JobOut:
    video = db.row_to_dict(
        await db.fetch_one("SELECT * FROM videos WHERE id=?", (job.get("video_id"),))
    ) if job.get("video_id") else {}
    moments = await db.fetch_all(
        "SELECT * FROM moments WHERE job_id=? ORDER BY rank", (job["id"],)
    )
    return JobOut(
        id=str(job["id"]),
        url=str(job["url"]),
        status=job["status"],
        stage=str(job.get("stage") or ""),
        progress=float(job.get("progress") or 0.0),
        message=str(job.get("message") or ""),
        error=job.get("error"),
        chat_available=bool(job.get("chat_available")),
        chat_messages=int(job.get("chat_messages") or 0),
        enriched=bool(job.get("enriched")),
        transcribed=bool(job.get("transcribed")),
        warnings=db.loads(job.get("warnings"), []) or [],
        providers=db.loads(job.get("providers"), {}) or {},
        created_at=float(job.get("created_at") or 0.0),
        updated_at=float(job.get("updated_at") or 0.0),
        finished_at=job.get("finished_at"),
        video=_video_out(video or None),
        moments=[_moment_out(db.row_to_dict(m)) for m in moments],
    )


# ------------------------------------------------------------------------- rutas


@api.post("/jobs", response_model=JobCreated, status_code=201)
async def create_job(payload: JobCreate) -> JobCreated:
    try:
        resolved = resolve_url(payload.url)
    except UnsupportedUrl as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        info = await probe(resolved)
    except (VodTooLong, ProbeFailed) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - yt-dlp puede fallar por mil motivos
        raise HTTPException(
            status_code=502, detail=f"No se pudo leer el VOD: {exc}"
        ) from exc

    from .pipeline.orchestrator import _upsert_video

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
    video_row = db.row_to_dict(await db.fetch_one("SELECT * FROM videos WHERE id=?", (video_id,)))
    return JobCreated(job_id=job_id, video=_video_out(video_row))


@api.get("/jobs", response_model=JobList)
async def list_jobs(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)
) -> JobList:
    total = int(await db.fetch_value("SELECT COUNT(*) FROM jobs", (), 0) or 0)
    rows = await db.fetch_all(
        """SELECT j.*, v.title, v.thumbnail, v.uploader, v.duration,
                  (SELECT COUNT(*) FROM moments m WHERE m.job_id = j.id) AS moment_count
           FROM jobs j LEFT JOIN videos v ON v.id = j.video_id
           ORDER BY j.created_at DESC LIMIT ? OFFSET ?""",
        (limit, offset),
    )
    items = [
        JobListItem(
            id=str(r["id"]),
            url=str(r["url"]),
            status=r["status"],
            stage=str(r["stage"] or ""),
            progress=float(r["progress"] or 0.0),
            title=str(r["title"] or ""),
            thumbnail=str(r["thumbnail"] or ""),
            uploader=str(r["uploader"] or ""),
            duration=float(r["duration"] or 0.0),
            moments=int(r["moment_count"] or 0),
            created_at=float(r["created_at"] or 0.0),
        )
        for r in rows
    ]
    return JobList(items=items, total=total, limit=limit, offset=offset)


@api.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str) -> JobOut:
    row = await db.fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return await _job_out(db.row_to_dict(row))


@api.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = Query(0, ge=0)):
    """SSE con el progreso. Reenvia el historial y luego los eventos en vivo.

    Asi, si el navegador se cierra y se vuelve a `/job/{id}`, el cliente recupera todo
    el progreso sin hacer polling.
    """
    row = await db.fetch_one("SELECT id, status FROM jobs WHERE id=?", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")

    last_header = request.headers.get("last-event-id")
    start_seq = after
    if last_header and last_header.isdigit():
        start_seq = max(start_seq, int(last_header))

    # Suscribirse ANTES de leer el historial para no perder eventos intermedios.
    queue = hub.subscribe(job_id)

    async def stream() -> AsyncIterator[bytes]:
        last_seq = start_seq
        try:
            for event in await hub.history(job_id, start_seq):
                last_seq = max(last_seq, int(event.get("seq", 0)))
                yield _sse(event)
            terminal = {"done", "error", "cancelled"}
            job_status = str(row["status"])
            if job_status in ("done", "error", "cancelled"):
                yield _sse({"stage": job_status, "progress": 1.0, "seq": last_seq + 1})
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_S)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                seq = int(event.get("seq", 0))
                if seq <= last_seq:
                    continue
                last_seq = seq
                yield _sse(event)
                if event.get("stage") in terminal:
                    return
        finally:
            hub.unsubscribe(job_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: dict[str, Any]) -> bytes:
    seq = event.get("seq")
    prefix = f"id: {seq}\n" if seq is not None else ""
    data = json.dumps(event, ensure_ascii=False)
    return f"{prefix}data: {data}\n\n".encode()


@api.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    job = db.row_to_dict(row)
    await runner.cancel(job_id)
    if job["status"] in ("queued", "running"):
        await db.execute(
            "UPDATE jobs SET status='cancelled', message='Cancelado', updated_at=?, "
            "finished_at=? WHERE id=?",
            (time.time(), time.time(), job_id),
        )
        await hub.publish(job_id, {"stage": "cancelled", "message": "Job cancelado"})

    removed = await _cleanup_job_files(job_id, job.get("video_id"))
    return {"ok": True, "job_id": job_id, "removed_files": removed}


async def _cleanup_job_files(job_id: str, video_id: str | None) -> int:
    removed = 0
    rows = await db.fetch_all("SELECT id, thumb_path FROM moments WHERE job_id=?", (job_id,))
    for r in rows:
        for candidate in (r["thumb_path"], str(frames.thumb_path_for(str(r["id"])))):
            if candidate and Path(candidate).exists():
                with contextlib.suppress(OSError):
                    Path(candidate).unlink()
                    removed += 1
    if video_id:
        other = int(
            await db.fetch_value(
                "SELECT COUNT(*) FROM jobs WHERE video_id=? AND id<>? AND status<>'cancelled'",
                (video_id, job_id),
                0,
            )
            or 0
        )
        if other == 0:
            vid = db.row_to_dict(
                await db.fetch_one(
                    "SELECT audio_path, video_path FROM videos WHERE id=?", (video_id,)
                )
            )
            for key in ("audio_path", "video_path"):
                path = vid.get(key)
                if path and Path(path).exists():
                    with contextlib.suppress(OSError):
                        Path(path).unlink()
                        removed += 1
            await db.execute(
                "UPDATE videos SET audio_path=NULL, video_path=NULL WHERE id=?", (video_id,)
            )
            frames.reset_failures(video_id)
    return removed


@api.get("/moments/{moment_id}/thumbnail")
async def moment_thumbnail(moment_id: str) -> FileResponse:
    row = await db.fetch_one("SELECT * FROM moments WHERE id=?", (moment_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Momento no encontrado")
    moment = db.row_to_dict(row)
    path = frames.thumb_path_for(moment_id)
    if not (path.exists() and path.stat().st_size > 512):
        video = db.row_to_dict(
            await db.fetch_one("SELECT * FROM videos WHERE id=?", (moment["video_id"],))
        )
        if not video:
            raise HTTPException(status_code=404, detail="Video no encontrado")
        result = await frames.extract_for_moment(video, moment_id, float(moment["t_peak"]))
        if result is None:
            raise HTTPException(status_code=503, detail="No se pudo extraer el fotograma")
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"}
    )


def _build_render_spec(moment: dict[str, Any], video: dict[str, Any]) -> RenderSpec:
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


@api.post("/moments/{moment_id}/render", status_code=501, response_model=RenderSpec)
async def render_moment(moment_id: str) -> RenderSpec:
    """STUB: el render del clip esta fuera de alcance. El `RenderSpec` si se construye,
    porque es el contrato de la fase 2."""
    row = await db.fetch_one("SELECT * FROM moments WHERE id=?", (moment_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Momento no encontrado")
    moment = db.row_to_dict(row)
    video = db.row_to_dict(
        await db.fetch_one("SELECT * FROM videos WHERE id=?", (moment["video_id"],))
    )
    spec = _build_render_spec(moment, video)
    raise HTTPException(
        status_code=501,
        detail={"message": "Render no implementado todavia", "spec": spec.model_dump()},
    )


@api.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    groq = GroqTranscriber()
    gemini = GeminiScorer()
    providers: list[ProviderHealth] = []

    groq_req, groq_units = await groq.limiter.usage()
    providers.append(
        ProviderHealth(
            name="groq",
            configured=groq.configured,
            available=groq.configured and await groq.limiter.has_room(10.0),
            requests_today=groq_req,
            requests_limit=settings.groq_rpd,
            units_today=round(groq_units, 1),
            units_limit=float(settings.groq_asd),
            note="Transcripcion (segundos de audio/dia)" if groq.configured else "Sin GROQ_API_KEY",
        )
    )
    gem_req, gem_units = await gemini.limiter.usage()
    providers.append(
        ProviderHealth(
            name="gemini",
            configured=gemini.configured,
            available=gemini.configured and await gemini.limiter.has_room(1.0),
            requests_today=gem_req,
            requests_limit=settings.gemini_rpd,
            units_today=round(gem_units, 1),
            units_limit=None,
            note="Titulos y puntuacion" if gemini.configured else "Sin GEMINI_API_KEY",
        )
    )
    fw = have_faster_whisper()
    providers.append(
        ProviderHealth(
            name="faster-whisper",
            configured=fw,
            available=fw,
            note=f"Modelo {settings.whisper_model}" if fw else "No instalado (extra 'local')",
        )
    )

    if groq.configured and gemini.configured:
        mode = "cloud"
    elif groq.configured or gemini.configured:
        mode = "hybrid"
    else:
        mode = "local"
    transcriber = "groq" if groq.configured else ("local" if fw else "none")
    vision = settings.vision_enabled and gemini.configured
    return HealthOut(
        mode=mode,
        vision=vision,
        ffmpeg=have_ffmpeg(),
        ytdlp=have_ytdlp(),
        faster_whisper=fw,
        transcriber=transcriber,
        scorer="gemini" if gemini.configured else "heuristic",
        providers=providers,
    )


app.include_router(api)


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "clipper", "docs": "/docs", "api": "/api/health"}
