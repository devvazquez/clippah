"""Transcripcion de las ventanas candidatas (nunca del VOD entero) y refinado de bordes.

30 candidatos x 40 s = 20 minutos de audio frente a 6 horas. Esta decision es la que
hace viable el tier gratuito.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from ..providers.base import TranscriberUnavailable, Transcript, Word
from ..providers.groq import GroqTranscriber
from ..providers.local import LocalTranscriber
from ..providers.ratelimit import QuotaExhausted
from ..utils import CommandFailed, clamp, ffmpeg_bin, log, run

WarnCb = Callable[[str], Awaitable[None]]

# Margen extra alrededor de la ventana para que el refinado de bordes tenga material.
PAD_BEFORE_S = 8.0
PAD_AFTER_S = 5.0
# Silencio a partir del cual consideramos que la frase termino.
SILENCE_S = 0.6


@dataclass(slots=True)
class Bounds:
    t_start: float
    t_end: float


async def cut_window(audio_path: Path, t_start: float, t_end: float, out: Path) -> Path:
    """Recorta un WAV 16 kHz mono de la ventana. `-ss` antes de `-i` para seek rapido."""
    duration = max(0.2, t_end - t_start)
    cmd = [
        ffmpeg_bin(),
        "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{max(0.0, t_start):.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(audio_path),
        "-ar", "16000", "-ac", "1",
        str(out),
    ]
    await run(cmd, timeout=180)
    if not out.exists() or out.stat().st_size < 1024:
        raise CommandFailed(cmd, 0, "el recorte de audio salio vacio")
    return out


def shift_words(words: list[Word], offset: float) -> list[Word]:
    """Los timestamps de Whisper son relativos a la ventana: los pasamos a absolutos."""
    return [Word(text=w.text, start=w.start + offset, end=w.end + offset) for w in words]


def refine_bounds(
    words: list[Word], t_peak: float, t_start: float, t_end: float, duration: float
) -> Bounds:
    """Ajusta el corte para que no empiece ni acabe a media frase.

    Esto es lo que separa un clip que parece editado de un recorte aleatorio.
    """
    min_len = settings.min_clip_s
    max_len = settings.max_clip_s
    start, end = t_start, t_end

    if words:
        # --- inicio: primera palabra tras el silencio mas largo de [t_peak-30, t_peak-10]
        lo, hi = t_peak - 30.0, t_peak - 10.0
        best_gap = 0.0
        best_start: float | None = None
        prev_end: float | None = None
        for w in words:
            if w.start < lo:
                prev_end = w.end
                continue
            if w.start > hi:
                break
            if prev_end is not None:
                gap = w.start - prev_end
                if gap > best_gap:
                    best_gap = gap
                    best_start = w.start
            prev_end = w.end
        if best_start is not None and best_gap >= 0.35:
            start = best_start - 0.25
        else:
            first_after = next((w for w in words if w.start >= lo), None)
            if first_after is not None and first_after.start <= hi:
                start = first_after.start - 0.25

        # --- fin: final de la ultima palabra antes del siguiente silencio > 0.6 s
        after = [w for w in words if w.end > t_peak]
        if after:
            new_end = after[0].end
            for prev, nxt in zip(after, after[1:], strict=False):
                if nxt.start - prev.end > SILENCE_S:
                    new_end = prev.end
                    break
                new_end = nxt.end
            end = new_end + 0.35

    start = max(0.0, start)
    end = min(duration, max(end, start + min_len))

    # Los clips que funcionan duran lo que duran: medido sobre los 30 mas vistos de
    # auronplay e ibai, mediana de 26 s los dos, y el 60% entre 16 y 30 s. Si el corte
    # refinado se pasa holgadamente del objetivo, se recorta por el final (el pico esta
    # al principio de la ventana, no al final) en lugar de dejar 50 s que nadie termina.
    target = clamp(settings.target_clip_s, min_len, max_len)
    if end - start > target * 1.5:
        trimmed = start + target
        if trimmed > t_peak + 2.0:
            end = min(end, trimmed)

    # Clamp de duracion, respetando siempre que el pico quede dentro.
    if end - start > max_len:
        end = start + max_len
        if t_peak > end:
            end = min(duration, t_peak + 3.0)
            start = max(0.0, end - max_len)
    if end - start < min_len:
        start = max(0.0, min(start, end - min_len))
        end = min(duration, start + min_len)
    return Bounds(t_start=round(start, 2), t_end=round(min(duration, end), 2))


class TranscriptionEngine:
    """Elige proveedor y degrada de Groq a Whisper local sin romper el job."""

    def __init__(self, *, on_warning: WarnCb | None = None) -> None:
        self.on_warning = on_warning
        self.groq = GroqTranscriber()
        self.local = LocalTranscriber()
        self.provider = "none"
        self._groq_disabled = False
        self._local_disabled = False

    async def _warn(self, message: str) -> None:
        log.warning(message)
        if self.on_warning:
            await self.on_warning(message)

    async def prepare(self) -> str:
        if self.groq.configured:
            self.provider = "groq"
        elif self.local.configured:
            self.provider = "local"
        else:
            self.provider = "none"
            await self._warn(
                "Sin GROQ_API_KEY y sin faster-whisper instalado: no habra transcripciones "
                "(los titulos se generaran solo con senales)."
            )
        if not self.local.configured:
            self._local_disabled = True
        return self.provider

    @property
    def available(self) -> bool:
        return self.provider != "none"

    async def transcribe_window(
        self, audio_path: Path, t_start: float, t_end: float, *, work_dir: Path, tag: str
    ) -> tuple[Transcript, str]:
        """Devuelve (transcript con timestamps ABSOLUTOS, proveedor usado)."""
        if self.provider == "none":
            return Transcript(text="", words=[], language=""), "none"

        clip = work_dir / f"win-{tag}.wav"
        # Groq factura un minimo de 10 s: nunca enviamos clips mas cortos.
        length = max(t_end - t_start, settings.groq_min_billed_seconds + 0.5)
        real_end = t_start + length
        try:
            await cut_window(audio_path, t_start, real_end, clip)
        except (CommandFailed, OSError) as exc:
            await self._warn(f"No se pudo recortar el audio en {t_start:.0f}s: {exc}")
            return Transcript(text="", words=[], language=""), "none"

        try:
            transcript, used = await self._run_providers(clip)
        finally:
            clip.unlink(missing_ok=True)

        transcript.words = shift_words(transcript.words, t_start)
        return transcript, used

    async def _run_providers(self, clip: Path) -> tuple[Transcript, str]:
        if self.provider == "groq" and not self._groq_disabled:
            try:
                if not await self.groq.has_room(clip):
                    raise QuotaExhausted("groq", "cuota diaria", "sin margen para este fragmento")
                return await self.groq.transcribe(clip, None), "groq"
            except QuotaExhausted as exc:
                self._groq_disabled = True
                self.provider = "local" if not self._local_disabled else "none"
                await self._warn(f"{exc}. Usando Whisper local.")
            except TranscriberUnavailable as exc:
                self._groq_disabled = True
                self.provider = "local" if not self._local_disabled else "none"
                await self._warn(f"Groq no disponible ({exc}). Usando Whisper local.")

        if self.provider == "local" and not self._local_disabled:
            try:
                return await self.local.transcribe(clip, None), "local"
            except TranscriberUnavailable as exc:
                self._local_disabled = True
                self.provider = "none"
                await self._warn(f"Whisper local no disponible ({exc}). Sin transcripciones.")
            except Exception as exc:  # noqa: BLE001 - una ventana rota no tumba el job
                await self._warn(f"Whisper local fallo en un fragmento: {exc}")

        return Transcript(text="", words=[], language=""), "none"


def first_words(text: str, n: int = 8) -> str:
    parts = [p for p in text.split() if p]
    if not parts:
        return ""
    out = " ".join(parts[:n])
    return out if len(parts) <= n else out + "..."


def clamp_duration(t_start: float, t_end: float) -> tuple[float, float]:
    length = clamp(t_end - t_start, settings.min_clip_s, settings.max_clip_s)
    return t_start, t_start + length
