"""Comprueba que lo que pide el usuario en una frase se interpreta y se localiza bien.

No toca la red ni gasta cuota: usa las transcripciones que estan en el repo y un chat de
mentira. Es lo que hay que ejecutar despues de tocar `backend/app/pipeline/hint.py`,
porque un fallo aqui no se ve en el resultado (el analisis sigue funcionando, solo que
ignorando lo que se pidio).

    python scripts/check_hint.py
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "backend"))

from app.pipeline import hint  # noqa: E402
from app.pipeline.candidates import Candidate  # noqa: E402

fallos: list[str] = []
hechas = 0


def comprueba(que: str, condicion: bool, detalle: str = "") -> None:
    global hechas
    hechas += 1
    if condicion:
        print(f"  ok   {que}")
    else:
        print(f"  FALLA {que}" + (f" -> {detalle}" if detalle else ""))
        fallos.append(que)


# ------------------------------------------------------- interpretar la peticion
print("interpretar la frase")

vacio = hint.parse("")
comprueba("una peticion vacia no busca nada", not vacio.search and not bool(vacio))
comprueba("y no tiene terminos", vacio.terms == ())

gusto = hint.parse("el mas gracioso")
comprueba("«el mas gracioso» NO es una busqueda", not gusto.search, str(gusto))
comprueba("pero se guarda para el scorer", gusto.text == "el mas gracioso")
comprueba("y saca la palabra que importa", gusto.terms == ("gracioso",), str(gusto.terms))

busca = hint.parse("busca donde le llaman jopa")
comprueba("«busca donde le llaman jopa» SI es una busqueda", busca.search)
comprueba("los verbos de mandar no son terminos", "busca" not in busca.terms, str(busca.terms))
comprueba("el nombre propio si lo es", "jopa" in busca.terms, str(busca.terms))

comillas = hint.parse('encuentra donde dice "no me lo creo"')
comprueba("lo que va entre comillas es un termino entero",
          "no me lo creo" in comillas.terms, str(comillas.terms))
comprueba("y sus palabras no se buscan por separado",
          "creo" not in comillas.terms, str(comillas.terms))

tildes = hint.parse("busca donde dice adiós")
comprueba("las tildes se quitan para poder comparar",
          "adios" in tildes.terms, str(tildes.terms))

largo = hint.parse("x" * 900)
comprueba("una frase larguisima se recorta a 300", len(largo.text) == 300, str(len(largo.text)))

# ---------------------------------------------------------------- localizarlo
print("\nlocalizar el momento")

VOD = "https://www.twitch.tv/videos/2850022597"
DURACION = 6100.0
# El momento de verdad: en el directo del 18 de agosto, a 1:07:42, dice «ayer me llamaron
# Jopa, hoy me llaman PoliSpawn». Es el clip que se saco a mano y el que esta busqueda
# tiene que encontrar sola.
REAL = 4062.0

path = hint.transcript_path(VOD)
comprueba("encuentra la transcripcion versionada de ese VOD",
          path is not None and path.name == "v2850022597.groq.tsv", str(path))
comprueba("y no confunde un VOD que no tiene transcripcion",
          hint.transcript_path("https://www.twitch.tv/videos/999999999") is None)

sin_busqueda = hint.find(gusto, duration=DURACION, video_url=VOD, chat=[])
comprueba("un gusto no dispara la busqueda", sin_busqueda.windows == [])

solo_transcript = hint.find(busca, duration=DURACION, video_url=VOD, chat=[])
comprueba("con la transcripcion sola ya sale alguna ventana",
          len(solo_transcript.windows) > 0, str(solo_transcript))
comprueba("y una de ellas contiene el momento de verdad",
          any(c.t_start <= REAL <= c.t_end for c in solo_transcript.windows),
          str([(c.t_start, c.t_end) for c in solo_transcript.windows]))

# El chat escribe bien los nombres propios; Whisper los escribe de oido («Hopa»). Por eso
# el chat manda cuando los dos dicen algo.
chat = [(4066.0, "JAJAJA jopa"), (4069.0, "es jopa el confundido"), (900.0, "buenas")]
con_chat = hint.find(busca, duration=DURACION, video_url=VOD, chat=chat)
comprueba("el chat cuenta como fuente", con_chat.chat_hits == 2, str(con_chat.chat_hits))
primera = con_chat.windows[0] if con_chat.windows else None
comprueba("la ventana con mas coincidencias va primera",
          primera is not None and primera.t_start <= REAL <= primera.t_end,
          str(primera))
comprueba("el candidato viene marcado como pedido",
          primera is not None and primera.source == "hint")
comprueba("y lleva la nota de por que esta",
          primera is not None and "jopa" in primera.vision_note.lower())
comprueba("nunca se pasa del tope de ventanas",
          len(con_chat.windows) <= hint.MAX_WINDOWS, str(len(con_chat.windows)))

# Una palabra que sale en todo el directo no senala nada: mejor no forzar ventanas.
comun = hint.find(
    hint.parse("busca donde dice que"),
    duration=DURACION, video_url=VOD,
    chat=[(float(i), "que") for i in range(300)],
)
comprueba("una palabra omnipresente no genera ventanas", comun.windows == [], str(comun))

# --------------------------------------------------------------------- mezclar
print("\nmezclar con los candidatos de siempre")


def cand(t_start: float, t_end: float, source: str = "signals") -> Candidate:
    return Candidate(
        t_peak=(t_start + t_end) / 2, t_start=t_start, t_end=t_end, signal_score=1.0,
        chat_z=0.0, audio_z=0.0, unique_users=0, msg_count=0, chat_ratio=0.0,
        combo=False, source=source,
    )


picos = [cand(100.0, 130.0), cand(4050.0, 4085.0)]
pedidas = [cand(4055.0, 4079.0, "hint"), cand(200.0, 220.0, "hint")]
mezcla = hint.merge(picos, pedidas)
comprueba("una ventana que ya estaba entre los picos no se duplica",
          len(mezcla) == 3, f"{len(mezcla)} candidatos")
comprueba("la que no estaba si se anade",
          any(c.source == "hint" and c.t_start == 200.0 for c in mezcla))
comprueba("los picos originales siguen ahi",
          sum(1 for c in mezcla if c.source == "signals") == 2)
comprueba("mezclar sin nada pedido no cambia nada", hint.merge(picos, []) == picos)

# ---------------------------------------------------------------------- final
print(f"\n{hechas} comprobaciones, {len(fallos)} fallos")
if fallos:
    for f in fallos:
        print(f"  - {f}")
    raise SystemExit(1)
print("la busqueda por peticion funciona")
