"""Proponente visual: muestrea fotogramas y le pregunta a Gemini cuales muestran algo.

Las senales de audio y chat solo encuentran momentos con *reaccion*. Un hito visual
silencioso -- equipo raro conseguido, construccion terminada, un marcador alto -- no
levanta el audio ni el chat, asi que con el pipeline reactivo nunca llega a ser
candidato y el LLM no lo ve nunca (solo filtra y describe lo que las senales proponen).
Este modulo abre esa puerta: propone momentos mirando la pantalla.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from ..providers.base import ScorerUnavailable, VisionContext, VisualHit
from ..providers.gemini import GeminiScorer
from ..providers.ratelimit import QuotaExhausted
from ..utils import CommandFailed, ffmpeg_bin, log

ProgressCb = Callable[[float, str], Awaitable[None]]
WarnCb = Callable[[str], Awaitable[None]]

_OUT_TIME = re.compile(r"out_time_us=(\d+)")


@dataclass(slots=True)
class SampledFrame:
    t: float
    path: Path


def frames_dir(video_id: str) -> Path:
    d = settings.data_dir / "frames" / video_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def sample_frames(
    video: dict[str, Any],
    *,
    every_s: float,
    width: int,
    progress: ProgressCb | None = None,
) -> list[SampledFrame]:
    """Muestrea el VOD entero en UNA pasada de ffmpeg.

    Una pasada streaming cuesta lo mismo sea 1 fotograma cada 20 s o cada 5: lo que se
    paga es recorrer el video. Hacer un `-ss` por fotograma seria mucho mas lento (2-3 s
    por seek remoto). Los fotogramas se cachean en disco, asi que reanalizar es gratis.
    """
    video_id = str(video["id"])
    out_dir = frames_dir(video_id)
    existing = sorted(out_dir.glob("s_*.jpg"))
    if existing:
        log.info("muestreo visual ya en cache: %d fotogramas", len(existing))
        if progress:
            await progress(1.0, f"{len(existing)} fotogramas (en cache)")
        return [_frame_from_path(p, every_s) for p in existing]

    source = str(video.get("video_path") or "") or str(video.get("stream_url") or "")
    if not source:
        raise RuntimeError("sin fuente de video para el muestreo visual")
    duration = float(video.get("duration") or 0.0)

    cmd = [
        ffmpeg_bin(),
        "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", source,
        "-vf", f"fps=1/{max(1.0, every_s):g},scale={width}:-2",
        "-q:v", "6",
        "-progress", "pipe:1", "-nostats",
        str(out_dir / "s_%05d.jpg"),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            m = _OUT_TIME.search(raw.decode("utf-8", "replace"))
            if m and progress and duration > 0:
                done = int(m.group(1)) / 1e6
                await progress(
                    min(0.99, done / duration),
                    f"Muestreando fotogramas ({done / duration * 100:.0f}%)",
                )
        code = await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if code != 0:
        err = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
        shutil.rmtree(out_dir, ignore_errors=True)
        raise CommandFailed(cmd, code, err)

    frames = [_frame_from_path(p, every_s) for p in sorted(out_dir.glob("s_*.jpg"))]
    if progress:
        await progress(1.0, f"{len(frames)} fotogramas")
    return frames


def _frame_from_path(path: Path, every_s: float) -> SampledFrame:
    # ffmpeg numera desde 1 y el primer fotograma es t=0.
    idx = int(path.stem.split("_")[-1])
    return SampledFrame(t=(idx - 1) * every_s, path=path)


class VisionProposer:
    """Propone momentos a partir de lo que se ve, no de como reacciona la audiencia."""

    def __init__(self, *, on_warning: WarnCb | None = None) -> None:
        self.on_warning = on_warning
        self.gemini = GeminiScorer()
        self.hits: list[VisualHit] = []
        self.context = VisionContext()

    @property
    def available(self) -> bool:
        return settings.vision_enabled and self.gemini.configured

    async def _warn(self, message: str) -> None:
        log.warning(message)
        if self.on_warning:
            await self.on_warning(message)

    async def propose(
        self, frames: list[SampledFrame], *, progress: ProgressCb | None = None
    ) -> list[VisualHit]:
        """Devuelve los fotogramas notables, ordenados por confianza."""
        if not frames:
            return []

        # Primero la linea base del VOD: sin ella el modelo marca como "inesperado"
        # cualquier cosa que no conozca (un zombi de noche en Minecraft acaba
        # propuesto como momento). Cuesta una sola peticion.
        sample = frames[:: max(1, len(frames) // 8)][:8]
        if progress:
            await progress(0.0, "Calibrando que es rutina en este directo")
        try:
            self.context = await self.gemini.calibrate_vision([(f.t, f.path) for f in sample])
            if self.context.game:
                log.info("vision calibrada: %s", self.context.game)
        except (QuotaExhausted, ScorerUnavailable) as exc:
            await self._warn(
                f"No se pudo calibrar el analisis visual ({exc}): se juzgara sin contexto "
                f"del juego y habra mas falsos positivos."
            )

        batch = max(1, settings.vision_batch)
        batches = [frames[i : i + batch] for i in range(0, len(frames), batch)]
        hits: list[VisualHit] = []
        for i, chunk in enumerate(batches):
            if progress:
                await progress(
                    i / len(batches),
                    f"Analizando fotogramas {i * batch + 1}-"
                    f"{min(len(frames), (i + 1) * batch)} de {len(frames)}",
                )
            try:
                found = await self.gemini.look_at_frames(
                    [(f.t, f.path) for f in chunk], self.context
                )
            except QuotaExhausted as exc:
                await self._warn(f"{exc}. Se omite el resto del analisis visual.")
                break
            except ScorerUnavailable as exc:
                await self._warn(f"Analisis visual no disponible ({exc}).")
                break
            hits.extend(
                h
                for h in found
                if h.notable
                and h.kind != "rutina"
                and h.confidence >= settings.vision_min_confidence
            )
        hits.sort(key=lambda h: -h.confidence)
        self.hits = hits[: settings.vision_max_hits]
        if progress:
            await progress(1.0, f"{len(self.hits)} momentos visuales")
        return self.hits
