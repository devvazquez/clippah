"""Sube a Supabase clips que ya estan renderizados en la sandbox.

Los clips hechos antes de conectar Supabase estan en disco pero no en la tabla `clips`, asi
que la interfaz no los ve. Esto los publica sin volver a renderizar nada.

    python scripts/push_clips.py                # todos los que tengan mp4 en disco
    python scripts/push_clips.py mom_a mom_b    # solo esos
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import db, queue  # noqa: E402
from app.providers.supabase import Supabase, configured  # noqa: E402


async def main() -> None:
    if not configured():
        print("Falta SUPABASE_URL o SUPABASE_SERVICE_KEY en backend/.env")
        raise SystemExit(2)
    await db.connect()
    sb = Supabase()
    try:
        ids = sys.argv[1:]
        if not ids:
            rows = await db.fetch_all(
                """SELECT id FROM moments
                   WHERE clip_path IS NOT NULL AND clip_path <> ''
                   ORDER BY created_at DESC"""
            )
            ids = [str(db.row_to_dict(r)["id"]) for r in rows]
        if not ids:
            print("No hay clips renderizados en disco")
            return
        print(f"Subiendo {len(ids)} clips…")
        published = await queue.publish_existing(sb, ids)
        print(f"{len(published)}/{len(ids)} publicados")
    finally:
        await sb.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
