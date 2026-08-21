"""Puntuacion y descripcion de los momentos: Gemini si hay key, heuristica si no."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import CATEGORY_KEYWORDS, settings
from ..providers.base import ScoredMoment, ScorerUnavailable
from ..providers.gemini import GeminiScorer
from ..providers.ratelimit import QuotaExhausted
from ..utils import hhmmss, log
from .transcribe import first_words

WarnCb = Callable[[str], Awaitable[None]]

# Ponderacion final entre la senal medida y el juicio del LLM.
#
# Sin LLM, `clip_score` es el percentil del propio signal_score, asi que el reparto es
# nominal. Con LLM hay que tener cuidado: el prompt ya recibe los mensajes de chat, los
# usuarios distintos y el pico de audio, asi que la reaccion de la audiencia *ya esta
# dentro* de clip_score. Sumar aparte un 40% de senal la cuenta dos veces, y el efecto
# no es neutro: hunde justo lo que el proponente visual encuentra, que por construccion
# tiene poca reaccion (si la tuviera, lo habrian encontrado las senales). Con el reparto
# 0.4/0.6, el momento que el modelo puntuaba mejor como clip (55) caia al 5o puesto por
# detras de una charla que el mismo modelo puntuaba 35.
#
# Con LLM, la senal se queda como desempate entre clip_scores parecidos (el modelo los
# emite de 5 en 5), no como juez.
W_SIGNAL = 0.4
W_LLM = 0.6
W_SIGNAL_ENRICHED = 0.15
# Un candidato de vision no tiene reaccion medida: no es que la audiencia lo ignorara,
# es que se encontro por otra via. Se le da un valor neutro en vez de su ~0 real.
NEUTRAL_SIGNAL = 0.5


@dataclass(slots=True)
class Fragment:
    """Candidato ya transcrito, listo para puntuar."""

    id: str
    t_start: float
    t_end: float
    t_peak: float
    signal_score: float
    chat_z: float
    audio_z: float
    unique_users: int
    msg_count: int
    combo: bool
    chat_ratio: float = 0.0
    source: str = "signals"
    vision_note: str = ""
    vision_kind: str = ""
    transcript: str = ""
    language: str = ""
    words: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.t_end - self.t_start)

    def to_prompt_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "timestamp": hhmmss(self.t_peak),
            "duration": self.duration,
            "signal_score": self.signal_score,
            "msg_count": self.msg_count,
            "unique_users": self.unique_users,
            "audio_z": self.audio_z,
            "transcript": self.transcript,
        }
        if self.vision_note:
            d["visto_en_pantalla"] = self.vision_note
            d["propuesto_por"] = "vision"
        return d


VISION_KIND_TO_CATEGORY = {
    "logro": "habilidad",
    "peligro": "fail",
    "inesperado": "reaccion",
    "reaccion": "reaccion",
}


def guess_category(text: str, fragment: Fragment) -> str:
    if fragment.vision_kind in VISION_KIND_TO_CATEGORY:
        return VISION_KIND_TO_CATEGORY[fragment.vision_kind]
    low = (text or "").lower()
    best, best_hits = "otro", 0
    for category, keywords in CATEGORY_KEYWORDS.items():
        hits = sum(1 for k in keywords if k in low)
        if hits > best_hits:
            best, best_hits = category, hits
    if best_hits:
        return best
    # Sin texto util: el perfil de senales sigue diciendo algo.
    if fragment.audio_z >= 3.0 and fragment.msg_count == 0:
        return "reaccion"
    if fragment.msg_count > 0 and fragment.audio_z < 1.0:
        return "reaccion"
    return "otro"


def heuristic_scores(fragments: list[Fragment], *, chat_available: bool) -> list[ScoredMoment]:
    """Sin LLM la app sigue funcionando: titulos y descripciones derivadas de las senales."""
    raw = [f.signal_score for f in fragments]
    pct = _percentiles(raw)
    out: list[ScoredMoment] = []
    for frag, p in zip(fragments, pct, strict=True):
        # Sin LLM que redacte, la pista visual es mejor titulo que las primeras
        # palabras del transcript (y desde luego mejor que "Momento a 41:25").
        head = first_words(frag.transcript, 8)
        title = frag.vision_note or head or f"Momento a {hhmmss(frag.t_peak)}"
        # Con 1-2 mensajes no hay "pico de actividad" que describir: manda el audio.
        chat_is_signal = chat_available and frag.msg_count >= 3
        if chat_is_signal:
            window = int(2 * settings.combo_window_s)
            description = f"Pico de actividad: {frag.msg_count} mensajes en {window} s"
            if frag.chat_ratio >= 1.1:
                description += f", {frag.chat_ratio:.1f}x el ritmo normal del chat."
            else:
                description += "."
        elif frag.audio_z:
            description = (
                f"Pico de audio de +{frag.audio_z:.1f} sigma sobre el nivel habitual "
                f"del directo."
            )
        else:
            description = "Pico de actividad detectado por las senales del directo."
        if chat_available and frag.msg_count and not chat_is_signal:
            msgs = frag.msg_count
            description += (
                f" {msgs} mensaje{'s' if msgs != 1 else ''} de chat en la ventana."
            )
        if frag.source == "vision" and frag.vision_note:
            description = f"Detectado en pantalla: {frag.vision_note}. {description}"
        if frag.combo:
            description += " Coinciden pico de audio y pico de chat."
        out.append(
            ScoredMoment(
                id=frag.id,
                title=title[:120],
                description=description,
                category=guess_category(frag.transcript, frag),
                clip_score=round(p * 100.0, 1),
                worth_clipping=True,
                hook=first_words(frag.transcript, 6),
            )
        )
    return out


# Sin LLM no hay ningun candidato que "valga 0": todos han superado el umbral de pico.
# El percentil se mapea a [FLOOR, 1] para que el ultimo no salga con un score de 0/100.
HEURISTIC_FLOOR = 0.3


def _percentiles(values: list[float]) -> list[float]:
    """Percentil de cada valor dentro de la propia lista, mapeado a [HEURISTIC_FLOOR, 1]."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.75]
    order = sorted(range(n), key=lambda i: values[i])
    pct = [0.0] * n
    for rank, idx in enumerate(order):
        pct[idx] = HEURISTIC_FLOOR + (1.0 - HEURISTIC_FLOOR) * (rank / (n - 1))
    return pct


class ScoringEngine:
    def __init__(self, *, on_warning: WarnCb | None = None) -> None:
        self.on_warning = on_warning
        self.gemini = GeminiScorer()
        self.provider = "heuristic"

    async def _warn(self, message: str) -> None:
        log.warning(message)
        if self.on_warning:
            await self.on_warning(message)

    async def prepare(self) -> str:
        self.provider = "gemini" if self.gemini.configured else "heuristic"
        return self.provider

    async def score(
        self, fragments: list[Fragment], *, chat_available: bool
    ) -> tuple[list[ScoredMoment], bool]:
        """Devuelve (scores, enriched). `enriched=False` = generado sin LLM."""
        if not fragments:
            return [], False
        if self.provider != "gemini":
            return heuristic_scores(fragments, chat_available=chat_available), False

        by_id = {f.id: f for f in fragments}
        collected: dict[str, ScoredMoment] = {}
        batch_size = self.gemini.batch_size
        batches = [
            fragments[i : i + batch_size] for i in range(0, len(fragments), batch_size)
        ]
        degraded = False
        for batch in batches:
            if degraded:
                break
            try:
                scored = await self.gemini.score_batch([f.to_prompt_dict() for f in batch])
            except QuotaExhausted as exc:
                degraded = True
                await self._warn(f"{exc}. Puntuando con la heuristica local.")
                break
            except ScorerUnavailable as exc:
                degraded = True
                await self._warn(f"Gemini no disponible ({exc}). Puntuando con la heuristica.")
                break
            for item in scored:
                if item.id in by_id:
                    collected[item.id] = item

        missing = [f for f in fragments if f.id not in collected]
        if missing:
            for item in heuristic_scores(missing, chat_available=chat_available):
                collected[item.id] = item
            if not degraded and len(missing) != len(fragments):
                log.info("Gemini omitio %d fragmentos, completados con heuristica", len(missing))

        enriched = len(collected) > len(missing)
        return [collected[f.id] for f in fragments], enriched


def finalize(
    fragments: list[Fragment], scores: list[ScoredMoment], *, enriched: bool = False
) -> list[dict[str, Any]]:
    """Combina senal y LLM, filtra falsas alarmas, ordena y recorta a TOP_N."""
    # La normalizacion se hace solo sobre los candidatos de senales: incluir los de
    # vision (con score ~0) comprimiria a todos los demas contra el techo.
    signal_only = [f.signal_score for f in fragments if f.source != "vision"]
    lo = min(signal_only) if signal_only else 0.0
    hi = max(signal_only) if signal_only else 0.0
    spread = hi - lo

    w_signal = W_SIGNAL_ENRICHED if enriched else W_SIGNAL
    w_llm = 1.0 - w_signal
    rows: list[dict[str, Any]] = []
    for frag, score in zip(fragments, scores, strict=True):
        if not score.worth_clipping:
            continue
        if frag.source == "vision":
            nrm = NEUTRAL_SIGNAL
        elif spread < 1e-9:
            nrm = 0.5
        else:
            nrm = (frag.signal_score - lo) / spread
        final = w_signal * nrm + w_llm * (score.clip_score / 100.0)
        rows.append(
            {
                "id": frag.id,
                "t_start": frag.t_start,
                "t_end": frag.t_end,
                "t_peak": frag.t_peak,
                "title": score.title or f"Momento a {hhmmss(frag.t_peak)}",
                "description": score.description,
                "category": score.category,
                "hook": score.hook,
                "clip_title": score.clip_title or score.title,
                "final_score": round(min(1.0, max(0.0, final)), 4),
                "signal_score": frag.signal_score,
                "clip_score": score.clip_score,
                "chat_z": frag.chat_z,
                "audio_z": frag.audio_z,
                "unique_users": frag.unique_users,
                "msg_count": frag.msg_count,
                "combo": frag.combo,
                "transcript": frag.transcript,
                "words": frag.words,
                "language": frag.language,
                "source": frag.source,
                "vision_note": frag.vision_note,
            }
        )
    rows.sort(key=lambda r: -r["final_score"])
    return rows[: settings.top_n]
