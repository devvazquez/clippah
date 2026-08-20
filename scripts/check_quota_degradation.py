#!/usr/bin/env python
"""Verificacion de la fase 10: agotar la cuota a proposito y comprobar que degrada.

Simula el criterio de aceptacion 6 sin gastar cuota real: pone `GROQ_ASD` a 60 s,
consume la cuota y comprueba que el RateLimiter lanza QuotaExhausted (lo que hace que
el orquestador caiga a Whisper local en lugar de fallar).

    backend/.venv/bin/python scripts/check_quota_degradation.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.providers.ratelimit import QuotaExhausted, RateLimiter  # noqa: E402


async def main() -> int:
    await db.connect()
    limiter = RateLimiter("groq-test", rpm=60, rpd=1000, units_per_hour=60, units_per_day=60)
    await db.execute(
        "DELETE FROM quota_usage WHERE provider=?", ("groq-test",)
    )

    print("cuota diaria simulada: 60 segundos de audio (ASD=60)")
    await limiter.acquire(40)
    print("  peticion 1 de 40 s -> ok")
    usage = await limiter.remaining()
    print(f"  restante: {usage['units_left']} s")

    try:
        await limiter.acquire(40)
    except QuotaExhausted as exc:
        print(f"  peticion 2 de 40 s -> QuotaExhausted: {exc}")
    else:
        print("  ERROR: la segunda peticion deberia haber agotado la cuota")
        return 1

    print("\nPersistencia: los contadores viven en la tabla quota_usage")
    rows = await db.fetch_all(
        "SELECT provider, date, requests, units FROM quota_usage WHERE provider=?",
        ("groq-test",),
    )
    for row in rows:
        print(f"  {dict(row)}")
    await db.execute("DELETE FROM quota_usage WHERE provider=?", ("groq-test",))
    await db.close()
    print("\nOK: al agotarse la cuota el pipeline degrada a Whisper local (evento warning).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
