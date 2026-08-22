"""Rellena la portada y la fecha del directo de los clips ya publicados.

Las tarjetas de la galeria necesitan una miniatura y saber de que dia es el directo. Los
clips subidos antes de que existieran esas dos columnas siguen en Supabase sin ellas, y
volver a renderizarlos para eso seria absurdo: la miniatura ya esta en disco y la fecha,
en la base local.

    python scripts/backfill_posters.py            # los que les falte algo
    python scripts/backfill_posters.py --force    # todos, otra vez
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import db, queue  # noqa: E402
from app.providers.supabase import Supabase, configured  # noqa: E402

CLIPS = "clips"


async def main() -> None:
    if not configured():
        print("Falta SUPABASE_URL o SUPABASE_SERVICE_KEY en backend/.env")
        raise SystemExit(2)
    force = "--force" in sys.argv
    await db.connect()
    sb = Supabase()
    try:
        rows = await sb.select(CLIPS, params={
            "select": "id,moment_id,storage_path,poster_path,video_date",
            "order": "created_at.asc",
        })
        done = 0
        for row in rows:
            moment_id = str(row.get("moment_id") or "")
            if not force and row.get("poster_path") and row.get("video_date"):
                continue
            moment = db.row_to_dict(
                await db.fetch_one(
                    """SELECT m.id, m.thumb_path, v.upload_date
                         FROM moments m JOIN videos v ON v.id = m.video_id
                        WHERE m.id = ?""",
                    (moment_id,),
                )
            )
            if not moment:
                print(f"{moment_id}: no esta en la base local, se queda como esta")
                continue
            # La portada va en la carpeta del mp4: un jpg al lado de su clip.
            path = str(row.get("storage_path") or "")
            folder = path.rsplit("/", 1)[0] if "/" in path else "manual"
            poster = await queue.upload_poster(sb, folder, moment)
            patch = {"video_date": str(moment.get("upload_date") or "") or None}
            if poster:
                patch["poster_path"] = poster
            await sb.update(CLIPS, patch, match={"id": f"eq.{row['id']}"})
            print(f"{moment_id}: {poster or 'sin miniatura'} · "
                  f"{patch['video_date'] or 'sin fecha'}")
            done += 1
        print(f"{done}/{len(rows)} clips actualizados")
    finally:
        await sb.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
