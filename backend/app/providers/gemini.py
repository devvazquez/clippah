"""Puntuacion y descripcion de momentos con Gemini (salida JSON forzada por schema)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import settings
from ..models import CATEGORIES
from ..utils import log
from .base import ScoredMoment, ScorerUnavailable
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

Devuelve SOLO un array JSON. Nada de markdown ni backticks.

Fragmentos:
"""

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
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

        models = [self._model_in_use]
        if self.fallback_model and self.fallback_model != self._model_in_use:
            models.append(self.fallback_model)

        async with httpx.AsyncClient(timeout=120.0) as client:
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
                        log.warning("modelo %s no disponible, probando fallback", model)
                        break
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
                    return _parse_response(resp.json())
        raise ScorerUnavailable("Gemini: ningun modelo respondio correctamente")


def _parse_response(payload: dict[str, Any]) -> list[ScoredMoment]:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise ScorerUnavailable("Gemini devolvio una respuesta vacia")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    raw = "".join(str(p.get("text") or "") for p in parts).strip()
    if not raw:
        raise ScorerUnavailable("Gemini devolvio contenido vacio")
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ScorerUnavailable(f"Gemini devolvio JSON invalido: {raw[:160]}") from exc
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
