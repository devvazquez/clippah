"""Busca una frase en el audio de un directo y dice en que segundo se dice.

El analisis solo transcribe las ventanas candidatas, asi que si alguien dice algo fuera de
esas ventanas no queda por ningun lado. Esto pasa el VOD entero por Whisper en local (sin
gastar cuota de nadie), guarda la transcripcion completa en `backend/data/transcripts/` y
busca los patrones. La segunda vez que se busca en el mismo directo ya no transcribe: lee
el fichero.

    python scripts/find_phrase.py jopa plex                  # en todos los VODs con audio
    python scripts/find_phrase.py --vod v2851488297 jopa     # en uno
    python scripts/find_phrase.py --model small jopa         # mas fino y mas lento

Con `tiny` (el de serie) un nombre propio puede salir mal escrito, asi que conviene buscar
varias formas: `jopa yopa hopa`.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402


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


def cached(ext_id: str, wav: Path, model_name: str) -> list[tuple[float, float, str]]:
    """La transcripcion completa, de disco si ya estaba."""
    carpeta = settings.data_dir / "transcripts"
    carpeta.mkdir(parents=True, exist_ok=True)
    fichero = carpeta / f"{ext_id}.{model_name}.tsv"
    if fichero.exists():
        filas = []
        for linea in fichero.read_text(encoding="utf-8").splitlines():
            a, b, texto = linea.split("\t", 2)
            filas.append((float(a), float(b), texto))
        print(f"{ext_id}: {len(filas)} segmentos ya transcritos")
        return filas
    print(f"{ext_id}: transcribiendo {wav.stat().st_size / 1048576:.0f} MB con {model_name}…")
    t0 = time.monotonic()
    filas = transcribe(wav, model_name)
    fichero.write_text(
        "\n".join(f"{a:.2f}\t{b:.2f}\t{t}" for a, b, t in filas), encoding="utf-8"
    )
    print(f"{ext_id}: {len(filas)} segmentos en {time.monotonic() - t0:.0f}s -> {fichero}")
    return filas


def main() -> None:
    args = sys.argv[1:]
    solo_vod, model_name = "", "tiny"
    patrones = []
    i = 0
    while i < len(args):
        if args[i] == "--vod":
            solo_vod = args[i + 1]
            i += 2
        elif args[i] == "--model":
            model_name = args[i + 1]
            i += 2
        else:
            patrones.append(args[i].lower())
            i += 1
    if not patrones:
        print(__doc__)
        raise SystemExit(2)

    wavs = sorted(settings.media_dir.glob("*.wav"), key=lambda p: p.stat().st_size)
    if solo_vod:
        wavs = [w for w in wavs if solo_vod in w.name]
    if not wavs:
        print("No hay wavs en", settings.media_dir)
        raise SystemExit(2)

    total = 0
    for wav in wavs:
        ext_id = wav.stem.replace("twitch-", "")
        filas = cached(ext_id, wav, model_name)
        for inicio, _fin, texto in filas:
            bajo = texto.lower()
            for p in patrones:
                if re.search(rf"\b{re.escape(p)}", bajo):
                    print(f"  >>> {ext_id} {hhmmss(inicio)} ({inicio:.1f}s): {texto}")
                    total += 1
                    break
    print(f"\n{total} coincidencias de {', '.join(patrones)}")


if __name__ == "__main__":
    main()
