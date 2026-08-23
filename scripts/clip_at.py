"""Corta un clip de un instante concreto de un directo que ya este descargado.

El pipeline decide el que: mide senales, elige candidatos y puntua. Pero a veces uno sabe
el momento -lo recuerda, o lo ha encontrado con `find_phrase.py`- y lo unico que quiere es
ese corte. Esto hace lo mismo que haria el pipeline con esa ventana: transcribe, se la pasa
al scorer para que ponga titulo, musica y efectos con el criterio de siempre, saca la
miniatura, renderiza y (si se le pide) lo publica en Supabase como cualquier otro clip.

    python scripts/clip_at.py --vod v2850022597 --start 4061 --end 4078.5
    python scripts/clip_at.py --vod v2850022597 --start 4061 --end 4078.5 --publish
    python scripts/clip_at.py … --title "ayer Jopa, hoy PoliSpawn 💀"   # titulo a mano

Necesita el wav del directo en `backend/data/media/`. Los segundos son los del VOD, los
mismos que dan `find_phrase.py` y la barra de Twitch.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import db, queue, service  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline import frames, render  # noqa: E402
from app.pipeline.orchestrator import new_id  # noqa: E402
from app.pipeline.score import Fragment, ScoringEngine  # noqa: E402
from app.pipeline.transcribe import TranscriptionEngine  # noqa: E402
from app.providers.supabase import Supabase, configured  # noqa: E402


def parse(argv: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {"publicar": False, "titulo": "", "peak": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--vod":
            opts["vod"] = argv[i + 1]
            i += 2
        elif a == "--start":
            opts["start"] = float(argv[i + 1])
            i += 2
        elif a == "--end":
            opts["end"] = float(argv[i + 1])
            i += 2
        elif a == "--peak":
            opts["peak"] = float(argv[i + 1])
            i += 2
        elif a == "--title":
            opts["titulo"] = argv[i + 1]
            i += 2
        elif a == "--publish":
            opts["publicar"] = True
            i += 1
        else:
            print(f"opcion desconocida: {a}")
            raise SystemExit(2)
    if not all(k in opts for k in ("vod", "start", "end")):
        print(__doc__)
        raise SystemExit(2)
    return opts


async def main() -> None:
    opts = parse(sys.argv[1:])
    t0, t1 = float(opts["start"]), float(opts["end"])
    if t1 - t0 < 5.0:
        print("La ventana es demasiado corta: pide al menos cinco segundos")
        raise SystemExit(2)

    await db.connect()
    sb = Supabase() if (opts["publicar"] and configured()) else None
    try:
        video = db.row_to_dict(
            await db.fetch_one("SELECT * FROM videos WHERE ext_id=?", (opts["vod"],))
        )
        if not video:
            print(f"El directo {opts['vod']} no esta en la base local")
            raise SystemExit(2)
        audio = Path(str(video.get("audio_path") or ""))
        if not audio.exists():
            audio = settings.media_dir / f"{video['platform']}-{video['ext_id']}.wav"
        if not audio.exists():
            print(f"Falta el audio del directo ({audio})")
            raise SystemExit(2)

        # 1. La transcripcion de la ventana, con tiempos absolutos: es lo que se quema.
        with tempfile.TemporaryDirectory(prefix="clipper-clip-") as tmp:
            transcriber = TranscriptionEngine()
            await transcriber.prepare()
            transcript, proveedor = await transcriber.transcribe_window(
                audio, t0, t1, work_dir=Path(tmp), tag="manual"
            )
        # El `Word` del proveedor es un dataclass, no el pydantic de la API.
        palabras = [asdict(w) for w in transcript.words]
        print(f"transcrito con {proveedor}: {len(palabras)} palabras")
        print(f"  «{transcript.text.strip()[:200]}»")

        # 2. El scorer pone titulo, musica y efectos con el criterio de siempre.
        frag = Fragment(
            id=new_id("mom"), t_start=t0, t_end=t1,
            t_peak=float(opts["peak"] or (t0 + t1) / 2),
            # Sin senales medidas: no es que la audiencia lo ignorara, es que el momento
            # lo eligio una persona. Se le da un valor neutro, como a los de vision.
            signal_score=0.5, chat_z=0.0, audio_z=0.0, unique_users=0, msg_count=0,
            combo=False, source="manual", transcript=transcript.text,
            language=transcript.language, words=palabras,
        )
        scorer = ScoringEngine()
        await scorer.prepare()
        scores, enriquecido = await scorer.score([frag], chat_available=False)
        s = scores[0]
        titulo = opts["titulo"] or s.clip_title or s.title
        print(f"puntuado: {s.clip_score:.0f}/100 {s.category} "
              f"sfx={s.sfx} musica={s.music} | «{titulo}»")
        if not s.worth_clipping:
            print("  (el modelo no lo habria elegido, pero lo has pedido tu)")

        # 3. El momento, como cualquier otro, para que el render sea el de siempre.
        ahora = time.time()
        await db.execute(
            """INSERT INTO moments
                   (id, job_id, video_id, t_start, t_end, t_peak, title, description,
                    category, hook, clip_title, sfx_fit, music, transcript, words,
                    language, enriched, final_score, clip_score, source, rank, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (frag.id, f"manual-{int(ahora)}", video["id"], t0, t1, frag.t_peak,
             s.title or titulo, s.description, s.category, s.hook, titulo, s.sfx, s.music,
             transcript.text, db.dumps(palabras), transcript.language,
             1 if (enriquecido and titulo) else 0, s.clip_score / 100.0, s.clip_score,
             "manual", 0, ahora),
        )
        await frames.extract_for_moment(video, frag.id, frag.t_peak)

        # 4. Render y, si se pide, publicacion.
        clip = await service.render_moment_clip(frag.id)
        lufs, pico = await render.measure_loudness(render.clip_path(frag.id))
        print(f"\nCLIP {frag.id}: {clip.duration:.1f}s {clip.size_bytes / 1048576:.1f} MB "
              f"{clip.width}x{clip.height} | {clip.captions} frases | musica={clip.music} "
              f"| {lufs:.1f} LUFS / pico {pico:.1f} dBFS")
        print(f"  fichero: {render.clip_path(frag.id)}")
        if sb:
            await queue.publish_existing(sb, [frag.id])
            print("  publicado en Supabase")
        elif opts["publicar"]:
            print("  (no publicado: falta configurar Supabase)")
    finally:
        if sb:
            await sb.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
