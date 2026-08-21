"""Vuelve a puntuar con el LLM los momentos ya guardados de un job.

Sirve para probar cambios en el prompt sin gastar otra vez transcripcion: los
transcripts, las senales y los fotogramas ya estan en la base de datos, asi que esto
solo repite la llamada al modelo y reescribe los campos que salen de ella.

    python scripts/rescore.py job_xxxxxxxx [job_yyyyyyyy ...]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import db  # noqa: E402
from app.pipeline.score import Fragment, ScoringEngine, finalize  # noqa: E402


def _fragment(row: dict) -> Fragment:
    return Fragment(
        id=row["id"],
        t_start=row["t_start"],
        t_end=row["t_end"],
        t_peak=row["t_peak"],
        signal_score=row["signal_score"],
        chat_z=row["chat_z"],
        audio_z=row["audio_z"],
        unique_users=row["unique_users"],
        msg_count=row["msg_count"],
        combo=bool(row["combo"]),
        source=row["source"] or "signals",
        vision_note=row["vision_note"] or "",
        transcript=row["transcript"] or "",
        language=row["language"] or "",
        words=json.loads(row["words"] or "[]"),
    )


async def rescore(job_id: str) -> None:
    rows = [
        db.row_to_dict(r)
        for r in await db.fetch_all(
            "SELECT * FROM moments WHERE job_id=? ORDER BY rank", (job_id,)
        )
    ]
    if not rows:
        print(f"{job_id}: sin momentos guardados")
        return
    fragments = [_fragment(r) for r in rows]
    chat_available = any(r["msg_count"] for r in rows)

    engine = ScoringEngine()
    provider = await engine.prepare()
    scores, enriched = await engine.score(fragments, chat_available=chat_available)
    scored = finalize(fragments, scores, enriched=enriched)
    if not scored:
        # Mismo criterio que el orquestador: si el modelo descarta todo, nos quedamos
        # con su ranking en vez de dejar el job sin momentos.
        for s in scores:
            s.worth_clipping = True
        scored = finalize(fragments, scores, enriched=enriched)
        print(f"{job_id}: el modelo descarto todo, se mantiene su orden")
    print(f"{job_id}: {provider}, {len(scored)}/{len(rows)} momentos")

    for rank, row in enumerate(scored):
        await db.execute(
            """UPDATE moments SET title=?, description=?, category=?, clip_score=?,
                   final_score=?, hook=?, clip_title=?, sfx_fit=?, music=?, rank=?
               WHERE id=?""",
            (
                row["title"], row["description"], row["category"], row["clip_score"],
                row["final_score"], row.get("hook", ""), row.get("clip_title", ""),
                row.get("sfx_fit", "ninguno"), row.get("music", "ninguna"), rank,
                row["id"],
            ),
        )
        print(
            f"  #{rank} {row['t_start']:.0f}s {row['final_score']:.3f} "
            f"sfx={row.get('sfx_fit')} music={row.get('music')} "
            f"| {row.get('clip_title')!r}"
        )
    survivors = {r["id"] for r in scored}
    dropped = [r["id"] for r in rows if r["id"] not in survivors]
    if dropped:
        print(f"  descartados por el modelo: {len(dropped)}")


async def main() -> None:
    jobs = sys.argv[1:]
    if not jobs:
        print(__doc__)
        raise SystemExit(2)
    await db.connect()
    try:
        for job_id in jobs:
            await rescore(job_id)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
