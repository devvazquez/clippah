"""Extraccion de fotogramas. Seek remoto con ffmpeg y fallback a descarga completa."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .. import db
from ..config import settings
from ..utils import CommandFailed, ffmpeg_bin, log, run
from .ingest import VideoInfo, download_video, resolve_stream_url

THUMB_WIDTH = 640
FRAME_TIMEOUT_S = 90.0
# Tras 2 fallos de extraccion remota para el mismo video pasamos a FULL_DOWNLOAD.
REMOTE_FAILURE_LIMIT = 2

_remote_failures: dict[str, int] = {}
_video_locks: dict[str, asyncio.Lock] = {}


def _lock_for(video_id: str) -> asyncio.Lock:
    lock = _video_locks.get(video_id)
    if lock is None:
        lock = asyncio.Lock()
        _video_locks[video_id] = lock
    return lock


def thumb_path_for(moment_id: str) -> Path:
    settings.ensure_dirs()
    return settings.thumbs_dir / f"{moment_id}.jpg"


async def _ffmpeg_extract(source: str, t: float, out: Path) -> None:
    """`-ss` va ANTES de `-i`: asi ffmpeg hace seek en el contenedor (remoto incluido)
    en lugar de decodificar el fichero entero desde el principio."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{max(0.0, t):.3f}",
        "-i", source,
        "-frames:v", "1",
        "-vf", f"scale={THUMB_WIDTH}:-2",
        "-q:v", "3",
        str(out),
    ]
    await run(cmd, timeout=FRAME_TIMEOUT_S)
    if not out.exists() or out.stat().st_size < 512:
        raise CommandFailed(cmd, 0, "ffmpeg no produjo un JPEG valido")


def _looks_like_expired(err: str) -> bool:
    low = err.lower()
    return any(tok in low for tok in ("403", "forbidden", "401", "unauthorized", "410"))


async def _refresh_stream_url(video: dict[str, Any]) -> str:
    info = VideoInfo(
        platform=video["platform"],
        ext_id=video["ext_id"],
        url=video["url"],
        duration=float(video.get("duration") or 0.0),
    )
    url, expires = await resolve_stream_url(info)
    if url:
        await db.execute(
            "UPDATE videos SET stream_url=?, stream_url_expires_at=? WHERE id=?",
            (url, expires, video["id"]),
        )
        video["stream_url"] = url
        video["stream_url_expires_at"] = expires
    return url


async def _ensure_local_video(video: dict[str, Any]) -> str:
    """Descarga el mp4 a 480p (una sola vez por video) para poder sacar fotogramas."""
    existing = video.get("video_path")
    if existing and Path(existing).exists():
        return str(existing)
    async with _lock_for(str(video["id"])):
        row = await db.fetch_one("SELECT video_path FROM videos WHERE id=?", (video["id"],))
        cached = row["video_path"] if row else None
        if cached and Path(cached).exists():
            video["video_path"] = cached
            return str(cached)
        info = VideoInfo(
            platform=video["platform"],
            ext_id=video["ext_id"],
            url=video["url"],
            duration=float(video.get("duration") or 0.0),
        )
        log.info("cayendo a FULL_DOWNLOAD para %s", video["url"])
        path = await download_video(info)
        await db.execute(
            "UPDATE videos SET video_path=? WHERE id=?", (str(path), video["id"])
        )
        video["video_path"] = str(path)
        return str(path)


async def extract_frame(video: dict[str, Any], t: float, out: Path) -> Path:
    """Fotograma del video en el segundo `t`. Degrada de stream remoto a fichero local."""
    video_id = str(video["id"])
    local = video.get("video_path")
    force_local = (
        settings.download_mode == "full"
        or _remote_failures.get(video_id, 0) >= REMOTE_FAILURE_LIMIT
        or bool(local and Path(str(local)).exists())
    )

    if not force_local:
        stream_url = str(video.get("stream_url") or "")
        expires = float(video.get("stream_url_expires_at") or 0.0)
        if not stream_url or (expires and expires < time.time()):
            stream_url = await _refresh_stream_url(video)
        if stream_url:
            try:
                await _ffmpeg_extract(stream_url, t, out)
                return out
            except CommandFailed as exc:
                if _looks_like_expired(exc.stderr):
                    # Token HLS caducado: re-resolver y reintentar UNA vez.
                    log.warning("stream URL caducada (%s), re-resolviendo", video_id)
                    fresh = await _refresh_stream_url(video)
                    if fresh:
                        try:
                            await _ffmpeg_extract(fresh, t, out)
                            return out
                        except CommandFailed as exc2:
                            exc = exc2
                _remote_failures[video_id] = _remote_failures.get(video_id, 0) + 1
                log.warning(
                    "extraccion remota fallida (%d/%d): %s",
                    _remote_failures[video_id], REMOTE_FAILURE_LIMIT, exc,
                )

    path = await _ensure_local_video(video)
    await _ffmpeg_extract(path, t, out)
    return out


async def extract_for_moment(video: dict[str, Any], moment_id: str, t: float) -> Path | None:
    out = thumb_path_for(moment_id)
    if out.exists() and out.stat().st_size > 512:
        return out
    try:
        await extract_frame(video, t, out)
    except Exception as exc:  # noqa: BLE001 - un fotograma que falta no tumba el job
        log.warning("no se pudo extraer fotograma de %s en %.1fs: %s", moment_id, t, exc)
        return None
    await db.execute("UPDATE moments SET thumb_path=? WHERE id=?", (str(out), moment_id))
    return out


def reset_failures(video_id: str | None = None) -> None:
    if video_id is None:
        _remote_failures.clear()
    else:
        _remote_failures.pop(video_id, None)


__all__ = [
    "extract_for_moment",
    "extract_frame",
    "reset_failures",
    "thumb_path_for",
]
