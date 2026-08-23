"""Procesa todo lo que haya en la cola de Supabase y termina.

Es el turno programado: la sandbox no esta encendida siempre, asi que cada pocas horas
una sesion nueva arranca esto, se come lo que haya pendiente (analisis nuevos y clips a
rehacer) y se apaga. El backend de siempre (`make dev`) hace lo mismo pero quedandose a
la espera, que es lo que aqui no se puede hacer.

Necesita las claves en el entorno o en `backend/.env`: en un contenedor recien clonado no
hay `.env`, asi que `SUPABASE_URL` y `SUPABASE_SERVICE_KEY` tienen que venir como
variables de entorno del proyecto. Si faltan, lo dice y sale con codigo 2 en vez de
quedarse callado.

    python scripts/drain.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import db, queue  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline import render  # noqa: E402
from app.providers.supabase import Supabase, configured  # noqa: E402

REQUESTS = "clip_requests"
CLIPS = "clips"


async def pendiente(sb: Supabase) -> tuple[list[dict], list[dict]]:
    """Lo que hay por hacer: peticiones sin empezar y clips esperando re-render."""
    peticiones = await sb.select(REQUESTS, params={
        "select": "*",
        "status": "in.(queued,claimed,running)",
        "order": "created_at.asc",
    })
    clips = await sb.select(CLIPS, params={
        "select": "id,moment_id,title,render_status",
        "render_status": "in.(rerender_queued,rendering)",
        "order": "created_at.asc",
    })
    return peticiones, clips


async def main() -> None:
    if not configured():
        print("FALTAN LAS CLAVES: define SUPABASE_URL y SUPABASE_SERVICE_KEY en el entorno")
        print("(en un contenedor nuevo no hay backend/.env: van como variables del proyecto)")
        raise SystemExit(2)
    if not render.have_render_deps():
        print("FALTA ffmpeg: sin el no se puede renderizar nada")
        raise SystemExit(2)

    await db.connect()
    sb = Supabase()
    try:
        peticiones, clips = await pendiente(sb)
        if not peticiones and not clips:
            print("Nada pendiente en la cola")
            return
        for p in peticiones:
            pide = f" pidiendo: {p['prompt']}" if p.get("prompt") else ""
            print(f"pendiente: {p['url']} ({p['clips']} clips, {p['status']}){pide}")
        for c in clips:
            print(f"pendiente: rehacer {c['moment_id']} ({c['render_status']})")

        worker = queue.QueueWorker(drain=True)
        await worker.start()
        if not worker.running:
            print("el worker no arranco")
            raise SystemExit(1)
        await worker.wait()

        # Como quedo cada cosa, que es lo que hay que contar al terminar el turno.
        print("\nresultado:")
        for p in peticiones:
            fila = await sb.select(REQUESTS, params={
                "select": "status,clips_done,message,error", "id": f"eq.{p['id']}", "limit": "1",
            })
            if fila:
                f = fila[0]
                extra = f" ({f['error']})" if f.get("error") else ""
                print(f"  {p['url']}: {f['status']}, {f['clips_done']} clips"
                      f" - {f['message']}{extra}")
        for c in clips:
            fila = await sb.select(CLIPS, params={
                "select": "render_status,version,render_error", "id": f"eq.{c['id']}",
                "limit": "1",
            })
            if fila:
                f = fila[0]
                extra = f" ({f['render_error']})" if f.get("render_error") else ""
                print(f"  {c['moment_id']}: {f['render_status']} v{f['version']}{extra}")
        print(f"\nproyecto: {settings.supabase_url}")
    finally:
        await sb.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
