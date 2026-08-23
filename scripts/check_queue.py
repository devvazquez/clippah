"""Prueba el worker de la cola contra un Supabase de mentira.

Levanta un servidor que imita lo que el worker usa de PostgREST y de Storage (filtros
`eq.`, `order`, `limit`, `Prefer: return=representation`, subida de objetos) y le da una
peticion en cola. Comprueba el ciclo entero: reclamarla, reflejar el progreso, renderizar,
subir el mp4 y escribir la fila de `clips`.

No hace falta proyecto de Supabase ni claves, y no gasta cuota de nada: el analisis se
sustituye por un job ya terminado de la base local y el render sale de la cache en disco.

    python scripts/check_queue.py
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import uvicorn  # noqa: E402
from app import db, queue, service  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline import render  # noqa: E402
from app.utils import ffprobe_bin  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402

PORT = 8799
REQUEST_ID = "11111111-2222-3333-4444-555555555555"
# La frase que se escribe en la interfaz al encolar.
PEDIDO = "busca donde le llaman jopa"


# ------------------------------------------------------ el Supabase de mentira


class Fake:
    """Estado del proyecto falso: dos tablas en memoria y un bucket en un diccionario."""

    def __init__(self) -> None:
        # Los nombres son los de las tablas: el stub resuelve la ruta con getattr.
        self.clip_requests: list[dict[str, Any]] = []
        self.clips: list[dict[str, Any]] = []
        self.objects: dict[str, int] = {}
        self.patches: list[dict[str, Any]] = []


fake = Fake()
stub = FastAPI()


def _matches(row: dict[str, Any], params: dict[str, str]) -> bool:
    """Solo el subconjunto de filtros de PostgREST que usa el worker."""
    for key, raw in params.items():
        if key in ("select", "order", "limit", "on_conflict"):
            continue
        if raw.startswith("eq."):
            if str(row.get(key)) != raw[3:]:
                return False
        elif raw.startswith("in."):
            allowed = raw[4:-1].split(",")
            if str(row.get(key)) not in allowed:
                return False
    return True


@stub.get("/rest/v1/{table}")
async def select(table: str, request: Request) -> list[dict[str, Any]]:
    rows = [r for r in getattr(fake, table) if _matches(r, dict(request.query_params))]
    order = request.query_params.get("order", "")
    if order:
        field, _, direction = order.partition(".")
        rows.sort(key=lambda r: str(r.get(field) or ""), reverse=direction == "desc")
    limit = request.query_params.get("limit")
    return rows[: int(limit)] if limit else rows


@stub.patch("/rest/v1/{table}")
async def update(table: str, request: Request) -> list[dict[str, Any]]:
    patch = await request.json()
    params = dict(request.query_params)
    touched = []
    for row in getattr(fake, table):
        if _matches(row, params):
            row.update(patch)
            touched.append(row)
            if table == "clip_requests":
                fake.patches.append(dict(patch))
    return touched


@stub.post("/rest/v1/{table}")
async def insert(table: str, request: Request) -> list[dict[str, Any]]:
    row = await request.json()
    rows = getattr(fake, table)
    key = request.query_params.get("on_conflict")
    if key:
        for existing in rows:
            if existing.get(key) == row.get(key):
                existing.update(row)
                return [existing]
    row.setdefault("id", f"{table}-{len(rows) + 1}")
    rows.append(row)
    return [row]


@stub.post("/storage/v1/object/{bucket}/{path:path}")
async def upload(bucket: str, path: str, request: Request) -> dict[str, str]:
    body = await request.body()
    fake.objects[f"{bucket}/{path}"] = len(body)
    return {"Key": f"{bucket}/{path}"}


@stub.delete("/storage/v1/object/{bucket}")
async def remove(bucket: str, request: Request) -> dict[str, str]:
    """Borrado en lote, que es el que usa el cliente (`{"prefixes": [...]}`)."""
    body = await request.json()
    for path in body.get("prefixes", []):
        fake.objects.pop(f"{bucket}/{path}", None)
    return {"message": "Successfully deleted"}


@stub.post("/storage/v1/object/sign/{bucket}/{path:path}")
async def sign(bucket: str, path: str) -> dict[str, str]:
    return {"signedURL": f"/object/sign/{bucket}/{path}?token=fake"}


@stub.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=204)


def audio_start(path: Path) -> tuple[float, float, float]:
    """(inicio del audio, duracion del audio, duracion del video) de un mp4, en segundos."""
    def probe(stream: str, fields: str) -> list[str]:
        out = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", stream,
             "-show_entries", f"stream={fields}", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out.split(",")

    a = probe("a:0", "start_time,duration")
    v = probe("v:0", "duration")
    return float(a[0]), float(a[1]), float(v[0])


# ------------------------------------------------------------------- la prueba


async def pick_job() -> tuple[str, dict[str, Any], str]:
    """Un job terminado cuyo *mejor* momento ya tenga el mp4 en disco.

    Tiene que ser el mejor del job, porque es el que el worker renderiza al atender una
    peticion de un clip: si se elige otro, la prueba espera un fichero que nadie sube.
    """
    rows = await db.fetch_all(
        """SELECT m.job_id, m.id AS moment_id, m.video_id, m.clip_path
             FROM moments m JOIN jobs j ON j.id = m.job_id
            WHERE j.status = 'done' AND m.clip_path IS NOT NULL AND m.clip_path <> ''
              AND m.final_score = (
                    SELECT MAX(x.final_score) FROM moments x WHERE x.job_id = m.job_id
              )
            -- El mas corto: la prueba renderiza dos veces y el clip largo la eterniza.
            ORDER BY (m.t_end - m.t_start) ASC LIMIT 1"""
    )
    if not rows:
        print("No hay ningun momento con clip renderizado: primero genera uno")
        raise SystemExit(2)
    row = db.row_to_dict(rows[0])
    video = db.row_to_dict(
        await db.fetch_one("SELECT * FROM videos WHERE id=?", (row["video_id"],))
    )
    return str(row["job_id"]), video, str(row["moment_id"])


async def main() -> None:
    await db.connect()
    job_id, video, moment_id = await pick_job()

    settings.supabase_url = f"http://127.0.0.1:{PORT}"
    settings.supabase_service_key = "fake-service-key"
    settings.supabase_bucket = "clips"
    settings.supabase_poll_s = 0.2

    fake.clip_requests.append({
        "id": REQUEST_ID, "url": str(video["url"]), "clips": 1, "status": "queued",
        # Lo que se escribio en la interfaz: tiene que llegar hasta el pipeline.
        "prompt": PEDIDO,
        "stage": "queued", "progress": 0, "message": "En cola", "clips_done": 0,
        "created_at": "2026-01-01T00:00:00Z", "error": None,
    })

    # El analisis y el render de verdad ya se han probado por su cuenta: aqui lo que se
    # comprueba es el puente. `submit_job` devuelve un job que ya esta terminado, y
    # `render_moment_clip` sale de la cache en disco sin tocar ffmpeg.
    recibido: dict[str, str] = {}

    async def fake_submit(url: str, hint: str = "") -> tuple[str, dict[str, Any]]:
        assert url == video["url"]
        recibido["hint"] = hint
        return job_id, video

    service.submit_job = fake_submit  # type: ignore[assignment]

    # El re-render escribe el mp4 donde vive el del clip de verdad, asi que la prueba
    # dejaba en disco un clip de la galeria con los subtitulos "PRUEBA" quemados. Se le
    # cambia la carpeta por una temporal, con una copia dentro para que la primera escena
    # siga saliendo de la cache.
    real_clip = render.clip_path(moment_id)
    tmp_clips = Path(tempfile.mkdtemp(prefix="clipper-check-"))
    shutil.copy2(real_clip, tmp_clips / real_clip.name)
    render.clips_dir = lambda: tmp_clips  # type: ignore[assignment]

    config = uvicorn.Config(stub, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)

    async def until(done, limit: int = 3000) -> None:
        for _ in range(limit):                     # 5 min de margen: renderiza de verdad
            if done():
                return
            await asyncio.sleep(0.1)

    worker = queue.QueueWorker()
    await worker.start()
    scene1: dict[str, Any] = {}
    scene2: dict[str, Any] = {}
    try:
        await until(lambda: fake.clip_requests[0]["status"] in ("done", "error"))
        # Foto de como quedo la primera escena: la segunda reemplaza el objeto y la ruta,
        # y si no se guarda esto las comprobaciones de la primera miran el resultado de
        # la segunda.
        scene1 = {
            "objects": dict(fake.objects),
            "storage_path": fake.clips[0]["storage_path"] if fake.clips else "",
            "poster_path": fake.clips[0].get("poster_path") if fake.clips else "",
            "video_date": fake.clips[0].get("video_date") if fake.clips else "",
        }
        # --- segunda escena: desde la interfaz se corrige una frase, se cambia la
        #     musica y se coloca un efecto a mano
        if fake.clips:
            clip = fake.clips[0]
            before = list(clip.get("captions") or [])
            edited = [
                {**c, "text": "PRUEBA" if i == 0 else c["text"]}
                for i, c in enumerate(before)
            ] or [{"text": "PRUEBA", "start": 0.5, "end": 2.0}]
            first_path = clip["storage_path"]
            clip["captions_edited"] = edited
            clip["sfx_edited"] = [{"name": "vineboom.mp3", "t": 3.5, "gain_db": -9}]
            clip["music_edited"] = "sneaky_snitch"
            clip["title_edited"] = "TITULO A MANO 🧪"
            clip["render_status"] = "rerender_queued"
            await until(lambda: clip.get("render_status") in ("ready", "error")
                        and clip.get("storage_path") != first_path)
            # Se mide aqui: la copia del clip desaparece al salir de este bloque.
            if (tmp_clips / real_clip.name).exists():
                scene2["audio"] = audio_start(tmp_clips / real_clip.name)
                scene2["lufs"], _ = await render.measure_loudness(tmp_clips / real_clip.name)
    finally:
        await worker.stop()
        server.should_exit = True
        await serving
        # La base apuntaba al clip de verdad y el render la ha movido a la copia: se
        # devuelve antes de borrarla, que si no queda apuntando a un fichero que no esta.
        await db.execute(
            "UPDATE moments SET clip_path=? WHERE id=?", (str(real_clip), moment_id)
        )
        await db.close()
        shutil.rmtree(tmp_clips, ignore_errors=True)

    request = fake.clip_requests[0]
    stages = [p["stage"] for p in fake.patches if "stage" in p]
    ok = True

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and condition
        print(f"  {'OK  ' if condition else 'FALLA'} {label}{f': {detail}' if detail else ''}")

    print(f"\nPeticion {REQUEST_ID[:8]} sobre {video['url']}")
    check("termina en done", request["status"] == "done", request.get("error") or "")
    check("progreso al 100%", abs(float(request["progress"]) - 1.0) < 1e-6,
          str(request["progress"]))
    check("guarda el job local", request.get("job_id") == job_id)
    check("lo que se pidio llega al pipeline", recibido.get("hint") == PEDIDO,
          repr(recibido.get("hint")))
    check("y se ve en el mensaje de la fila", PEDIDO in str(request.get("message", ""))
          or request["status"] == "done", str(request.get("message")))
    check("guarda el titulo del VOD", bool(request.get("video_title")))
    check("cuenta el clip", int(request.get("clips_done") or 0) == 1)
    check("pasa por render", "render" in stages, " -> ".join(stages))
    check("sube el mp4", any(
        p.endswith(f"{moment_id}.mp4") and size > 1_000_000
        for p, size in scene1["objects"].items()
    ), ", ".join(f"{p} ({s / 1048576:.1f} MB)" for p, s in scene1["objects"].items()))
    check("escribe la fila del clip", len(fake.clips) == 1)
    if fake.clips:
        clip = fake.clips[0]
        check("la fila trae titulo, duracion y origen",
              bool(clip.get("title")) and float(clip.get("duration_s") or 0) > 1
              and bool(clip.get("video_url")),
              f"{clip.get('title')!r} {clip.get('duration_s')}s {clip.get('music')}")
        check("apunta al objeto subido",
              scene1["storage_path"] == f"{REQUEST_ID}/{moment_id}.mp4",
              str(scene1["storage_path"]))
        # La tarjeta del directo necesita las dos cosas: una portada y una fecha.
        check("sube la portada del clip",
              f"clips/{REQUEST_ID}/{moment_id}.jpg" in scene1["objects"]
              and scene1["poster_path"] == f"{REQUEST_ID}/{moment_id}.jpg",
              str(scene1["poster_path"]))
        check("guarda la fecha del directo", bool(scene1["video_date"]),
              str(scene1["video_date"]))
    print("\nSubtitulos corregidos -> re-render")
    if not fake.clips:
        check("hay un clip que editar", False)
    else:
        clip = fake.clips[0]
        cues = clip.get("captions") or []
        check("vuelve a ready", clip.get("render_status") == "ready",
              str(clip.get("render_error") or ""))
        check("sube de version", int(clip.get("version") or 0) == 2, str(clip.get("version")))
        check("la ruta nueva lleva la version",
              str(clip.get("storage_path", "")).endswith("-v2.mp4"),
              str(clip.get("storage_path")))
        check("el objeto nuevo esta subido",
              f"clips/{clip.get('storage_path')}" in fake.objects,
              ", ".join(fake.objects))
        check("el objeto viejo se borra",
              f"clips/{scene1['storage_path']}" not in fake.objects,
              ", ".join(fake.objects))
        # La portada no cambia al rehacer el clip: es la misma foto y la misma ruta.
        check("la portada sobrevive al re-render",
              f"clips/{clip.get('poster_path')}" in fake.objects,
              str(clip.get("poster_path")))
        # El audio tiene que empezar en 0 y durar lo que el video: el clip salia con el
        # primer segundo mudo cuando la busqueda del `-ss` caia en un limite de segmento.
        start, adur, vdur = scene2.get("audio") or (9.0, 0.0, 0.0)
        check("el audio empieza con el clip", start < 0.05, f"{start:.3f}s")
        check("el audio dura lo que el video", abs(adur - vdur) < 0.15,
              f"audio {adur:.2f}s / video {vdur:.2f}s")
        # Y a un volumen parecido al del resto del feed: los primeros clips salieron
        # entre -18 y -25 LUFS y habia que subir los altavoces para oirlos. El margen de
        # abajo es para los clips que topan con su propio pico antes de llegar al objetivo.
        lufs = scene2.get("lufs")
        objetivo = settings.render_target_lufs
        check("sale al volumen de las redes",
              lufs is not None and objetivo - 4.0 <= lufs <= objetivo + 1.0,
              f"{lufs:.1f} LUFS (objetivo {objetivo:.0f})" if lufs else "sin medida")
        check("guarda el texto editado",
              bool(cues) and cues[0]["text"] == "PRUEBA",
              cues[0]["text"] if cues else "sin frases")
        efectos = clip.get("sfx_cues") or []
        check("quema el efecto colocado a mano",
              len(efectos) == 1 and efectos[0]["name"] == "vineboom.mp3"
              and abs(float(efectos[0]["t"]) - 3.5) < 0.01,
              str(efectos))
        check("cambia la musica", clip.get("music") == "sneaky_snitch", str(clip.get("music")))
        check("marca los efectos como manuales", clip.get("sfx") == "manual",
              str(clip.get("sfx")))
        check("guarda el titulo escrito a mano", clip.get("title") == "TITULO A MANO 🧪",
              str(clip.get("title")))
        check("limpia los borradores",
              clip.get("captions_edited") is None and clip.get("sfx_edited") is None
              and clip.get("music_edited") is None and clip.get("title_edited") is None)

    print("\n" + ("Todo correcto" if ok else "Hay fallos"))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
