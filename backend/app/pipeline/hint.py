"""Lo que pide el usuario en una frase: interpretarlo y, si hace falta, ir a buscarlo.

La interfaz manda un enlace y una frase corta: «el clip mas gracioso», «busca donde le
llaman jopa». Con eso se hacen dos cosas distintas:

- **Siempre**: la frase se le pasa al scorer como lo que quiere quien lo pide, asi que
  entre dos momentos parecidos gana el que encaja con lo pedido.
- **Si la frase es una busqueda** («encuentra donde dicen X»), ademas hay que ir a por
  ese momento. El analisis normal solo mira los picos de reaccion, y lo que busca una
  persona puede estar en un tramo tranquilo que ningun pico senala: sin esto, el momento
  no llega ni a transcribirse.

La busqueda no gasta cuota. Mira dos sitios que ya estan pagados:

1. El **chat** del propio VOD, que el pipeline descarga entero de todas formas. Si al
   streamer le llaman algo, en el chat suele estar escrito.
2. Las **transcripciones completas** versionadas en `data/transcripts/*.tsv`, para los
   directos que ya se transcribieron alguna vez.

Transcribir el directo entero solo para buscar costaria la cuota de una hora de Groq, asi
que eso no se hace por las buenas: si no hay chat ni transcripcion, se dice y el analisis
sigue por los picos de siempre.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from .candidates import Candidate

log = logging.getLogger("clipper.hint")

# Palabras que en una peticion no dicen nada: los verbos de mandar («encuentra»), los
# articulos y lo que se repite en todas («clip», «momento», «directo»). Si se dejaran,
# «busca el momento del clip» buscaria la palabra "momento" en el chat.
STOP = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "a", "en",
    "y", "o", "que", "qué", "con", "por", "para", "su", "sus", "lo", "le", "les", "me",
    "mi", "se", "es", "esta", "este", "esto", "eso", "ese", "esa", "mas", "más", "muy",
    "donde", "dónde", "cuando", "cuándo", "como", "cómo", "quien", "quién",
    "encuentra", "encontrar", "busca", "buscar", "buscame", "sacame", "saca", "genera",
    "generar", "haz", "hazme", "dame", "quiero", "necesito", "ponme", "pon",
    "clip", "clips", "momento", "momentos", "directo", "stream", "video", "vod",
    "parte", "trozo", "fragmento", "cacho", "mejor", "mejores", "sale", "salen",
    "dice", "dicen", "diciendo", "habla", "hablan", "hace", "hacen", "pasa", "pasan",
    "streamer", "canal", "grabacion", "grabación",
}

# Si la frase lleva alguna de estas, no es un gusto ("algo gracioso") sino un encargo de
# buscar una cosa concreta que hay que ir a localizar en el VOD.
SEARCH_CUES = (
    "encuentra", "encontrar", "busca", "buscar", "buscame", "donde", "dónde",
    "cuando", "cuándo", "el momento en que", "la parte en que", "la parte donde",
    "dice", "dicen", "llaman", "confunden", "menciona", "mencionan", "nombra",
    "nombran", "sale", "salen", "aparece",
)

# Cuantas ventanas se anaden como maximo. Cada una se transcribe, y transcribir cuesta
# tiempo y cuota: cuatro sitios donde se dijo la palabra sobran para elegir.
MAX_WINDOWS = 4
# Dos apariciones a menos de esto son el mismo momento, no dos.
CLUSTER_GAP_S = 25.0
# La palabra cae al principio de lo que interesa, no en medio: cuando alguien dice «me
# llaman PoliSpawn», la gracia es lo que viene detras. Y ojo, el tiempo de una linea del
# transcript es el de su primera palabra, asi que la frase buscada puede estar unos
# segundos mas adelante de lo que marca el hit (ver CLAUDE.md). Por eso la cola es larga.
LEAD_S = 5.0
TAIL_S = 16.0
MIN_WINDOW_S = 14.0
MAX_WINDOW_S = 45.0
# El chat reacciona despues de que pase la cosa: un mensaje a los 100 s comenta algo que
# paso sobre los 94 s.
CHAT_LAG_S = 6.0


def _norm(text: str) -> str:
    """Minusculas y sin tildes: en el chat «Jopa» se escribe de seis maneras."""
    plano = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in plano if unicodedata.category(c) != "Mn")


@dataclass(frozen=True)
class Hint:
    """La peticion del usuario, ya masticada."""

    text: str = ""
    #: Lo que hay que buscar: palabras sueltas y frases entre comillas.
    terms: tuple[str, ...] = ()
    #: True si la frase pide localizar algo concreto, no solo elegir con un gusto.
    search: bool = False

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def __bool__(self) -> bool:
        return not self.empty


def parse(text: str | None) -> Hint:
    """Saca de la frase del usuario si es una busqueda y que hay que buscar."""
    raw = (text or "").strip()
    if not raw:
        return Hint()
    plano = _norm(raw)
    search = any(cue in plano for cue in SEARCH_CUES)

    # Lo que va entre comillas es literal: se busca la frase entera, no sus palabras.
    frases = [_norm(m) for m in re.findall(r'["“‘\']([^"”’\']{3,60})', raw)]
    sueltas = [
        w for w in re.findall(r"[a-z0-9ñ]{3,}", plano)
        if w not in STOP and not any(w in f for f in frases)
    ]
    terms = tuple(dict.fromkeys([*frases, *sueltas]))
    # «el mas gracioso» no es una busqueda aunque lleve palabras: sin nada que localizar,
    # la frase solo sirve para orientar al scorer.
    return Hint(text=raw[:300], terms=terms, search=bool(search and terms))


# --------------------------------------------------------------- donde se dice eso


@dataclass
class Hit:
    t: float
    source: str
    quote: str = ""
    #: El termino que hizo saltar este hit, para poder ordenar por cuantos encajan.
    term: str = ""


@dataclass
class Found:
    """Resultado de la busqueda, para poder contarlo tal cual en el aviso al usuario."""

    windows: list[Candidate] = field(default_factory=list)
    chat_hits: int = 0
    transcript_hits: int = 0
    transcript_file: str = ""


def _matches(text: str, terms: tuple[str, ...]) -> str:
    """El termino que aparece en el texto, o cadena vacia. Vale con uno."""
    plano = _norm(text)
    for term in terms:
        if term in plano:
            return term
    return ""


def transcript_path(video_url: str) -> Path | None:
    """La transcripcion completa versionada de este VOD, si esta en el repo.

    Los ficheros se llaman por el id del VOD (`v2850022597.groq.tsv`), asi que basta con
    buscar cual de ellos tiene su id dentro de la URL del video.
    """
    carpeta = settings.data_dir / "transcripts"
    if not carpeta.is_dir():
        return None
    for path in sorted(carpeta.glob("*.tsv")):
        vod = path.name.split(".")[0].lstrip("v")
        if vod and vod in video_url:
            return path
    return None


def _from_transcript(path: Path, terms: tuple[str, ...]) -> list[Hit]:
    hits: list[Hit] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        partes = line.split("\t")
        if len(partes) < 3:
            continue
        term = _matches(partes[2], terms)
        if not term:
            continue
        try:
            hits.append(
                Hit(t=float(partes[0]), source="transcript", quote=partes[2][:120], term=term)
            )
        except ValueError:
            continue
    return hits


def _cluster(hits: list[Hit], duration: float) -> list[tuple[float, float, list[Hit]]]:
    """Agrupa lo que esta pegado y devuelve la ventana de cada grupo."""
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h.t)
    grupos: list[list[Hit]] = [[hits[0]]]
    for hit in hits[1:]:
        if hit.t - grupos[-1][-1].t <= CLUSTER_GAP_S:
            grupos[-1].append(hit)
        else:
            grupos.append([hit])

    ventanas = []
    for grupo in grupos:
        inicio = max(0.0, grupo[0].t - LEAD_S)
        fin = min(duration, max(grupo[-1].t + TAIL_S, inicio + MIN_WINDOW_S))
        if fin - inicio > MAX_WINDOW_S:
            fin = inicio + MAX_WINDOW_S
        ventanas.append((inicio, fin, grupo))
    return ventanas


def _note(grupo: list[Hit], terms: tuple[str, ...]) -> str:
    """Que se encontro aqui, en una linea, para que el scorer sepa por que esta."""
    cita = next((h.quote for h in grupo if h.quote), "")
    donde = "el chat" if grupo[0].source == "chat" else "la transcripcion"
    texto = f"aqui aparece lo que se pide ({', '.join(terms[:3])}) en {donde}"
    return f"{texto}: «{cita.strip()}»" if cita else texto


def find(
    hint: Hint,
    *,
    duration: float,
    video_url: str,
    chat: list[tuple[float, str]],
) -> Found:
    """Ventanas del VOD donde aparece lo que pide el usuario.

    `chat` son los mensajes ya cargados (t, texto): se pasan de fuera para no volver a
    consultar la base ni depender de ella.
    """
    found = Found()
    if not hint.search or not hint.terms:
        return found

    hits: list[Hit] = []
    for t, text in chat:
        term = _matches(text, hint.terms)
        if term:
            hits.append(
                Hit(t=max(0.0, t - CHAT_LAG_S), source="chat", quote=text[:80], term=term)
            )
    found.chat_hits = len(hits)

    path = transcript_path(video_url)
    if path is not None:
        del_transcript = _from_transcript(path, hint.terms)
        found.transcript_hits = len(del_transcript)
        found.transcript_file = path.name
        hits.extend(del_transcript)

    if not hits:
        return found

    # Con muchisimas apariciones la palabra es de relleno («risa» en un directo de risas):
    # entonces no senala un momento y no merece la pena forzar ventanas.
    if len(hits) > 120:
        log.info("la busqueda sale %d veces: demasiado comun para senalar un momento", len(hits))
        return found

    ventanas = _cluster(hits, duration)
    # Delante, donde encajan mas terminos distintos: un sitio donde salen «llaman» y
    # «jopa» a la vez es el que se busca, y uno donde solo sale «llaman» es ruido. A
    # igualdad de terminos, donde mas se repite.
    ventanas.sort(key=lambda v: (-len({h.term for h in v[2]}), -len(v[2])))
    for inicio, fin, grupo in ventanas[:MAX_WINDOWS]:
        found.windows.append(
            Candidate(
                t_peak=grupo[len(grupo) // 2].t,
                t_start=inicio,
                t_end=fin,
                # Los candidatos pedidos no compiten por senal: se normalizan aparte
                # (ver `score.finalize`), asi que este numero no los ordena.
                signal_score=0.0,
                chat_z=0.0,
                audio_z=0.0,
                unique_users=0,
                msg_count=sum(1 for h in grupo if h.source == "chat"),
                chat_ratio=0.0,
                combo=False,
                source="hint",
                vision_note=_note(grupo, hint.terms),
            )
        )
    return found


def merge(cands: list[Candidate], windows: list[Candidate]) -> list[Candidate]:
    """Anade las ventanas pedidas, sin duplicar las que ya estaban entre los picos."""
    fuera = []
    for w in windows:
        solapa = any(
            not (w.t_end <= c.t_start or w.t_start >= c.t_end)
            and min(w.t_end, c.t_end) - max(w.t_start, c.t_start) > 0.4 * (w.t_end - w.t_start)
            for c in cands
        )
        if not solapa:
            fuera.append(w)
    return [*cands, *fuera]
