"""Tipos e interfaces comunes de los proveedores (transcripcion y puntuacion)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(slots=True)
class Transcript:
    text: str
    words: list[Word] = field(default_factory=list)
    language: str = ""

    @property
    def empty(self) -> bool:
        return not self.text.strip()


@runtime_checkable
class Transcriber(Protocol):
    name: str

    async def transcribe(self, wav_path: Path, language: str | None) -> Transcript: ...


@dataclass(slots=True)
class VisualHit:
    """Un fotograma que el proponente visual considera digno de un clip.

    `what` es una *pista*, no el titulo final: Gemini ve un fotograma sin audio ni
    contexto y se equivoca en la semantica del juego (llamo "criatura robotica hostil"
    al companero de partida). La etapa de puntuacion, que si tiene el transcript de la
    ventana, es la que escribe el titulo y puede contradecirla.
    """

    t: float
    notable: bool
    what: str
    kind: str
    confidence: float


@dataclass(slots=True)
class ScoredMoment:
    """Salida del scorer para un candidato."""

    id: str
    title: str
    description: str
    category: str
    clip_score: float
    worth_clipping: bool


class ScorerUnavailable(RuntimeError):
    """El scorer LLM no esta disponible (sin key, cuota agotada, error de red)."""


class TranscriberUnavailable(RuntimeError):
    """El transcriptor no esta disponible (sin key, cuota agotada, modelo ausente)."""
