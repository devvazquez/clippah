"""Transcripcion local con faster-whisper. Sin API keys y sin cuotas."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from ..config import settings
from ..utils import log
from .base import TranscriberUnavailable, Transcript, Word

_model: Any = None
_model_key: tuple[str, str, str] | None = None
_model_lock = threading.Lock()
# faster-whisper no es thread-safe: serializamos las transcripciones.
_infer_lock = asyncio.Lock()


def _pick_device() -> tuple[str, str]:
    device = settings.whisper_device.strip().lower()
    if device == "auto":
        try:
            import ctranslate2

            cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001 - sin ctranslate2 asumimos CPU
            cuda = False
        device = "cuda" if cuda else "cpu"
    compute = settings.whisper_compute_type.strip()
    if not compute:
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def _load_model() -> Any:
    global _model, _model_key
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise TranscriberUnavailable(
            "faster-whisper no esta instalado. Instala el extra: pip install -e '.[local]'"
        ) from exc

    device, compute = _pick_device()
    key = (settings.whisper_model, device, compute)
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        log.info(
            "cargando Whisper local %s en %s (%s). La primera vez descarga el modelo.",
            settings.whisper_model, device, compute,
        )
        _model = WhisperModel(settings.whisper_model, device=device, compute_type=compute)
        _model_key = key
        log.info("Whisper local listo")
        return _model


class LocalTranscriber:
    name = "local"

    def __init__(self) -> None:
        self.model_name = settings.whisper_model

    @property
    def configured(self) -> bool:
        try:
            import faster_whisper  # noqa: F401

            return True
        except ImportError:
            return False

    async def warmup(self) -> None:
        await asyncio.to_thread(_load_model)

    async def transcribe(self, wav_path: Path, language: str | None = None) -> Transcript:
        async with _infer_lock:
            return await asyncio.to_thread(self._transcribe_sync, wav_path, language)

    def _transcribe_sync(self, wav_path: Path, language: str | None) -> Transcript:
        model = _load_model()
        # `language=None` deja la autodeteccion: los streamers de aqui mezclan
        # castellano y catalan en la misma frase y forzar el idioma empeora el
        # resultado.
        segments, info = model.transcribe(
            str(wav_path),
            language=language,
            word_timestamps=True,
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,
        )
        words: list[Word] = []
        chunks: list[str] = []
        for seg in segments:
            chunks.append(seg.text.strip())
            for w in getattr(seg, "words", None) or []:
                text = (w.word or "").strip()
                if not text:
                    continue
                words.append(Word(text=text, start=float(w.start or 0.0), end=float(w.end or 0.0)))
        return Transcript(
            text=" ".join(c for c in chunks if c).strip(),
            words=words,
            language=str(getattr(info, "language", "") or ""),
        )
