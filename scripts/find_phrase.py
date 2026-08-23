"""Busca una frase en el audio de un directo y dice en que segundo se dice.

El analisis solo transcribe las ventanas candidatas, asi que si alguien dice algo fuera de
esas ventanas no queda por ningun lado. Esto transcribe el VOD entero, lo guarda en
`backend/data/transcripts/` y busca los patrones. La segunda vez que se busca en el mismo
directo ya no transcribe: lee el fichero.

    python scripts/find_phrase.py --groq --vod v2851488297 jopa yopa
    python scripts/find_phrase.py jopa plex                  # local, en todo lo que haya
    python scripts/find_phrase.py --model small jopa          # local mas fino y mas lento

**Para buscar nombres propios hace falta `--groq`.** Whisper en local con `tiny` o `small`
destroza el habla rapida y con ruido de juego de fondo ("un sigo, un senor", "el arte de
modismo de vida"): sirve para hacerse una idea, no para buscar un nombre, porque el nombre
sale escrito de cualquier manera. `--groq` usa `whisper-large-v3-turbo`, acierta, y va a
unas 30 veces el tiempo real; a cambio gasta cuota (`GROQ_ASD` la limita, y el propio
cliente se para antes de pasarse). Aun con Groq conviene buscar variantes: `jopa yopa`.

`--vod` se puede repetir, y entonces se respeta ese orden (util para ir del directo mas
probable al menos probable y parar en cuanto aparezca).
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.utils import ffmpeg_bin, ffprobe_bin, run  # noqa: E402

# Diez minutos por peticion: en mp3 mono a 64 kbps son unos 5 MB, con margen de sobra
# sobre los 25 MB que acepta Groq.
CHUNK_S = 600


def hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def transcribe(wav: Path, model_name: str) -> list[tuple[float, float, str]]:
    """Transcribe el wav entero. Devuelve (inicio, fin, texto) por segmento."""
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    # `vad_filter` se salta el silencio, que en un directo de Minecraft es la mitad del
    # tiempo: es lo que hace esto viable en local.
    segments, _ = model.transcribe(
        str(wav), language="es", vad_filter=True, beam_size=1, condition_on_previous_text=False
    )
    out = []
    inicio = time.monotonic()
    for seg in segments:
        out.append((seg.start, seg.end, seg.text.strip()))
        if len(out) % 200 == 0:
            print(f"  … {hhmmss(seg.end)} transcrito ({time.monotonic() - inicio:.0f}s)",
                  flush=True)
    return out


async def transcribe_groq(wav: Path) -> tuple[list[tuple[float, float, str]], bool]:
    """Transcribe el wav entero con Groq, por trozos, sumando el desplazamiento.

    Se trocea porque Groq acepta 25 MB por peticion, y en mp3 mono a 64 kbps diez minutos
    son cinco. Las palabras vienen con tiempo, asi que se agrupan en lineas cortas para
    que al buscar se lea el contexto.
    """
    from app.providers.groq import GroqTranscriber

    groq = GroqTranscriber()
    if not groq.configured:
        print("Falta GROQ_API_KEY")
        raise SystemExit(2)

    medida = await run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(wav)]
    )
    duracion = float(medida.stdout.strip())
    # Cada trozo se guarda en cuanto se transcribe. La cuota por hora de Groq se agota
    # antes de acabar un directo largo, y sin esto el intento siguiente empezaria de cero
    # (o peor: guardaria media transcripcion como si estuviera entera).
    partes = settings.data_dir / "transcripts" / f"{wav.stem}.groq.parts"
    partes.mkdir(parents=True, exist_ok=True)

    filas: list[tuple[float, float, str]] = []
    completo = True
    with tempfile.TemporaryDirectory(prefix="clipper-buscar-") as tmp:
        for i, inicio in enumerate(range(0, int(duracion), CHUNK_S)):
            hecho = partes / f"{i:03d}.tsv"
            if hecho.exists():
                for linea in hecho.read_text(encoding="utf-8").splitlines():
                    a, b, texto = linea.split("\t", 2)
                    filas.append((float(a), float(b), texto))
                continue
            trozo = Path(tmp) / f"trozo{i:03d}.mp3"
            await run(
                [ffmpeg_bin(), "-v", "error", "-nostdin", "-y", "-ss", str(inicio),
                 "-t", str(CHUNK_S), "-i", str(wav), "-ac", "1", "-b:a", "64k",
                 str(trozo)]
            )
            if not await groq.has_room(trozo):
                print(f"  cuota de Groq agotada en {hhmmss(inicio)}: se para aqui y lo que"
                      " lleva transcrito se guarda; vuelve a lanzarlo cuando libere")
                completo = False
                break
            t = await groq.transcribe(trozo, "es")
            nuevas: list[tuple[float, float, str]] = []
            linea: list[str] = []
            desde = float(inicio)
            for w in t.words:
                if not linea:
                    desde = inicio + w.start
                linea.append(w.text)
                if len(linea) >= 10:
                    nuevas.append((desde, inicio + w.end, " ".join(linea).strip()))
                    linea = []
            if linea:
                nuevas.append((desde, min(duracion, inicio + CHUNK_S), " ".join(linea).strip()))
            hecho.write_text(
                "\n".join(f"{a:.2f}\t{b:.2f}\t{t}" for a, b, t in nuevas), encoding="utf-8"
            )
            filas.extend(nuevas)
            print(f"  … {hhmmss(inicio + CHUNK_S)} transcrito", flush=True)
    return filas, completo


def cached(
    ext_id: str, wav: Path, model_name: str, groq: bool = False
) -> list[tuple[float, float, str]]:
    """La transcripcion completa, de disco si ya estaba."""
    carpeta = settings.data_dir / "transcripts"
    carpeta.mkdir(parents=True, exist_ok=True)
    fichero = carpeta / f"{ext_id}.{'groq' if groq else model_name}.tsv"
    if fichero.exists():
        filas = []
        for linea in fichero.read_text(encoding="utf-8").splitlines():
            a, b, texto = linea.split("\t", 2)
            filas.append((float(a), float(b), texto))
        print(f"{ext_id}: {len(filas)} segmentos ya transcritos")
        return filas
    motor = "groq" if groq else model_name
    print(f"{ext_id}: transcribiendo {wav.stat().st_size / 1048576:.0f} MB con {motor}…")
    t0 = time.monotonic()
    if groq:
        filas, completo = asyncio.run(transcribe_groq(wav))
    else:
        filas, completo = transcribe(wav, model_name), True
    # El fichero final solo se escribe si el directo esta entero: con media
    # transcripcion guardada como completa, la busqueda diria "no esta" mintiendo. Los
    # trozos ya hechos se quedan en `.groq.parts` y el siguiente intento sigue por ahi.
    if completo:
        fichero.write_text(
            "\n".join(f"{a:.2f}\t{b:.2f}\t{t}" for a, b, t in filas), encoding="utf-8"
        )
        print(f"{ext_id}: {len(filas)} segmentos en {time.monotonic() - t0:.0f}s -> {fichero}")
    else:
        print(f"{ext_id}: incompleto ({len(filas)} segmentos). Se busca en lo que hay.")
    return filas


def main() -> None:
    args = sys.argv[1:]
    vods: list[str] = []
    model_name, groq = "tiny", False
    patrones = []
    i = 0
    while i < len(args):
        if args[i] == "--vod":
            vods.append(args[i + 1])
            i += 2
        elif args[i] == "--model":
            model_name = args[i + 1]
            i += 2
        elif args[i] == "--groq":
            groq = True
            i += 1
        else:
            patrones.append(args[i].lower())
            i += 1
    if not patrones:
        print(__doc__)
        raise SystemExit(2)

    disponibles = list(settings.media_dir.glob("*.wav"))
    if vods:
        # El orden que pide quien llama, que sabe por donde empezar a buscar.
        wavs = [w for v in vods for w in disponibles if v in w.name]
    else:
        wavs = sorted(disponibles, key=lambda p: p.stat().st_size)
    if not wavs:
        print("No hay wavs en", settings.media_dir)
        raise SystemExit(2)

    total = 0
    for wav in wavs:
        ext_id = wav.stem.replace("twitch-", "")
        filas = cached(ext_id, wav, model_name, groq)
        encontrado = 0
        for inicio, _fin, texto in filas:
            bajo = texto.lower()
            for p in patrones:
                if re.search(rf"\b{re.escape(p)}", bajo):
                    print(f"  >>> {ext_id} {hhmmss(inicio)} ({inicio:.1f}s): {texto}")
                    encontrado += 1
                    break
        print(f"{ext_id}: {encontrado} coincidencias")
        total += encontrado
        if encontrado:
            # Se para aqui a proposito: transcribir lo que queda cuesta cuota y ya
            # tenemos donde mirar.
            print("(se para: ya hay coincidencias en este directo)")
            break
    print(f"\n{total} coincidencias de {', '.join(patrones)}")


if __name__ == "__main__":
    main()
