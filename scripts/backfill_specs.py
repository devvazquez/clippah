"""Rellena la ficha de re-render (`render_spec`) de los clips ya publicados.

Sin esa ficha, un clip solo se puede rehacer en la maquina que hizo el analisis: el resto
solo tiene la fila de Supabase, y ahi no estaba de que VOD sale ni en que segundos. Los
clips subidos antes de que existiera la columna la reciben aqui, leyendo la base local.

    python scripts/backfill_specs.py            # los que no la tengan
    python scripts/backfill_specs.py --force    # todos, otra vez
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import db, service  # noqa: E402
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
            "select": "id,moment_id,title,render_spec", "order": "created_at.asc",
        })
        hechos = 0
        for row in rows:
            moment_id = str(row["moment_id"])
            if row.get("render_spec") and not force:
                continue
            try:
                moment, video = await service.load_moment(moment_id)
            except service.NotFound:
                print(f"{moment_id}: no esta en la base local, no se puede rellenar")
                continue
            spec = service.rerender_spec(moment, video)
            await sb.update(CLIPS, {"render_spec": spec}, match={"id": f"eq.{row['id']}"})
            camaras = "con camaras" if spec["video"].get("cam_layout") else "sin camaras"
            print(f"{moment_id}: {spec['video']['ext_id']} "
                  f"{spec['moment']['t_start']:.0f}-{spec['moment']['t_end']:.0f}s {camaras}")
            hechos += 1
        print(f"\n{hechos}/{len(rows)} fichas escritas")
    finally:
        await sb.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
