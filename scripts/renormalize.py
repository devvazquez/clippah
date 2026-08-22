"""Sube el volumen de los clips ya publicados y los reemplaza en Supabase.

Los primeros clips salieron entre -18 y -23 LUFS, cuando la referencia de TikTok,
Instagram y YouTube es -14: se oian mucho mas bajos que el resto del feed. Esto los
arregla sin volver a renderizar nada, porque el arreglo es solo de audio (el video se
copia) y lo que lleva quemado no cambia.

No usa `publish_existing` a proposito: esa funcion reescribe la fila entera desde el
momento local, y con ella se perderia lo que se haya editado a mano en la interfaz (los
subtitulos corregidos, la musica elegida, los efectos colocados). Aqui solo se cambian la
ruta, la version y el tamano.

    python scripts/renormalize.py            # los que esten por debajo del objetivo
    python scripts/renormalize.py --dry-run  # solo medir y decir que haria
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import db, service  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline import render  # noqa: E402
from app.providers.supabase import Supabase, configured  # noqa: E402

CLIPS = "clips"
# Se da por bueno lo que este a menos de 1 dB del objetivo: repetir el AAC por medio
# decibelio no compensa.
MARGEN_DB = 1.0


async def local_clip(moment_id: str) -> Path | None:
    """El mp4 en disco, renderizandolo si hiciera falta (la cache lo devuelve tal cual)."""
    path = render.clip_path(moment_id)
    if path.exists() and path.stat().st_size > 4096:
        return path
    try:
        await service.render_moment_clip(moment_id)
    except Exception as exc:  # noqa: BLE001 - un clip que no se pueda rehacer no para el resto
        print(f"  no se pudo renderizar: {exc}")
        return None
    return path if path.exists() else None


async def main() -> None:
    if not configured():
        print("Falta SUPABASE_URL o SUPABASE_SERVICE_KEY en backend/.env")
        raise SystemExit(2)
    dry = "--dry-run" in sys.argv
    await db.connect()
    sb = Supabase()
    try:
        rows = await sb.select(CLIPS, params={
            "select": "id,moment_id,storage_path,version,title,render_status",
            "order": "created_at.asc",
        })
        hechos = 0
        for row in rows:
            moment_id = str(row["moment_id"])
            print(f"{moment_id} {str(row.get('title'))[:38]}")
            if row.get("render_status") != "ready":
                # El worker lo esta rehaciendo: sale ya con el volumen bueno, y tocarlo
                # aqui seria pisarle el fichero por debajo.
                print(f"  lo esta rehaciendo el worker ({row.get('render_status')})")
                continue
            path = await local_clip(moment_id)
            if not path:
                continue
            lufs, peak = await render.measure_loudness(path)
            if lufs is None:
                print("  sin medida, se queda como esta")
                continue
            if lufs >= settings.render_target_lufs - MARGEN_DB:
                print(f"  ya esta a {lufs:.1f} LUFS")
                continue
            if dry:
                print(f"  {lufs:.1f} LUFS (pico {peak:.1f}) -> se subiria")
                continue

            gain = await render.normalize_loudness(path)
            if not gain:
                print(f"  {lufs:.1f} LUFS: no se pudo subir")
                continue
            despues, _ = await render.measure_loudness(path)

            # Ruta nueva por version, como en el re-render: una URL firmada apunta a un
            # objeto concreto, asi que reemplazarlo por debajo deja a los navegadores
            # sirviendo el mp4 viejo de su cache.
            old = str(row["storage_path"])
            folder = old.rsplit("/", 1)[0] if "/" in old else "manual"
            version = int(row.get("version") or 1) + 1
            new = f"{folder}/{moment_id}-v{version}.mp4"
            await sb.upload(new, path.read_bytes(), content_type="video/mp4")
            await sb.update(CLIPS, {
                "storage_path": new,
                "version": version,
                "size_bytes": path.stat().st_size,
            }, match={"id": f"eq.{row['id']}"})
            if old != new:
                await sb.delete(old)
            print(f"  {lufs:.1f} -> {despues:.1f} LUFS ({gain:+.1f} dB), v{version}")
            hechos += 1
        print(f"\n{hechos}/{len(rows)} clips subidos de volumen")
    finally:
        await sb.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
