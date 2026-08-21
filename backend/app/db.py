"""Acceso a SQLite mediante aiosqlite. Esquema + migraciones sencillas por user_version."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from .config import settings

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id                    TEXT PRIMARY KEY,
    platform              TEXT NOT NULL,
    ext_id                TEXT NOT NULL,
    url                   TEXT NOT NULL,
    title                 TEXT,
    duration              REAL,
    uploader              TEXT,
    upload_date           TEXT,
    thumbnail             TEXT,
    audio_path            TEXT,
    video_path            TEXT,
    stream_url            TEXT,
    stream_url_expires_at REAL,
    created_at            REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_platform_ext ON videos(platform, ext_id);

CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    video_id       TEXT,
    url            TEXT NOT NULL,
    status         TEXT NOT NULL,          -- queued | running | done | error | cancelled
    stage          TEXT,
    progress       REAL NOT NULL DEFAULT 0,
    message        TEXT,
    error          TEXT,
    chat_available INTEGER NOT NULL DEFAULT 0,
    chat_messages  INTEGER NOT NULL DEFAULT 0,
    enriched       INTEGER NOT NULL DEFAULT 0,
    transcribed    INTEGER NOT NULL DEFAULT 0,
    warnings       TEXT NOT NULL DEFAULT '[]',
    providers      TEXT NOT NULL DEFAULT '{}',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    finished_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS job_events (
    job_id   TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    payload  TEXT NOT NULL,
    ts       REAL NOT NULL,
    PRIMARY KEY (job_id, seq)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    t        REAL NOT NULL,
    user     TEXT,
    text     TEXT,
    emotes   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_chat_video_t ON chat_messages(video_id, t);

CREATE TABLE IF NOT EXISTS moments (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    video_id      TEXT NOT NULL,
    t_start       REAL NOT NULL,
    t_end         REAL NOT NULL,
    t_peak        REAL NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT 'otro',
    final_score   REAL NOT NULL DEFAULT 0,
    signal_score  REAL NOT NULL DEFAULT 0,
    clip_score    REAL NOT NULL DEFAULT 0,
    chat_z        REAL NOT NULL DEFAULT 0,
    audio_z       REAL NOT NULL DEFAULT 0,
    unique_users  INTEGER NOT NULL DEFAULT 0,
    msg_count     INTEGER NOT NULL DEFAULT 0,
    combo         INTEGER NOT NULL DEFAULT 0,
    transcript    TEXT NOT NULL DEFAULT '',
    words         TEXT NOT NULL DEFAULT '[]',
    language      TEXT,
    enriched      INTEGER NOT NULL DEFAULT 0,
    thumb_path    TEXT,
    source        TEXT NOT NULL DEFAULT 'signals',   -- signals | vision
    vision_note   TEXT NOT NULL DEFAULT '',
    hook          TEXT NOT NULL DEFAULT '',          -- que engancha en los primeros 2 s
    clip_path     TEXT,                              -- mp4 vertical renderizado
    rank          INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moments_job ON moments(job_id, rank);

CREATE TABLE IF NOT EXISTS quota_usage (
    provider TEXT NOT NULL,
    date     TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    units    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, date)
);
"""


_db: aiosqlite.Connection | None = None


async def connect() -> aiosqlite.Connection:
    """Abre (una sola vez) la conexion global y aplica el esquema."""
    global _db
    if _db is not None:
        return _db
    settings.ensure_dirs()
    conn = await aiosqlite.connect(settings.db_path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.executescript(SCHEMA)
    await _migrate(conn)
    _db = conn
    return conn


async def _migrate(conn: aiosqlite.Connection) -> None:
    cur = await conn.execute("PRAGMA user_version")
    row = await cur.fetchone()
    current = int(row[0]) if row else 0
    if current < 2:
        # v2: origen del candidato (senales o vision) y la pista visual.
        for ddl in (
            "ALTER TABLE moments ADD COLUMN source TEXT NOT NULL DEFAULT 'signals'",
            "ALTER TABLE moments ADD COLUMN vision_note TEXT NOT NULL DEFAULT ''",
        ):
            try:
                await conn.execute(ddl)
            except Exception:  # noqa: BLE001 - la columna ya existe en bases nuevas
                pass
    if current < 3:
        # v3: el gancho de los primeros segundos, clave para que un clip funcione.
        try:
            await conn.execute("ALTER TABLE moments ADD COLUMN hook TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001 - ya existe en bases nuevas
            pass
    if current < 4:
        # v4: ruta del mp4 vertical renderizado.
        try:
            await conn.execute("ALTER TABLE moments ADD COLUMN clip_path TEXT")
        except Exception:  # noqa: BLE001 - ya existe en bases nuevas
            pass
    if current != SCHEMA_VERSION:
        await conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


@asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect()
    await conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        await conn.execute("ROLLBACK")
        raise
    else:
        await conn.execute("COMMIT")


async def execute(sql: str, params: Iterable[Any] = ()) -> None:
    conn = await connect()
    await conn.execute(sql, tuple(params))


async def executemany(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    conn = await connect()
    await conn.executemany(sql, [tuple(r) for r in rows])


async def fetch_one(sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
    conn = await connect()
    cur = await conn.execute(sql, tuple(params))
    return await cur.fetchone()


async def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    conn = await connect()
    cur = await conn.execute(sql, tuple(params))
    return list(await cur.fetchall())


async def fetch_value(sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    row = await fetch_one(sql, params)
    return row[0] if row is not None else default


def row_to_dict(row: aiosqlite.Row | Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default
