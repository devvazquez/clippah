"""Cliente minimo de Supabase: PostgREST para las tablas y Storage para los mp4.

No se usa `supabase-py` a proposito: de esa libreria aqui solo harian falta dos verbos de
PostgREST y una subida a Storage, y arrastra su propio cliente HTTP, su capa de realtime y
sus versiones pineadas. Con `httpx`, que ya es dependencia, son cien lineas y se ve
exactamente que peticion sale.

Entra con la clave `service_role`, que se salta RLS: vive solo en el `.env` del backend y
nunca sale hacia el navegador.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import settings
from ..utils import log

TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=60.0)
# Subir un mp4 de 20 MB por una conexion domestica puede tardar.
UPLOAD_TIMEOUT = httpx.Timeout(300.0, connect=10.0)


class SupabaseError(RuntimeError):
    """Respuesta de error de Supabase, con el cuerpo tal cual para poder leerlo."""


def configured() -> bool:
    return bool(settings.supabase_url and settings.supabase_service_key)


class Supabase:
    """Acceso a una tabla/bucket del proyecto. Reutiliza una sola conexion."""

    def __init__(self) -> None:
        base = settings.supabase_url.rstrip("/")
        key = settings.supabase_service_key
        self._rest = f"{base}/rest/v1"
        self._storage = f"{base}/storage/v1"
        self._client = httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ tablas

    async def select(
        self, table: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        r = await self._client.get(f"{self._rest}/{table}", params=params or {})
        return self._rows(r)

    async def insert(
        self, table: str, row: dict[str, Any], *, upsert_on: str = ""
    ) -> dict[str, Any]:
        prefer = ["return=representation"]
        params = {}
        if upsert_on:
            prefer.append("resolution=merge-duplicates")
            params["on_conflict"] = upsert_on
        r = await self._client.post(
            f"{self._rest}/{table}",
            params=params,
            content=json.dumps(row),
            headers={"Prefer": ",".join(prefer)},
        )
        rows = self._rows(r)
        return rows[0] if rows else {}

    async def update(
        self, table: str, patch: dict[str, Any], *, match: dict[str, str]
    ) -> list[dict[str, Any]]:
        """PATCH con filtros de PostgREST (`{"id": "eq.123"}`)."""
        r = await self._client.patch(
            f"{self._rest}/{table}",
            params=match,
            content=json.dumps(patch),
            headers={"Prefer": "return=representation"},
        )
        return self._rows(r)

    # ----------------------------------------------------------------- storage

    async def upload(self, path: str, data: bytes, *, content_type: str) -> None:
        """Sube (o reemplaza) un objeto del bucket configurado."""
        url = f"{self._storage}/object/{settings.supabase_bucket}/{path}"
        r = await self._client.post(
            url,
            content=data,
            headers={
                "Content-Type": content_type,
                # Reintentar una peticion no debe fallar por que el objeto ya exista.
                "x-upsert": "true",
            },
            timeout=UPLOAD_TIMEOUT,
        )
        if r.status_code >= 400:
            raise SupabaseError(f"subida de {path} fallo ({r.status_code}): {r.text[:300]}")

    async def delete(self, path: str) -> None:
        """Borra un objeto. Que ya no exista no es un error: el fin es que no este."""
        url = f"{self._storage}/object/{settings.supabase_bucket}/{path}"
        r = await self._client.delete(url)
        if r.status_code >= 400 and r.status_code != 404:
            log.warning("no se pudo borrar %s (%s): %s", path, r.status_code, r.text[:200])

    async def signed_url(self, path: str, *, expires_in: int = 3600) -> str:
        url = f"{self._storage}/object/sign/{settings.supabase_bucket}/{path}"
        r = await self._client.post(url, content=json.dumps({"expiresIn": expires_in}))
        if r.status_code >= 400:
            raise SupabaseError(f"firma de {path} fallo ({r.status_code}): {r.text[:300]}")
        signed = str(r.json().get("signedURL") or "")
        return f"{settings.supabase_url.rstrip('/')}/storage/v1{signed}"

    # ------------------------------------------------------------------ interno

    @staticmethod
    def _rows(r: httpx.Response) -> list[dict[str, Any]]:
        if r.status_code >= 400:
            raise SupabaseError(f"{r.request.method} {r.request.url} -> {r.status_code}: "
                                f"{r.text[:300]}")
        if not r.content:
            return []
        body = r.json()
        return body if isinstance(body, list) else [body]


async def health() -> tuple[bool, str]:
    """Comprueba que las claves valen y que las tablas estan donde se espera."""
    if not configured():
        return False, "sin configurar (falta SUPABASE_URL o SUPABASE_SERVICE_KEY)"
    client = Supabase()
    try:
        await client.select("clip_requests", params={"select": "id", "limit": "1"})
        return True, "conectado"
    except (SupabaseError, httpx.HTTPError) as exc:
        log.warning("supabase no responde: %s", exc)
        return False, str(exc)[:200]
    finally:
        await client.close()
