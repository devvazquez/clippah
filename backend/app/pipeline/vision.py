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

import numpy as np
from PIL import Image

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


def frames_dir(video_id: str, every_s: float) -> Path:
    """El intervalo forma parte de la ruta.

    El timestamp de cada fotograma se deduce de su indice multiplicado por el intervalo,
    asi que una cache hecha cada 20 s reutilizada como si fuera de 10 s colocaria todos
    los momentos al doble de su tiempo real. Separar por intervalo evita esa mezcla y
    permite que convivan los dos muestreos.
    """
    d = settings.data_dir / "frames" / video_id / f"s{every_s:g}"
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
    out_dir = frames_dir(video_id, every_s)
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


def _mean_gradients(
    frames: list[Path], *, limit: int = 40
) -> tuple[np.ndarray, np.ndarray] | None:
    """Magnitud media del gradiente horizontal y vertical sobre varios fotogramas.

    El borde de una webcam compuesta no se mueve en todo el directo; el del juego si.
    Promediando sobre muchos fotogramas, los bordes del layout sobresalen y los del
    contenido se diluyen.
    """
    gx = gy = None
    used = 0
    for f in frames[:limit]:
        try:
            a = np.asarray(Image.open(f).convert("L"), dtype=float)
        except OSError:
            continue
        dx = np.abs(np.diff(a, axis=1))
        dy = np.abs(np.diff(a, axis=0))
        gx = dx if gx is None else gx + dx
        gy = dy if gy is None else gy + dy
        used += 1
    if used < 5 or gx is None or gy is None:
        return None
    return gx / used, gy / used


def _peak(profile: np.ndarray, n: int, lo: float, hi: float) -> tuple[float, float] | None:
    """Posicion (0-1) y fuerza del maximo del perfil dentro del rango."""
    a, b = int(n * lo), int(n * hi)
    if b - a < 3:
        return None
    seg = profile[a:b]
    i = int(np.argmax(seg))
    return (a + i) / n, float(seg[i])


def detect_cam_layout(frames: list[Path]) -> dict[str, list[float]] | None:
    """Localiza las dos webcams (arriba-izquierda y abajo-derecha) del layout.

    Cada borde se mide *dentro de su region*, no sobre todo el fotograma: buscando el
    borde superior de la camara de abajo en el ancho completo, lo que gana es la barra
    de objetos del juego, que cruza el centro y tiene mas contraste que la camara.

    Devuelve rectangulos en fraccion del fotograma, o None si lo que sale no parece una
    webcam (entonces se usan las coordenadas de configuracion).
    """
    grads = _mean_gradients(frames)
    if grads is None:
        return None
    gx, gy = grads
    h, w = gy.shape[0] + 1, gx.shape[1] + 1

    def rows(arr, lo, hi):
        return arr[int(arr.shape[0] * lo) : int(arr.shape[0] * hi)]

    def cols(arr, lo, hi):
        return arr[:, int(arr.shape[1] * lo) : int(arr.shape[1] * hi)]

    # Bordes verticales: el derecho de la camara de arriba solo en la mitad superior,
    # y el izquierdo de la de abajo solo en la inferior.
    right = _peak(rows(gx, 0.0, 0.45).mean(axis=0), w, 0.12, 0.50)
    left = _peak(rows(gx, 0.55, 1.0).mean(axis=0), w, 0.50, 0.92)
    if not (right and left):
        return None
    # Bordes horizontales, cada uno restringido a las columnas de su camara.
    bottom = _peak(cols(gy, 0.0, right[0]).mean(axis=1), h, 0.12, 0.55)
    top = _peak(cols(gy, left[0], 1.0).mean(axis=1), h, 0.45, 0.92)
    if not (bottom and top):
        return None

    floor = float(np.median(gx)) * 1.5
    if min(right[1], left[1], bottom[1], top[1]) < floor:
        log.info("bordes de camara poco marcados: se usan las coordenadas de config")
        return None

    # Las camaras van pegadas a sus esquinas.
    top_rect = [0.0, 0.0, right[0], bottom[0]]
    bottom_rect = [left[0], top[0], 1.0 - left[0], 1.0 - top[0]]

    # En estos layouts las dos camaras son del mismo tamano. Si la de abajo sale con una
    # proporcion muy distinta, es que el borde vertical detectado no era el suyo: se
    # deduce su ancho de la proporcion de la de arriba, anclada a la esquina derecha.
    aspect_top = (top_rect[2] * w) / max(top_rect[3] * h, 1e-6)
    aspect_bottom = (bottom_rect[2] * w) / max(bottom_rect[3] * h, 1e-6)
    if aspect_top > 0 and abs(aspect_bottom / aspect_top - 1.0) > 0.25:
        width = aspect_top * bottom_rect[3] * h / w
        log.info(
            "camara inferior con proporcion %.2f frente a %.2f de la superior: "
            "se deduce su ancho (%.3f -> %.3f)",
            aspect_bottom, aspect_top, bottom_rect[2], width,
        )
        bottom_rect = [1.0 - width, top[0], width, bottom_rect[3]]
    for label, rect in (("superior", top_rect), ("inferior", bottom_rect)):
        rw, rh = rect[2] * w, rect[3] * h
        if not (0.10 <= rect[2] <= 0.45 and 0.12 <= rect[3] <= 0.50):
            log.info("camara %s con tamano improbable (%.2f x %.2f): config", label,
                     rect[2], rect[3])
            return None
        if not (0.9 <= rw / max(rh, 1e-6) <= 2.4):
            log.info("camara %s con proporcion improbable (%.2f): config", label, rw / rh)
            return None

    log.info(
        "layout detectado: cam sup 0-%.3f x 0-%.3f | cam inf %.3f-1 x %.3f-1",
        right[0], bottom[0], left[0], top[0],
    )
    return {
        "top": [round(v, 4) for v in top_rect],
        "bottom": [round(v, 4) for v in bottom_rect],
        # El hueco para el juego se toma de los rectangulos finales, no de los bordes
        # detectados: si el de abajo se ha corregido, el hueco cambia con el.
        "game_x": [round(top_rect[2], 4), round(bottom_rect[0], 4)],
    }


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
        self,
        frames: list[SampledFrame],
        *,
        progress: ProgressCb | None = None,
        max_hits: int | None = None,
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
        self.hits = hits[: max_hits or settings.vision_max_hits]
        if progress:
            await progress(1.0, f"{len(self.hits)} momentos visuales")
        return self.hits
