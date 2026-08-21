"""Puntuacion y descripcion de momentos con Gemini (salida JSON forzada por schema)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from ..models import CATEGORIES
from ..utils import log
from .base import ScoredMoment, ScorerUnavailable, VisualHit
from .ratelimit import (
    MAX_BACKOFF_ATTEMPTS,
    PACIFIC_TZ,
    QuotaExhausted,
    RateLimiter,
    parse_retry_after,
)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

PROMPT = """Eres un editor de clips para un streamer. Te doy N fragmentos transcritos de un directo, cada uno con su score de reaccion de la audiencia (chat + audio).

Para cada fragmento devuelve:
- id
- title: titulo en el idioma del fragmento, max 60 caracteres, sin clickbait vacio, concreto sobre lo que pasa
- description: 1-2 frases explicando que ocurre y por que funcionaria como clip
- category: reaccion | gracioso | habilidad | fail | polemica | informativo | otro
- clip_score: 0-100, que tan bueno seria como clip corto independiente
- worth_clipping: boolean - false si es una falsa alarma (el chat reacciono a algo externo, es un anuncio, es un raid, no se entiende sin contexto)

Si un fragmento trae `visto_en_pantalla`, es una pista de otro modelo que solo vio un
fotograma, sin audio ni contexto: puede equivocarse en la semantica del juego. Usala
como indicio, pero si el transcript la contradice, hazle caso al transcript.

Devuelve SOLO un array JSON. Nada de markdown ni backticks.

Fragmentos:
"""

VISION_PROMPT = """Eres un editor de clips de un directo. Te doy fotogramas del VOD, cada uno precedido por su timestamp.

Marca SOLO los que muestran algo que un espectador querria ver en un clip corto:
- un logro o hito: objeto o equipo raro conseguido, construccion terminada, nivel superado, marcador alto
- peligro, muerte o fallo del jugador
- algo inesperado, raro o gracioso en pantalla
- una reaccion visible del streamer en la webcam (sorpresa, risa, susto)

NO marques: juego rutinario (andar, minar, colocar bloques sueltos), menus e inventarios sin nada notable, pantallas de espera o de carga, o al streamer simplemente hablando.

Para cada fotograma devuelve: t (el timestamp que te doy, tal cual), notable (bool), what (que se ve, en espanol, maximo 90 caracteres), kind, confidence (0-100).
Devuelve SOLO el array JSON."""

VISION_KINDS = ("logro", "peligro", "inesperado", "reaccion", "rutina")

VISION_SCHEMA: dict[str, Any] = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "t": {"type": "NUMBER"},
            "notable": {"type": "BOOLEAN"},
            "what": {"type": "STRING"},
            "kind": {"type": "STRING", "enum": list(VISION_KINDS)},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["t", "notable", "what", "kind", "confidence"],
    },
}

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "title": {"type": "STRING"},
            "description": {"type": "STRING"},
            "category": {"type": "STRING", "enum": list(CATEGORIES)},
            "clip_score": {"type": "NUMBER"},
            "worth_clipping": {"type": "BOOLEAN"},
        },
        "required": ["id", "title", "description", "category", "clip_score", "worth_clipping"],
    },
}


class GeminiScorer:
    name = "gemini"

    def __init__(self) -> None:
        self.api_key = settings.gemini_api_key.strip()
        self.model = settings.gemini_model
        self.fallback_model = settings.gemini_fallback_model
        # El tier gratuito de Gemini cuenta el dia en Pacific Time, no en UTC.
        self.limiter = RateLimiter(
            "gemini",
            rpm=settings.gemini_rpm,
            rpd=settings.gemini_rpd,
            units_per_hour=settings.gemini_tpm,
            tz=PACIFIC_TZ,
            unit_window_s=60.0,
        )
        self.batch_size = max(1, settings.gemini_batch_size)
        self._model_in_use = self.model

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def has_room(self) -> bool:
        return await self.limiter.has_room(1.0)

    def build_prompt(self, fragments: list[dict[str, Any]]) -> str:
        lines = []
        for frag in fragments:
            lines.append(
                json.dumps(
                    {
                        "id": frag["id"],
                        "timestamp": frag.get("timestamp", ""),
                        "duracion_s": round(float(frag.get("duration", 0.0)), 1),
                        "score_reaccion": round(float(frag.get("signal_score", 0.0)), 2),
                        "mensajes_chat": int(frag.get("msg_count", 0)),
                        "usuarios_distintos": int(frag.get("unique_users", 0)),
                        "pico_audio_sigma": round(float(frag.get("audio_z", 0.0)), 2),
                        "transcripcion": frag.get("transcript", "") or "(sin habla detectada)",
                    },
                    ensure_ascii=False,
                )
            )
        return PROMPT + "\n".join(lines)

    async def _generate(
        self, body: dict[str, Any], *, timeout: float, models: list[str]
    ) -> dict[str, Any]:
        """POST a :generateContent con backoff, cambio de modelo y errores traducidos."""
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            for model in models:
                url = f"{API_ROOT}/{model}:generateContent"
                for attempt in range(MAX_BACKOFF_ATTEMPTS):
                    try:
                        resp = await client.post(url, headers=headers, json=body)
                    except httpx.HTTPError as exc:
                        if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                            raise ScorerUnavailable(f"Gemini inalcanzable: {exc}") from exc
                        await self.limiter.backoff(attempt)
                        continue

                    if resp.status_code == 429:
                        retry = parse_retry_after(resp.headers.get("retry-after"))
                        if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                            raise QuotaExhausted("gemini", "429", "rate limit persistente")
                        await self.limiter.backoff(attempt, retry)
                        continue
                    if resp.status_code in (401, 403):
                        raise ScorerUnavailable(f"Gemini rechazo la clave ({resp.status_code})")
                    if resp.status_code == 404:
                        # Modelo retirado o no disponible para esta cuenta.
                        log.warning("modelo %s no disponible, probando fallback", model)
                        break
                    if resp.status_code == 503:
                        # "high demand": merece la pena reintentar y luego cambiar de modelo.
                        log.warning("modelo %s saturado (503)", model)
                        if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                            break
                        await self.limiter.backoff(attempt)
                        continue
                    if resp.status_code >= 500:
                        if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                            break
                        await self.limiter.backoff(attempt)
                        continue
                    if resp.status_code >= 400:
                        raise ScorerUnavailable(
                            f"Gemini devolvio {resp.status_code}: {resp.text[:200]}"
                        )

                    self._model_in_use = model
                    return resp.json()
        raise ScorerUnavailable("Gemini: ningun modelo respondio correctamente")

    def _model_chain(self, preferred: str = "") -> list[str]:
        chain = [preferred or self._model_in_use]
        for extra in (self.model, self.fallback_model):
            if extra and extra not in chain:
                chain.append(extra)
        return chain

    async def score_batch(self, fragments: list[dict[str, Any]]) -> list[ScoredMoment]:
        """Puntua un lote (por defecto 10 candidatos por peticion)."""
        if not self.configured:
            raise ScorerUnavailable("GEMINI_API_KEY no configurada")
        if not fragments:
            return []

        prompt = self.build_prompt(fragments)
        # Estimacion conservadora de tokens: ~4 caracteres por token + margen de salida.
        est_tokens = int(len(prompt) / 4) + 220 * len(fragments)
        await self.limiter.acquire(est_tokens)

        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "temperature": 0.4,
                "maxOutputTokens": 400 * len(fragments) + 512,
            },
        }
        return _parse_response(
            await self._generate(body, timeout=120.0, models=self._model_chain())
        )

    async def look_at_frames(self, frames: list[tuple[float, Path]]) -> list[VisualHit]:
        """Pregunta a Gemini que fotogramas muestran algo digno de un clip."""
        if not self.configured:
            raise ScorerUnavailable("GEMINI_API_KEY no configurada")
        if not frames:
            return []

        parts: list[dict[str, Any]] = [{"text": VISION_PROMPT}]
        for t, path in frames:
            parts.append({"text": f"t={t:.0f}s"})
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                    }
                }
            )
        # Medido con fotogramas de 512 px: ~1.100 tokens por imagen.
        est_tokens = 1100 * len(frames) + 400 + 90 * len(frames)
        await self.limiter.acquire(est_tokens)

        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": VISION_SCHEMA,
                "temperature": 0.2,
                "maxOutputTokens": 120 * len(frames) + 512,
            },
        }
        preferred = settings.vision_model or ""
        payload = await self._generate(
            body, timeout=240.0, models=self._model_chain(preferred)
        )
        return _parse_vision(payload)


def _raw_json(payload: dict[str, Any]) -> Any:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise ScorerUnavailable("Gemini devolvio una respuesta vacia")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    raw = "".join(str(p.get("text") or "") for p in parts).strip()
    if not raw:
        raise ScorerUnavailable("Gemini devolvio contenido vacio")
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ScorerUnavailable(f"Gemini devolvio JSON invalido: {raw[:160]}") from exc


def _parse_vision(payload: dict[str, Any]) -> list[VisualHit]:
    data = _raw_json(payload)
    if isinstance(data, dict):
        data = data.get("frames") or data.get("items") or [data]
    out: list[VisualHit] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            t = float(item.get("t"))
        except (TypeError, ValueError):
            continue
        kind = str(item.get("kind") or "rutina").strip().lower()
        if kind not in VISION_KINDS:
            kind = "rutina"
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out.append(
            VisualHit(
                t=t,
                notable=bool(item.get("notable")),
                what=str(item.get("what") or "").strip()[:140],
                kind=kind,
                confidence=max(0.0, min(100.0, conf)),
            )
        )
    return out


def _parse_response(payload: dict[str, Any]) -> list[ScoredMoment]:
    data = _raw_json(payload)
    if isinstance(data, dict):
        data = data.get("moments") or data.get("items") or [data]

    out: list[ScoredMoment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "otro").strip().lower()
        if category not in CATEGORIES:
            category = "otro"
        try:
            clip_score = float(item.get("clip_score", 0.0))
        except (TypeError, ValueError):
            clip_score = 0.0
        out.append(
            ScoredMoment(
                id=str(item.get("id") or ""),
                title=str(item.get("title") or "").strip()[:120],
                description=str(item.get("description") or "").strip(),
                category=category,
                clip_score=max(0.0, min(100.0, clip_score)),
                worth_clipping=bool(item.get("worth_clipping", True)),
            )
        )
    return out
