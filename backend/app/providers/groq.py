"""Transcripcion con Groq (`whisper-large-v3-turbo`, endpoint compatible con OpenAI)."""

from __future__ import annotations

import math
from pathlib import Path

import httpx
import soundfile as sf

from ..config import settings
from ..utils import log
from .base import TranscriberUnavailable, Transcript, Word
from .ratelimit import MAX_BACKOFF_ATTEMPTS, UTC_TZ, QuotaExhausted, RateLimiter, parse_retry_after

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class GroqTranscriber:
    """Los limites son por *organizacion*, no por API key: crear varias no da mas cuota."""

    name = "groq"

    def __init__(self) -> None:
        self.api_key = settings.groq_api_key.strip()
        self.model = settings.groq_model
        self.limiter = RateLimiter(
            "groq",
            rpm=settings.groq_rpm,
            rpd=settings.groq_rpd,
            units_per_hour=settings.groq_ash,
            units_per_day=settings.groq_asd,
            tz=UTC_TZ,
            unit_window_s=3600.0,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _billable_seconds(self, wav_path: Path) -> float:
        try:
            info = sf.info(str(wav_path))
            secs = info.frames / float(info.samplerate or 16000)
        except Exception:  # noqa: BLE001 - si no se puede leer, asumimos el minimo
            secs = settings.groq_min_billed_seconds
        # Groq factura un minimo de 10 s por peticion.
        return max(settings.groq_min_billed_seconds, math.ceil(secs))

    async def has_room(self, wav_path: Path) -> bool:
        return await self.limiter.has_room(self._billable_seconds(wav_path))

    async def transcribe(self, wav_path: Path, language: str | None = None) -> Transcript:
        if not self.configured:
            raise TranscriberUnavailable("GROQ_API_KEY no configurada")
        size_mb = wav_path.stat().st_size / (1024 * 1024)
        if size_mb > settings.groq_max_file_mb:
            raise TranscriberUnavailable(
                f"El fragmento pesa {size_mb:.1f} MB y Groq acepta como maximo "
                f"{settings.groq_max_file_mb:g} MB"
            )
        units = self._billable_seconds(wav_path)
        await self.limiter.acquire(units)

        data = {
            "model": self.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
        }
        if language:
            data["language"] = language
        # Vocabulario del canal como pista. Whisper escribe los nombres propios "a oido"
        # y en un directo son la mitad de lo que importa: el mote del streamer, los
        # amigos que salen, el juego. Sin esto, "Jopa" sale "Hopa" y "PoliSpawn"
        # "Polispol", y eso acaba quemado en el subtitulo y en el titulo del clip.
        vocab = settings.transcribe_vocab.strip()
        if vocab:
            data["prompt"] = vocab

        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = wav_path.read_bytes()

        async with httpx.AsyncClient(timeout=180.0) as client:
            for attempt in range(MAX_BACKOFF_ATTEMPTS):
                files = {"file": (wav_path.name, payload, "audio/wav")}
                try:
                    resp = await client.post(GROQ_URL, headers=headers, data=data, files=files)
                except httpx.HTTPError as exc:
                    if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                        raise TranscriberUnavailable(f"Groq inalcanzable: {exc}") from exc
                    await self.limiter.backoff(attempt)
                    continue

                if resp.status_code == 429:
                    retry = parse_retry_after(resp.headers.get("retry-after"))
                    if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                        raise QuotaExhausted("groq", "429", "rate limit persistente")
                    await self.limiter.backoff(attempt, retry)
                    continue
                if resp.status_code in (401, 403):
                    raise TranscriberUnavailable(
                        f"Groq rechazo la clave ({resp.status_code})"
                    )
                if resp.status_code >= 500:
                    if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                        raise TranscriberUnavailable(f"Groq 5xx: {resp.status_code}")
                    await self.limiter.backoff(attempt)
                    continue
                if resp.status_code >= 400:
                    raise TranscriberUnavailable(
                        f"Groq devolvio {resp.status_code}: {resp.text[:200]}"
                    )
                return _parse_verbose_json(resp.json())
        raise TranscriberUnavailable("Groq: se agotaron los reintentos")


def _parse_verbose_json(payload: dict) -> Transcript:
    words: list[Word] = []
    for w in payload.get("words") or []:
        try:
            words.append(
                Word(
                    text=str(w.get("word") or w.get("text") or "").strip(),
                    start=float(w.get("start", 0.0)),
                    end=float(w.get("end", 0.0)),
                )
            )
        except (TypeError, ValueError):
            continue
    if not words:
        # Algunos modelos solo devuelven segmentos: se usan como "palabras" gruesas.
        for seg in payload.get("segments") or []:
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            words.append(
                Word(text=text, start=float(seg.get("start", 0.0)), end=float(seg.get("end", 0.0)))
            )
    text = str(payload.get("text") or " ".join(w.text for w in words)).strip()
    lang = str(payload.get("language") or "")
    log.debug("groq transcript: %d palabras, idioma %s", len(words), lang)
    return Transcript(text=text, words=words, language=lang)
