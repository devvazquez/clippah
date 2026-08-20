"""Token bucket multi-dimension con cuota diaria persistida en SQLite.

Obligatorio, no opcional: es lo que permite que al agotar cuota el pipeline *degrade*
al proveedor local en lugar de fallar.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

from .. import db
from ..utils import log

# Los limites diarios no resetean a la misma hora en todos los proveedores.
UTC_TZ = UTC
PACIFIC_TZ = timezone(timedelta(hours=-8), "PT")  # Gemini cuenta en Pacific Time

MAX_BACKOFF_ATTEMPTS = 5


class QuotaExhausted(RuntimeError):
    """La cuota diaria (o por hora) del proveedor esta agotada."""

    def __init__(self, provider: str, dimension: str, detail: str = "") -> None:
        self.provider = provider
        self.dimension = dimension
        msg = f"Cuota de {provider} agotada ({dimension})"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


@dataclass(slots=True)
class _Bucket:
    """Token bucket clasico: `capacity` tokens que se rellenan en `period` segundos."""

    capacity: float
    period: float
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.capacity / self.period))
        self.updated = now

    def take(self, amount: float) -> float:
        """Consume `amount`; devuelve los segundos que habria que esperar (0 si hay cupo)."""
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return 0.0
        missing = amount - self.tokens
        return missing * (self.period / self.capacity)


class RateLimiter:
    """Limita peticiones por minuto/dia y "unidades" (segundos de audio, tokens) por hora/dia."""

    def __init__(
        self,
        name: str,
        rpm: int,
        rpd: int,
        units_per_hour: int | None = None,
        units_per_day: int | None = None,
        *,
        tz: timezone = UTC_TZ,
        unit_window_s: float = 3600.0,
    ) -> None:
        """`units_per_hour` es la capacidad del bucket de unidades y `unit_window_s` su
        periodo de recarga: 3600 s para los "audio seconds per hour" de Groq, 60 s para
        los "tokens per minute" de Gemini."""
        self.name = name
        self.rpm = max(1, rpm)
        self.rpd = max(1, rpd)
        self.units_per_hour = units_per_hour
        self.units_per_day = units_per_day
        self.tz = tz
        self._req_bucket = _Bucket(capacity=float(self.rpm), period=60.0)
        self.unit_window_s = unit_window_s
        self._unit_bucket = (
            _Bucket(capacity=float(units_per_hour), period=unit_window_s)
            if units_per_hour
            else None
        )
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ cuota diaria

    def today(self) -> str:
        return datetime.now(self.tz).strftime("%Y-%m-%d")

    async def usage(self) -> tuple[int, float]:
        row = await db.fetch_one(
            "SELECT requests, units FROM quota_usage WHERE provider=? AND date=?",
            (self.name, self.today()),
        )
        if row is None:
            return 0, 0.0
        return int(row["requests"]), float(row["units"])

    async def _add_usage(self, requests: int, units: float) -> None:
        await db.execute(
            """
            INSERT INTO quota_usage (provider, date, requests, units)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(provider, date) DO UPDATE SET
                requests = requests + excluded.requests,
                units    = units + excluded.units
            """,
            (self.name, self.today(), requests, units),
        )

    async def remaining(self) -> dict[str, float | None]:
        req, units = await self.usage()
        return {
            "requests_today": req,
            "requests_limit": float(self.rpd),
            "requests_left": max(0.0, self.rpd - req),
            "units_today": units,
            "units_limit": float(self.units_per_day) if self.units_per_day else None,
            "units_left": (
                max(0.0, self.units_per_day - units) if self.units_per_day else None
            ),
        }

    async def has_room(self, units: float = 1.0) -> bool:
        req, used = await self.usage()
        if req + 1 > self.rpd:
            return False
        return not (self.units_per_day and used + units > self.units_per_day)

    # ---------------------------------------------------------------------- acquire

    async def acquire(self, units: float = 1.0) -> None:
        """Bloquea hasta que haya cupo. Lanza QuotaExhausted si la cuota diaria se agoto."""
        async with self._lock:
            req_today, units_today = await self.usage()
            if req_today + 1 > self.rpd:
                raise QuotaExhausted(self.name, "RPD", f"{req_today}/{self.rpd} peticiones hoy")
            if self.units_per_day and units_today + units > self.units_per_day:
                raise QuotaExhausted(
                    self.name,
                    "unidades/dia",
                    f"{units_today:.0f}+{units:.0f} > {self.units_per_day}",
                )
            if self.units_per_hour and units > self.units_per_hour:
                raise QuotaExhausted(
                    self.name,
                    "unidades/ventana",
                    f"una sola peticion de {units:.0f} excede el limite de "
                    f"{self.units_per_hour} por {self.unit_window_s:.0f}s",
                )

            wait = self._req_bucket.take(1.0)
            if self._unit_bucket is not None:
                wait = max(wait, self._unit_bucket.take(units))
            if wait > 0:
                log.info("%s: esperando %.1fs por rate limit", self.name, wait)
                await asyncio.sleep(wait)
            await self._add_usage(1, units)

    async def refund(self, units: float = 1.0) -> None:
        """Devuelve la cuota de una peticion que no llego a consumirse."""
        await self._add_usage(-1, -units)

    # ---------------------------------------------------------------------- backoff

    async def backoff(self, attempt: int, retry_after: float | None = None) -> None:
        """`min(60, 2**attempt) + jitter`, respetando `retry-after` si viene."""
        delay = retry_after if retry_after is not None else min(60.0, 2.0**attempt)
        delay += random.random()  # noqa: S311 - jitter, no cripto
        log.info("%s: backoff %.1fs (intento %d)", self.name, delay, attempt + 1)
        await asyncio.sleep(delay)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
