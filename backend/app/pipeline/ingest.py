"""Ingesta: resolucion del enlace, metadata via yt-dlp, audio 16 kHz y URL de stream."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from ..utils import (
    CommandFailed,
    ffmpeg_bin,
    friendly_ytdlp_error,
    log,
    run,
    run_streaming,
    ytdlp_base,
    ytdlp_error_line,
)

ProgressCb = Callable[[float, str], Awaitable[None]]

# Duracion de vida asumida del token de un stream HLS de Twitch (~24 h). Guardamos la
# expiracion para poder re-resolver antes de que ffmpeg empiece a devolver 403.
STREAM_URL_TTL_S = 20 * 3600

TWITCH_PATTERNS = (
    re.compile(r"^https?://(?:www\.|m\.)?twitch\.tv/videos/(\d+)"),
    re.compile(r"^https?://(?:www\.|m\.)?twitch\.tv/[^/]+/v(?:ideo)?/(\d+)"),
)
YOUTUBE_PATTERNS = (
    re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/watch\?(?:.*&)?v=([\w-]{6,})"),
    re.compile(r"^https?://youtu\.be/([\w-]{6,})"),
    re.compile(r"^https?://(?:www\.)?youtube\.com/live/([\w-]{6,})"),
)


class UnsupportedUrl(ValueError):
    pass


class ProbeFailed(RuntimeError):
    """yt-dlp no pudo leer el enlace; el mensaje ya es apto para mostrar al usuario."""


class VodTooLong(ValueError):
    pass


@dataclass(slots=True)
class ResolvedUrl:
    platform: str
    ext_id: str
    url: str


@dataclass(slots=True)
class VideoInfo:
    platform: str
    ext_id: str
    url: str
    title: str = ""
    duration: float = 0.0
    uploader: str = ""
    upload_date: str = ""
    thumbnail: str = ""
    is_live: bool = False
    raw: dict = field(default_factory=dict)


def resolve_url(raw_url: str) -> ResolvedUrl:
    """Normaliza el enlace de entrada. Lanza UnsupportedUrl si no es Twitch/YouTube."""
    url = (raw_url or "").strip()
    if not url:
        raise UnsupportedUrl("La URL esta vacia")
    if not url.startswith("http"):
        url = "https://" + url
    for pat in TWITCH_PATTERNS:
        m = pat.match(url)
        if m:
            vid = m.group(1)
            return ResolvedUrl("twitch", vid, f"https://www.twitch.tv/videos/{vid}")
    for pat in YOUTUBE_PATTERNS:
        m = pat.match(url)
        if m:
            vid = m.group(1)
            return ResolvedUrl("youtube", vid, f"https://www.youtube.com/watch?v={vid}")
    raise UnsupportedUrl(
        "Solo se aceptan VODs de Twitch (twitch.tv/videos/...) o videos de YouTube"
    )


async def probe(resolved: ResolvedUrl) -> VideoInfo:
    """`yt-dlp -J` para obtener metadata sin descargar nada."""
    cmd = [*ytdlp_base(), "-J", resolved.url]
    try:
        res = await run(cmd, timeout=180)
    except CommandFailed as exc:
        hint = friendly_ytdlp_error(exc.stderr)
        raise ProbeFailed(
            hint or f"No se pudo leer el enlace: {ytdlp_error_line(exc.stderr)}"
        ) from exc
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp devolvio una respuesta ilegible: {exc}") from exc

    duration = float(data.get("duration") or 0.0)
    info = VideoInfo(
        platform=resolved.platform,
        ext_id=str(data.get("id") or resolved.ext_id),
        url=resolved.url,
        title=str(data.get("title") or "Sin titulo"),
        duration=duration,
        uploader=str(data.get("uploader") or data.get("channel") or ""),
        upload_date=str(data.get("upload_date") or ""),
        thumbnail=str(data.get("thumbnail") or ""),
        is_live=bool(data.get("is_live")),
        raw=data,
    )
    if info.is_live:
        raise VodTooLong("El directo aun esta en curso; analiza el VOD cuando termine")
    if duration <= 0:
        raise VodTooLong("yt-dlp no reporta duracion para este enlace")
    max_s = settings.max_vod_hours * 3600
    if duration > max_s:
        raise VodTooLong(
            f"El VOD dura {duration / 3600:.1f} h y el limite configurado es "
            f"{settings.max_vod_hours:g} h (MAX_VOD_HOURS)"
        )
    return info


_PCT = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


async def _stream_or_explain(cmd: list[str]):
    """`run_streaming` traduciendo los fallos conocidos de yt-dlp a algo accionable."""
    try:
        async for line in run_streaming(cmd, timeout=None):
            yield line
    except CommandFailed as exc:
        hint = friendly_ytdlp_error(exc.stderr)
        if hint:
            raise ProbeFailed(hint) from exc
        raise


async def download_audio(
    info: VideoInfo, *, progress: ProgressCb | None = None
) -> Path:
    """Descarga solo el audio y lo normaliza a WAV 16 kHz mono (lo que necesita Whisper)."""
    settings.ensure_dirs()
    out_base = settings.media_dir / f"{info.platform}-{info.ext_id}"
    wav = out_base.with_suffix(".wav")
    if wav.exists() and wav.stat().st_size > 1024:
        log.info("audio ya descargado: %s", wav)
        if progress:
            await progress(1.0, "Audio ya en cache")
        return wav

    cmd = [
        *ytdlp_base(),
        "-f", "bestaudio/best",
        "-x",
        "--audio-format", "wav",
        "--postprocessor-args", "ffmpeg:-ar 16000 -ac 1",
        "--newline",
        "--no-part",
        "-o", str(out_base) + ".%(ext)s",
        info.url,
    ]
    last = -1.0
    async for line in _stream_or_explain(cmd):
        m = _PCT.search(line)
        if m and progress:
            pct = float(m.group(1)) / 100.0
            if pct - last >= 0.02:
                last = pct
                await progress(pct, f"Descargando audio ({pct * 100:.0f}%)")
        elif "Destination" in line or "ExtractAudio" in line:
            log.debug(line)

    if not wav.exists():
        # yt-dlp puede dejar la extension original si el postprocesado falla
        for cand in sorted(settings.media_dir.glob(f"{out_base.name}.*")):
            if cand.suffix.lower() in {".wav", ".m4a", ".mp4", ".webm", ".opus", ".mp3"}:
                await _transcode_to_wav(cand, wav)
                if cand != wav:
                    cand.unlink(missing_ok=True)
                break
    if not wav.exists():
        raise RuntimeError("No se pudo obtener el audio del VOD")
    return wav


async def _transcode_to_wav(src: Path, dst: Path) -> None:
    await run(
        [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-vn", "-ar", "16000", "-ac", "1", str(dst),
        ],
        timeout=None,
    )


# Por que fallo la ultima resolucion de stream. Cuando no se puede resolver, el error que
# ve quien pidio el clip decia solo "no se pudo resolver el stream", y eso no se puede
# diagnosticar: no distingue un VOD borrado de un bloqueo de Twitch a la IP del
# contenedor. Aqui se guarda lo que dijo yt-dlp para poder contarlo.
last_stream_error = ""


async def resolve_stream_url(info: VideoInfo, *, max_height: int = 720) -> tuple[str, float]:
    """URL directa del stream de video para hacer seek remoto con ffmpeg.

    Devuelve (url, expires_at_epoch). Cadena vacia si no se puede resolver, y entonces
    `last_stream_error` cuenta por que.
    """
    global last_stream_error
    fmts = [
        f"best[height<={max_height}][ext=mp4]",
        f"best[height<={max_height}]",
        "best",
    ]
    fallos = []
    for fmt in fmts:
        cmd = [*ytdlp_base(), "-g", "-f", fmt, info.url]
        try:
            res = await run(cmd, timeout=180)
        except CommandFailed as exc:
            log.warning("yt-dlp -g fallo con -f %s: %s", fmt, exc)
            # De la parrafada de yt-dlp interesa la ultima linea, que es el motivo.
            motivo = str(exc).strip().splitlines()[-1][:200]
            fallos.append(f"{fmt}: {motivo}")
            continue
        urls = [ln.strip() for ln in res.stdout.splitlines() if ln.strip().startswith("http")]
        if urls:
            last_stream_error = ""
            return urls[0], time.time() + STREAM_URL_TTL_S
        fallos.append(f"{fmt}: yt-dlp no devolvio ninguna URL")
    last_stream_error = " | ".join(fallos)
    return "", 0.0


async def download_video(info: VideoInfo, *, progress: ProgressCb | None = None) -> Path:
    """Modo FULL_DOWNLOAD: mp4 a 480p como fallback para extraer fotogramas."""
    settings.ensure_dirs()
    out_base = settings.media_dir / f"{info.platform}-{info.ext_id}-480p"
    for ext in (".mp4", ".mkv", ".webm"):
        cand = out_base.with_suffix(ext)
        if cand.exists() and cand.stat().st_size > 1024:
            return cand

    cmd = [
        *ytdlp_base(),
        "-f", "best[height<=480][ext=mp4]/best[height<=480]/best",
        "--newline", "--no-part",
        "-o", str(out_base) + ".%(ext)s",
        info.url,
    ]
    last = -1.0
    async for line in _stream_or_explain(cmd):
        m = _PCT.search(line)
        if m and progress:
            pct = float(m.group(1)) / 100.0
            if pct - last >= 0.02:
                last = pct
                await progress(pct, f"Descargando video 480p ({pct * 100:.0f}%)")
    for ext in (".mp4", ".mkv", ".webm"):
        cand = out_base.with_suffix(ext)
        if cand.exists():
            return cand
    raise RuntimeError("No se pudo descargar el video en modo FULL_DOWNLOAD")
