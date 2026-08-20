"""Orquestador: cola in-process, ejecucion del pipeline y emision de eventos SSE."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .. import db
from ..config import settings
from ..events import hub
from ..models import CATEGORIES
from ..utils import hhmmss, log
from . import frames
from .candidates import Candidate, select_candidates
from .chat import ChatMessage, ChatUnavailable, fetch_chat, fetch_twitch_chat_via_cli
from .ingest import (
    UnsupportedUrl,
    VideoInfo,
    VodTooLong,
    download_audio,
    probe,
    resolve_stream_url,
    resolve_url,
)
from .score import Fragment, ScoringEngine, finalize
from .signals import compute_signals
from .transcribe import TranscriptionEngine, refine_bounds

# Reparto del progreso por etapa (limites superiores).
STAGE_BOUNDS = {
    "ingest": (0.02, 0.30),
    "chat": (0.30, 0.45),
    "signals": (0.45, 0.50),
    "candidates": (0.50, 0.52),
    "transcribe": (0.52, 0.85),
    "score": (0.85, 0.92),
    "frames": (0.92, 0.99),
}


class JobCancelled(Exception):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class JobContext:
    """Estado mutable de un job en ejecucion + emision de eventos con progreso monotono."""

    def __init__(self, job_id: str, url: str) -> None:
        self.job_id = job_id
        self.url = url
        self.progress = 0.0
        self.warnings: list[str] = []
        self.providers: dict[str, Any] = {}
        self.video_id: str | None = None

    async def emit(
        self, stage: str, progress: float | None = None, message: str = "", **extra: Any
    ) -> None:
        if progress is not None:
            # El progreso nunca retrocede: el frontend lo pinta directo.
            self.progress = max(self.progress, min(1.0, progress))
        payload: dict[str, Any] = {"stage": stage, "progress": round(self.progress, 4)}
        if message:
            payload["message"] = message
        payload.update(extra)
        await hub.publish(self.job_id, payload)
        if stage not in ("warning", "error"):
            await db.execute(
                "UPDATE jobs SET stage=?, progress=?, message=?, updated_at=? WHERE id=?",
                (stage, self.progress, message, time.time(), self.job_id),
            )

    async def stage_progress(self, stage: str, fraction: float, message: str = "") -> None:
        lo, hi = STAGE_BOUNDS.get(stage, (0.0, 1.0))
        await self.emit(stage, lo + (hi - lo) * max(0.0, min(1.0, fraction)), message)

    async def warn(self, message: str) -> None:
        if message in self.warnings:
            return
        self.warnings.append(message)
        await db.execute(
            "UPDATE jobs SET warnings=?, updated_at=? WHERE id=?",
            (db.dumps(self.warnings), time.time(), self.job_id),
        )
        await hub.publish(self.job_id, {"stage": "warning", "message": message})


# --------------------------------------------------------------------------- helpers


async def _upsert_video(info: VideoInfo) -> str:
    row = await db.fetch_one(
        "SELECT id FROM videos WHERE platform=? AND ext_id=?", (info.platform, info.ext_id)
    )
    now = time.time()
    if row:
        video_id = str(row["id"])
        await db.execute(
            """UPDATE videos SET url=?, title=?, duration=?, uploader=?, upload_date=?,
                   thumbnail=? WHERE id=?""",
            (
                info.url, info.title, info.duration, info.uploader,
                info.upload_date, info.thumbnail, video_id,
            ),
        )
        return video_id
    video_id = new_id("vid")
    await db.execute(
        """INSERT INTO videos (id, platform, ext_id, url, title, duration, uploader,
               upload_date, thumbnail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            video_id, info.platform, info.ext_id, info.url, info.title, info.duration,
            info.uploader, info.upload_date, info.thumbnail, now,
        ),
    )
    return video_id


async def _store_chat(video_id: str, messages: list[ChatMessage]) -> None:
    await db.execute("DELETE FROM chat_messages WHERE video_id=?", (video_id,))
    await db.executemany(
        "INSERT INTO chat_messages (video_id, t, user, text, emotes) VALUES (?, ?, ?, ?, ?)",
        [(video_id, m.t, m.user, m.text, db.dumps(m.emotes)) for m in messages],
    )


async def _load_chat(video_id: str) -> list[ChatMessage]:
    rows = await db.fetch_all(
        "SELECT t, user, text, emotes FROM chat_messages WHERE video_id=? ORDER BY t", (video_id,)
    )
    return [
        ChatMessage(
            t=float(r["t"]),
            user=str(r["user"] or "anon"),
            text=str(r["text"] or ""),
            emotes=db.loads(r["emotes"], []) or [],
        )
        for r in rows
    ]


# ------------------------------------------------------------------------- pipeline


async def run_pipeline(ctx: JobContext) -> int:
    """Ejecuta el pipeline completo. Devuelve el numero de momentos guardados."""
    # ---------------------------------------------------------------- 1. ingesta
    await ctx.emit("ingest", 0.02, "Resolviendo el enlace")
    resolved = resolve_url(ctx.url)
    info = await probe(resolved)
    video_id = await _upsert_video(info)
    ctx.video_id = video_id
    await db.execute(
        "UPDATE jobs SET video_id=?, updated_at=? WHERE id=?",
        (video_id, time.time(), ctx.job_id),
    )
    await ctx.emit(
        "ingest", 0.05,
        f"{info.title} - {hhmmss(info.duration)}",
        video={
            "id": video_id, "platform": info.platform, "title": info.title,
            "duration": info.duration, "uploader": info.uploader,
            "thumbnail": info.thumbnail,
        },
    )

    async def audio_progress(pct: float, message: str) -> None:
        await ctx.stage_progress("ingest", 0.15 + 0.85 * pct, message)

    audio_path = await download_audio(info, progress=audio_progress)
    await db.execute(
        "UPDATE videos SET audio_path=? WHERE id=?", (str(audio_path), video_id)
    )

    if settings.download_mode == "stream_seek":
        stream_url, expires = await resolve_stream_url(info)
        if stream_url:
            await db.execute(
                "UPDATE videos SET stream_url=?, stream_url_expires_at=? WHERE id=?",
                (stream_url, expires, video_id),
            )
        else:
            await ctx.warn(
                "No se pudo resolver la URL del stream; los fotogramas se sacaran de una "
                "descarga a 480p."
            )

    # ------------------------------------------------------------------- 2. chat
    await ctx.stage_progress("chat", 0.05, "Descargando el chat")
    messages: list[ChatMessage] = []
    chat_available = False
    try:
        messages = await fetch_chat(
            info.platform, info.ext_id, info.url, info.duration,
            progress=lambda pct, msg: ctx.stage_progress("chat", 0.05 + 0.9 * pct, msg),
        )
        chat_available = True
    except ChatUnavailable as exc:
        if info.platform == "twitch":
            try:
                messages = await fetch_twitch_chat_via_cli(info.ext_id)
                chat_available = True
            except ChatUnavailable as exc2:
                log.info("fallback twitch-dl tampoco funciono: %s", exc2)
        if not chat_available:
            await ctx.warn(f"Chat no disponible: {exc} El analisis seguira solo con audio.")
    except Exception as exc:  # noqa: BLE001 - el chat es opcional por diseno
        await ctx.warn(f"Chat no disponible ({exc}). El analisis seguira solo con audio.")

    if chat_available:
        await _store_chat(video_id, messages)
        pretty = f"{len(messages):,}".replace(",", ".")
        await ctx.stage_progress("chat", 1.0, f"{pretty} mensajes")
    else:
        await ctx.stage_progress("chat", 1.0, "Sin chat: solo audio")
    await db.execute(
        "UPDATE jobs SET chat_available=?, chat_messages=?, updated_at=? WHERE id=?",
        (1 if chat_available else 0, len(messages), time.time(), ctx.job_id),
    )

    # ---------------------------------------------------------------- 3. senales
    await ctx.stage_progress("signals", 0.2, "Analizando audio y chat")
    signals = await asyncio.to_thread(
        compute_signals,
        duration=info.duration,
        audio_path=audio_path,
        messages=messages if chat_available else None,
    )
    if not signals.audio_available and not signals.chat_available:
        raise RuntimeError("Sin audio analizable ni chat: no hay nada que puntuar")
    await ctx.stage_progress("signals", 1.0, "Senales calculadas")

    # ------------------------------------------------------------- 4. candidatos
    cands: list[Candidate] = select_candidates(signals)
    if not cands:
        raise RuntimeError("No se encontro ningun pico de actividad en este VOD")
    await ctx.stage_progress("candidates", 1.0, f"{len(cands)} candidatos")

    # ---------------------------------------------------------- 5. transcripcion
    transcriber = TranscriptionEngine(on_warning=ctx.warn)
    provider = await transcriber.prepare()
    ctx.providers["transcriber"] = provider

    fragments: list[Fragment] = []
    total = len(cands)
    with tempfile.TemporaryDirectory(prefix="clipper-win-") as tmp:
        work_dir = Path(tmp)
        for i, cand in enumerate(cands):
            label = transcriber.provider if transcriber.available else "sin transcripcion"
            await ctx.stage_progress(
                "transcribe", i / max(1, total), f"Transcribiendo {i + 1}/{total} ({label})"
            )
            win_start = max(0.0, cand.t_start - 8.0)
            win_end = min(info.duration, cand.t_end + 5.0)
            transcript, used = await transcriber.transcribe_window(
                audio_path, win_start, win_end, work_dir=work_dir, tag=str(i)
            )
            bounds = refine_bounds(
                transcript.words, cand.t_peak, cand.t_start, cand.t_end, info.duration
            )
            words = [
                {"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)}
                for w in transcript.words
                if bounds.t_start - 0.5 <= w.start <= bounds.t_end + 0.5
            ]
            text = " ".join(w["text"] for w in words).strip() or transcript.text.strip()
            fragments.append(
                Fragment(
                    id=new_id("mom"),
                    t_start=bounds.t_start,
                    t_end=bounds.t_end,
                    t_peak=cand.t_peak,
                    signal_score=cand.signal_score,
                    chat_z=cand.chat_z,
                    audio_z=cand.audio_z,
                    unique_users=cand.unique_users,
                    msg_count=cand.msg_count,
                    combo=cand.combo,
                    transcript=text,
                    language=transcript.language,
                    words=words,
                )
            )
            if used != "none":
                ctx.providers["transcriber"] = used
    await ctx.stage_progress("transcribe", 1.0, f"{total} fragmentos transcritos")

    # ----------------------------------------------------------- 6. puntuacion
    scorer = ScoringEngine(on_warning=ctx.warn)
    scorer_provider = await scorer.prepare()
    ctx.providers["scorer"] = scorer_provider
    await ctx.stage_progress(
        "score", 0.2,
        "Puntuando con Gemini" if scorer_provider == "gemini" else "Puntuando (modo heuristico)",
    )
    scores, enriched = await scorer.score(fragments, chat_available=chat_available)
    rows = finalize(fragments, scores)
    if not rows:
        # El LLM descarto todo: nos quedamos con los mejores por senal para no
        # devolver una pantalla vacia.
        await ctx.warn(
            "El modelo descarto todos los candidatos; se muestran los mejores por senal."
        )
        for s in scores:
            s.worth_clipping = True
        rows = finalize(fragments, scores)
    await ctx.stage_progress("score", 1.0, f"{len(rows)} momentos")

    # ----------------------------------------------------------- 7. fotogramas
    video_row = db.row_to_dict(await db.fetch_one("SELECT * FROM videos WHERE id=?", (video_id,)))
    now = time.time()
    await db.execute("DELETE FROM moments WHERE job_id=?", (ctx.job_id,))
    for rank, row in enumerate(rows):
        await db.execute(
            """INSERT INTO moments (id, job_id, video_id, t_start, t_end, t_peak, title,
                   description, category, final_score, signal_score, clip_score, chat_z,
                   audio_z, unique_users, msg_count, combo, transcript, words, language,
                   enriched, rank, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["id"], ctx.job_id, video_id, row["t_start"], row["t_end"], row["t_peak"],
                row["title"], row["description"],
                row["category"] if row["category"] in CATEGORIES else "otro",
                row["final_score"], row["signal_score"], row["clip_score"], row["chat_z"],
                row["audio_z"], row["unique_users"], row["msg_count"],
                1 if row["combo"] else 0, row["transcript"], db.dumps(row["words"]),
                row["language"], 1 if enriched else 0, rank, now,
            ),
        )

    for i, row in enumerate(rows):
        await ctx.stage_progress(
            "frames", i / max(1, len(rows)), f"Extrayendo fotogramas {i + 1}/{len(rows)}"
        )
        await frames.extract_for_moment(video_row, row["id"], row["t_peak"])
    await ctx.stage_progress("frames", 1.0, "Fotogramas listos")

    await db.execute(
        "UPDATE jobs SET enriched=?, transcribed=?, providers=?, updated_at=? WHERE id=?",
        (
            1 if enriched else 0,
            1 if transcriber.available else 0,
            db.dumps(ctx.providers),
            time.time(),
            ctx.job_id,
        ),
    )

    # Limpieza: el WAV de un VOD de 6 h ocupa ~700 MB.
    if not settings.keep_media:
        with contextlib.suppress(OSError):
            Path(audio_path).unlink(missing_ok=True)
            await db.execute("UPDATE videos SET audio_path=NULL WHERE id=?", (video_id,))
    return len(rows)


# ---------------------------------------------------------------------- runner


class JobRunner:
    """Cola in-process con `asyncio`: nada de Redis/Celery en una app local."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._running: dict[str, asyncio.Task] = {}
        self._cancelled: set[str] = set()

    async def start(self) -> None:
        for i in range(max(1, settings.max_concurrent_jobs)):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"clipper-worker-{i}"))
        # Jobs que quedaron a medias por un reinicio del backend.
        rows = await db.fetch_all(
            "SELECT id FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
        )
        for row in rows:
            await db.execute(
                "UPDATE jobs SET status='queued', updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            await self._queue.put(str(row["id"]))
        if rows:
            log.info("reencolados %d jobs pendientes", len(rows))

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in list(self._running.values()):
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def submit(self, job_id: str) -> None:
        await self._queue.put(job_id)

    def is_running(self, job_id: str) -> bool:
        return job_id in self._running

    async def cancel(self, job_id: str) -> bool:
        self._cancelled.add(job_id)
        task = self._running.get(job_id)
        if task:
            task.cancel()
            return True
        return False

    async def _worker(self, index: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id in self._cancelled:
                    self._cancelled.discard(job_id)
                    continue
                task = asyncio.create_task(self._run(job_id), name=f"job-{job_id}")
                self._running[job_id] = task
                try:
                    await task
                except asyncio.CancelledError:
                    log.info("job %s cancelado", job_id)
                finally:
                    self._running.pop(job_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - el worker no puede morir
                log.exception("worker %d fallo procesando %s", index, job_id)
            finally:
                self._queue.task_done()

    async def _run(self, job_id: str) -> None:
        row = await db.fetch_one("SELECT url, status FROM jobs WHERE id=?", (job_id,))
        if row is None:
            return
        if str(row["status"]) in ("cancelled", "done"):
            return
        ctx = JobContext(job_id, str(row["url"]))
        await db.execute(
            "UPDATE jobs SET status='running', updated_at=? WHERE id=?", (time.time(), job_id)
        )
        try:
            count = await run_pipeline(ctx)
        except asyncio.CancelledError:
            await db.execute(
                "UPDATE jobs SET status='cancelled', message='Cancelado', updated_at=?, "
                "finished_at=? WHERE id=?",
                (time.time(), time.time(), job_id),
            )
            await hub.publish(job_id, {"stage": "cancelled", "message": "Job cancelado"})
            raise
        except (UnsupportedUrl, VodTooLong) as exc:
            await self._fail(job_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - cualquier fallo se reporta al cliente
            log.exception("job %s fallo", job_id)
            await self._fail(job_id, f"{type(exc).__name__}: {exc}")
        else:
            await db.execute(
                "UPDATE jobs SET status='done', stage='done', progress=1.0, message=?, "
                "updated_at=?, finished_at=? WHERE id=?",
                (f"{count} momentos", time.time(), time.time(), job_id),
            )
            await hub.publish(
                job_id, {"stage": "done", "progress": 1.0, "moments": count}
            )

    async def _fail(self, job_id: str, message: str) -> None:
        await db.execute(
            "UPDATE jobs SET status='error', error=?, message=?, updated_at=?, finished_at=? "
            "WHERE id=?",
            (message, message, time.time(), time.time(), job_id),
        )
        await hub.publish(job_id, {"stage": "error", "message": message})


runner = JobRunner()
