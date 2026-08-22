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
from .base import ScoredMoment, ScorerUnavailable, VisionContext, VisualHit
from .ratelimit import (
    MAX_BACKOFF_ATTEMPTS,
    PACIFIC_TZ,
    QuotaExhausted,
    RateLimiter,
    parse_retry_after,
)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

PROMPT = """Eres un editor de clips para un streamer. Te doy N fragmentos transcritos de un directo, cada uno con su score de reaccion de la audiencia (chat + audio).

Tu criterio no es "aqui pasa algo", es "esto funcionaria como clip corto en TikTok, Reels o Shorts, compitiendo con todo lo demas del feed". Y lo que buscamos, por encima de todo, es que HAGA GRACIA: risa, verguenza ajena, absurdo, fallo tonto, susto que acaba en carcajada. Eso es lo que la gente ve hasta el final y le manda a un amigo. Un momento admirable, tierno o interesante retiene mucho peor que uno que hace reir, aunque este mejor jugado.

Un clip que funciona cumple casi siempre esto:

1. TIENE GRACIA O NO TIENE NADA. Antes de nada pregunta: alguien que no conoce a nadie aqui, se rie, se sorprende o le da verguenza ajena? Si la respuesta es "es simpatico", es un no.
2. GANCHO INMEDIATO. Algo pasa en los primeros 1-2 segundos. Si arranca con conversacion de relleno y lo bueno llega al segundo 20, no sirve: nadie llega.
3. RETIENE HASTA EL FINAL. Lo que se ve en el segundo 3 tiene que hacer que se llegue al 15. Sube: una reaccion que crece, un segundo remate detras del primero, alguien que se pone peor. Un clip con el chiste al principio y veinte segundos de bajada muere igual que uno sin gancho. Y a igualdad de todo, gana el corto: 15-30 segundos es la medida, un minuto casi nunca se acaba.
4. SE ENTIENDE SOLO. Sin conocer al streamer, ni el juego, ni lo que paso antes. Si necesita contexto para tener gracia, no funciona.
5. TIENE REMATE. Un pico y un cierre: el fallo, el grito, el chiste, el logro. Una charla interesante sin remate no es un clip.
6. EMOCION FUERTE Y CLARA. Risa, asombro, tension, verguenza, rabia. Una sola emocion nitida vale mas que un momento "simpatico".
7. SE PUEDE CONTAR EN UNA FRASE. Si no puedes resumir por que alguien lo compartiria, no lo compartiran.

Penaliza sin piedad: conversacion cotidiana entre amigos, explicaciones tecnicas, "buenas ideas" que no se ven, planificar lo que van a construir, leer el chat, dar las gracias por follows, chistes internos, gameplay competente pero normal, y cualquier cosa cuyo interes dependa de seguir el directo a diario. Que la audiencia reaccionara en el chat NO lo convierte en viral: los suyos reaccionan a cosas que a un desconocido no le dicen nada.

Se duro con clip_score. Reservalo asi: 80-100 solo si lo compartirias tu mismo; 60-79 bueno para los seguidores del canal pero no fuera; 30-59 flojo; 0-29 no es un clip. La mayoria de los fragmentos de un directo normal estan por debajo de 50, y eso es la respuesta correcta. Un fragmento sin gracia no pasa de 45 aunque este bien jugado y el chat se venga arriba; y si dudas entre dos notas, decide con la pregunta del punto 1.

Para cada fragmento devuelve:
- id
- title: titulo en el idioma del fragmento, max 60 caracteres, sin clickbait vacio, concreto sobre lo que pasa
- description: 1-2 frases explicando que ocurre y por que funcionaria (o no) como clip
- category: reaccion | gracioso | habilidad | fail | polemica | informativo | otro
- hook: que se ve u oye en los primeros 2 segundos del fragmento, max 80 caracteres. Si no hay nada que enganche, dilo tal cual.
- clip_title: el titulo que va QUEMADO encima del video vertical, max 42 caracteres. Otro registro que `title`: como lo escribiria el propio streamer en el post, natural y con gracia, nada de resumen periodistico. Uno o dos emojis que aporten (el remate, la emocion), no de adorno ni al principio de la frase. Tono de internet en espanol, algo autoparodico, sin exclamaciones vacias ni mayusculas gritadas.
  Y sobre todo CONCRETO: tiene que nombrar la cosa de este clip. Quien lo lee esta
  decidiendo en dos segundos si se queda, y una plantilla que le encaja a cualquier video
  ("cuando te cruzas a los pesados de siempre", "momentos que no tienen sentido") no le
  dice nada y se va. Saca del transcript o de `visto_en_pantalla` el sustantivo que lo
  hace este momento y no otro: el bicho, el objeto, el juego, el mote, la cifra, la
  persona. Si el clip es de zombies, que salga el zombie; si es una muerte tonta con
  lava, que salga la lava. Regla para comprobarlo: si el titulo valdria igual para otro
  clip distinto, esta mal y hay que rehacerlo con lo que se ve o se oye aqui.
  Prohibido inventarse lo que no aparece: concreto, pero solo con lo que hay.
- sfx: si al clip le pega un efecto de sonido de edicion. "riser_golpe" cuando hay una subida de tension que desemboca en algo (un susto, una aparicion, un remate que se ve venir): el riser sube y el golpe cae encima. "golpe" cuando el remate llega de golpe sin aviso (un fallo, una frase lapidaria, una muerte tonta). "ninguno" cuando meterlo quedaria forzado y cutre: conversacion tranquila, explicaciones, anecdotas sin punto de giro. Ante la duda, "ninguno": un efecto mal puesto se nota mas que su ausencia.
- music: musica de fondo a volumen muy bajo, casi subliminal, para que el clip no suene a habitacion vacia. Por defecto TODO clip lleva musica: elige la que le pegue al tono. "fluffing_a_duck" para lo comico, lo absurdo y la charla con gracia, la mas socorrida y la que eliges si dudas. "sneaky_snitch" para lo travieso, lo que se hace a escondidas o con mala idea. "sneaky_adventure" para lo que tiene aire de aventura, exploracion o tension contenida. "ninguna" solo en el caso excepcional de que el clip ya traiga su propia musica o un ruido continuo con el que la pista chocaria; que un momento sea de hablar tranquilo NO es motivo para dejarlo sin musica.
- clip_score: 0-100 segun los criterios de arriba
- worth_clipping: boolean - false si es una falsa alarma (el chat reacciono a algo externo, es un anuncio, es un raid), si no se entiende sin contexto, o si simplemente no daria para un clip que alguien comparta

Si un fragmento trae `visto_en_pantalla`, es una pista de otro modelo que solo vio un
fotograma, sin audio ni contexto: puede equivocarse en la semantica del juego. Usala
como indicio, pero si el transcript la contradice, hazle caso al transcript.

Devuelve SOLO un array JSON. Nada de markdown ni backticks.

Fragmentos:
"""

CALIBRATE_PROMPT = """Estos fotogramas son una muestra de un mismo directo. Dime:
- game: que juego o actividad es
- routine: 5-8 cosas que en ESTE directo son RUTINA y no merecen un clip. Se concreto: entidades, acciones y pantallas habituales de este juego.
- notable: 5-8 cosas que en este juego SI serian un momento digno de clip
Devuelve solo el JSON."""

CALIBRATE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "game": {"type": "STRING"},
        "routine": {"type": "ARRAY", "items": {"type": "STRING"}},
        "notable": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["game", "routine", "notable"],
}

VISION_PROMPT = """Eres un editor de clips de un directo. Te doy fotogramas del VOD, cada uno precedido por su timestamp.

Marca SOLO los que muestran algo que funcionaria como clip corto para redes: algo que un desconocido entenderia de un vistazo y le haria parar el scroll.
- un logro o hito: objeto o equipo raro conseguido, construccion terminada, nivel superado, marcador alto
- peligro, muerte o fallo del jugador
- algo inesperado, raro o gracioso en pantalla
- una reaccion visible del streamer en la webcam (sorpresa, risa, susto)

NO marques: juego rutinario (andar, minar, colocar bloques sueltos), menus e inventarios sin nada notable, pantallas de espera o de carga, o al streamer simplemente hablando.
{context}
Ante la duda, marca rutina. Es mejor no proponer nada que proponer un momento aburrido.

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
            "hook": {"type": "STRING"},
            "clip_title": {"type": "STRING"},
            "sfx": {"type": "STRING", "enum": ["ninguno", "golpe", "riser_golpe"]},
            "music": {
                "type": "STRING",
                "enum": ["ninguna", "fluffing_a_duck", "sneaky_snitch", "sneaky_adventure"],
            },
            "clip_score": {"type": "NUMBER"},
            "worth_clipping": {"type": "BOOLEAN"},
        },
        "required": [
            "id", "title", "description", "category", "hook", "clip_title", "sfx",
            "music", "clip_score", "worth_clipping",
        ],
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
            item = {
                "id": frag["id"],
                "timestamp": frag.get("timestamp", ""),
                "duracion_s": round(float(frag.get("duration", 0.0)), 1),
                "score_reaccion": round(float(frag.get("signal_score", 0.0)), 2),
                "mensajes_chat": int(frag.get("msg_count", 0)),
                "usuarios_distintos": int(frag.get("unique_users", 0)),
                "pico_audio_sigma": round(float(frag.get("audio_z", 0.0)), 2),
                "transcripcion": frag.get("transcript", "") or "(sin habla detectada)",
            }
            # La pista del proponente visual tiene que llegar al modelo: sin esto, un
            # candidato propuesto por vision se juzgaba solo por su transcript.
            if frag.get("visto_en_pantalla"):
                item["visto_en_pantalla"] = frag["visto_en_pantalla"]
                item["propuesto_por"] = "vision"
            lines.append(json.dumps(item, ensure_ascii=False))
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

    async def calibrate_vision(self, frames: list[tuple[float, Path]]) -> VisionContext:
        """Establece la linea base del VOD: que es rutina aqui y que seria notable.

        Sin esto el modelo juzga cada fotograma aislado y marca como "inesperado"
        cualquier cosa que no conozca: un zombi de noche en Minecraft acaba propuesto
        como momento. Con la linea base del propio VOD, la precision cambia por completo.
        """
        if not self.configured:
            raise ScorerUnavailable("GEMINI_API_KEY no configurada")
        if not frames:
            return VisionContext()

        parts: list[dict[str, Any]] = [{"text": CALIBRATE_PROMPT}]
        for _t, path in frames:
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
        await self.limiter.acquire(1100 * len(frames) + 600)
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": CALIBRATE_SCHEMA,
                "temperature": 0.2,
                "maxOutputTokens": 2048,
            },
        }
        data = _raw_json(await self._generate(body, timeout=180.0, models=self._model_chain()))
        if not isinstance(data, dict):
            return VisionContext()
        return VisionContext(
            game=str(data.get("game") or "").strip()[:80],
            routine=[str(x).strip()[:140] for x in (data.get("routine") or [])][:10],
            notable=[str(x).strip()[:140] for x in (data.get("notable") or [])][:10],
        )

    async def look_at_frames(
        self, frames: list[tuple[float, Path]], context: VisionContext | None = None
    ) -> list[VisualHit]:
        """Pregunta a Gemini que fotogramas muestran algo digno de un clip."""
        if not self.configured:
            raise ScorerUnavailable("GEMINI_API_KEY no configurada")
        if not frames:
            return []

        parts: list[dict[str, Any]] = [
            {"text": VISION_PROMPT.format(context=(context.as_prompt() if context else ""))}
        ]
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
                hook=str(item.get("hook") or "").strip()[:160],
                clip_title=str(item.get("clip_title") or "").strip()[:90],
                sfx=(
                    str(item.get("sfx") or "ninguno").strip().lower()
                    if str(item.get("sfx") or "").strip().lower()
                    in ("ninguno", "golpe", "riser_golpe")
                    else "ninguno"
                ),
                music=(
                    str(item.get("music") or "ninguna").strip().lower()
                    if str(item.get("music") or "").strip().lower()
                    in ("ninguna", "fluffing_a_duck", "sneaky_snitch", "sneaky_adventure")
                    else "ninguna"
                ),
                clip_score=max(0.0, min(100.0, clip_score)),
                worth_clipping=bool(item.get("worth_clipping", True)),
            )
        )
    return out
