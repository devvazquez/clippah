"""Descarga del chat replay. Twitch via GQL privado (paginado por offset) y YouTube via yt-dlp.

El chat es la senal con mejor relacion valor/coste, pero es *opcional*: si falla, el
pipeline sigue solo con audio y se marca `chat_available=false`.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from ..utils import CommandFailed, log, run, ytdlp_cmd

ProgressCb = Callable[[float, str], Awaitable[None]]

# Client-ID publico que lleva hardcodeado el reproductor web de twitch.tv.
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_GQL = "https://gql.twitch.tv/gql"
# Persisted query del propio reproductor.
VIDEO_COMMENTS_SHA = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

PAGE_SLEEP_S = 0.3
MAX_BACKOFF_ATTEMPTS = 5


def normalize_twitch_id(ext_id: str) -> str:
    """yt-dlp devuelve el id como `v2845170759`; el GQL solo acepta el numero."""
    return str(ext_id).strip().lstrip("vV")


class ChatUnavailable(RuntimeError):
    """El chat no se puede recuperar (VOD caducado, sin replay, endpoint caido...)."""


@dataclass(slots=True)
class ChatMessage:
    t: float          # segundos desde el inicio del VOD
    user: str
    text: str
    emotes: list[str] = field(default_factory=list)


async def fetch_chat(
    platform: str,
    ext_id: str,
    url: str,
    duration: float,
    *,
    progress: ProgressCb | None = None,
) -> list[ChatMessage]:
    if platform == "twitch":
        return await fetch_twitch_chat(ext_id, duration, progress=progress)
    if platform == "youtube":
        return await fetch_youtube_chat(url, ext_id, duration, progress=progress)
    raise ChatUnavailable(f"Plataforma sin soporte de chat: {platform}")


# --------------------------------------------------------------------------- Twitch


async def fetch_twitch_chat(
    video_id: str, duration: float, *, progress: ProgressCb | None = None
) -> list[ChatMessage]:
    """Pagina el chat replay por `contentOffsetSeconds`.

    IMPORTANTE: paginar por cursor dispara el reto de integridad KPSDK en la segunda
    peticion (necesita navegador real). Paginar por offset no lo dispara.
    """
    video_id = normalize_twitch_id(video_id)
    messages: list[ChatMessage] = []
    seen: set[str] = set()
    offset = 0
    empty_pages = 0

    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://www.twitch.tv",
        "Referer": "https://www.twitch.tv/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
    }

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        while offset < max(duration, 1):
            page = await _gql_page(client, video_id, offset)
            edges = _extract_edges(page)
            batch, last_offset = _parse_twitch_edges(edges, seen)
            messages.extend(batch)

            if not edges:
                empty_pages += 1
                if empty_pages >= 2:
                    break
                offset += 30
                continue
            empty_pages = 0

            if progress and duration > 0:
                await progress(
                    min(0.99, offset / duration),
                    f"Chat: {len(messages):,} mensajes".replace(",", "."),
                )

            nxt = int(last_offset) + 1 if last_offset is not None else offset + 30
            offset = max(nxt, offset + 1)
            await asyncio.sleep(PAGE_SLEEP_S)

    if not messages:
        raise ChatUnavailable(
            "Twitch no devolvio mensajes. Los VODs de cuentas normales caducan a los "
            "60 dias y sin replay no hay chat."
        )
    messages.sort(key=lambda m: m.t)
    return messages


def _extract_edges(page: dict) -> list[dict]:
    """Camino None-safe hasta `data.video.comments.edges` (cualquier nivel puede venir null)."""
    node: Any = page
    for key in ("data", "video", "comments", "edges"):
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return node if isinstance(node, list) else []


async def _gql_page(client: httpx.AsyncClient, video_id: str, offset: int) -> dict:
    body = [
        {
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": {"videoID": str(video_id), "contentOffsetSeconds": int(offset)},
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": VIDEO_COMMENTS_SHA}
            },
        }
    ]
    for attempt in range(MAX_BACKOFF_ATTEMPTS):
        try:
            resp = await client.post(TWITCH_GQL, json=body)
        except httpx.HTTPError as exc:
            if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                raise ChatUnavailable(f"Error de red hablando con Twitch GQL: {exc}") from exc
            await _backoff(attempt)
            continue

        if resp.status_code in (429, 403, 503):
            retry_after = _retry_after(resp)
            log.warning("Twitch GQL %s en offset %s, backoff", resp.status_code, offset)
            if attempt == MAX_BACKOFF_ATTEMPTS - 1:
                raise ChatUnavailable(
                    f"Twitch limito las peticiones ({resp.status_code}) tras "
                    f"{MAX_BACKOFF_ATTEMPTS} intentos"
                )
            await _backoff(attempt, retry_after)
            continue
        if resp.status_code >= 400:
            raise ChatUnavailable(f"Twitch GQL devolvio {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ChatUnavailable("Twitch GQL devolvio una respuesta ilegible") from exc
        payload = data[0] if isinstance(data, list) and data else data
        if isinstance(payload, dict) and payload.get("errors"):
            raise ChatUnavailable(f"Twitch GQL: {payload['errors']}")
        return payload if isinstance(payload, dict) else {}
    return {}


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def _backoff(attempt: int, retry_after: float | None = None) -> None:
    delay = retry_after if retry_after is not None else min(60.0, 2.0**attempt)
    await asyncio.sleep(delay + random.random())  # noqa: S311 - jitter, no cripto


def _parse_twitch_edges(
    edges: list[dict], seen: set[str]
) -> tuple[list[ChatMessage], float | None]:
    out: list[ChatMessage] = []
    last_offset: float | None = None
    for edge in edges:
        node = edge.get("node") or {}
        mid = str(node.get("id") or "")
        offset = node.get("contentOffsetSeconds")
        if offset is None:
            continue
        last_offset = float(offset)
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        commenter = node.get("commenter") or {}
        user = str(commenter.get("displayName") or commenter.get("login") or "anon")
        fragments = ((node.get("message") or {}).get("fragments")) or []
        parts: list[str] = []
        emotes: list[str] = []
        for frag in fragments:
            txt = frag.get("text") or ""
            if txt:
                parts.append(txt)
            emote = frag.get("emote")
            if emote:
                emotes.append(str(emote.get("emoteID") or txt.strip() or "emote"))
        out.append(
            ChatMessage(t=float(offset), user=user, text=" ".join(parts).strip(), emotes=emotes)
        )
    return out, last_offset


async def fetch_twitch_chat_via_cli(video_id: str) -> list[ChatMessage]:
    """Fallback: `twitch-dl chat` si esta instalado (PyPI, mas estable que el GQL crudo)."""
    exe = shutil.which("twitch-dl")
    if not exe:
        raise ChatUnavailable("twitch-dl no esta instalado")
    video_id = normalize_twitch_id(video_id)
    settings.ensure_dirs()
    out = settings.media_dir / f"twitch-{video_id}.chat.json"
    try:
        await run(
            [exe, "chat", str(video_id), "--json", "--output", str(out), "--overwrite"],
            timeout=1800,
        )
    except CommandFailed as exc:
        raise ChatUnavailable(f"twitch-dl fallo: {exc}") from exc
    if not out.exists():
        raise ChatUnavailable("twitch-dl no genero fichero de chat")
    raw = json.loads(out.read_text("utf-8"))
    comments = raw.get("comments", raw) if isinstance(raw, dict) else raw
    msgs: list[ChatMessage] = []
    for c in comments or []:
        t = c.get("content_offset_seconds") or c.get("contentOffsetSeconds")
        if t is None:
            continue
        body = (c.get("message") or {})
        text = body.get("body") if isinstance(body, dict) else str(body)
        user = (c.get("commenter") or {}).get("display_name") or "anon"
        msgs.append(ChatMessage(t=float(t), user=str(user), text=str(text or ""), emotes=[]))
    if not msgs:
        raise ChatUnavailable("twitch-dl devolvio un chat vacio")
    msgs.sort(key=lambda m: m.t)
    return msgs


# -------------------------------------------------------------------------- YouTube

_YT_EMOJI = re.compile(r":([a-zA-Z0-9_\-]+):")


async def fetch_youtube_chat(
    url: str, ext_id: str, duration: float, *, progress: ProgressCb | None = None
) -> list[ChatMessage]:
    """`yt-dlp --write-subs --sub-langs live_chat` genera un JSONL con el replay."""
    settings.ensure_dirs()
    base = settings.media_dir / f"youtube-{ext_id}"
    target = Path(str(base) + ".live_chat.json")
    if not target.exists():
        cmd = [
            *ytdlp_cmd(),
            "--skip-download",
            "--write-subs",
            "--sub-langs", "live_chat",
            "--no-warnings",
            "--no-playlist",
            "-o", str(base) + ".%(ext)s",
            url,
        ]
        try:
            await run(cmd, timeout=1800)
        except CommandFailed as exc:
            raise ChatUnavailable(f"yt-dlp no pudo bajar el live chat: {exc}") from exc
    if not target.exists():
        cands = sorted(settings.media_dir.glob(f"{base.name}*live_chat*"))
        if not cands:
            raise ChatUnavailable("Este video de YouTube no tiene replay de chat")
        target = cands[0]

    msgs: list[ChatMessage] = []
    with target.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            parsed = _parse_youtube_item(obj)
            if parsed:
                msgs.append(parsed)
            if progress and line_no % 2000 == 0 and duration > 0 and msgs:
                await progress(
                    min(0.99, msgs[-1].t / duration),
                    f"Chat: {len(msgs)} mensajes",
                )
    if not msgs:
        raise ChatUnavailable("El replay de chat de YouTube vino vacio")
    msgs.sort(key=lambda m: m.t)
    return msgs


def _parse_youtube_item(obj: dict) -> ChatMessage | None:
    action = obj.get("replayChatItemAction") or {}
    offset_ms = action.get("videoOffsetTimeMsec")
    if offset_ms is None:
        return None
    for act in action.get("actions") or []:
        item = ((act.get("addChatItemAction") or {}).get("item")) or {}
        renderer = item.get("liveChatTextMessageRenderer")
        if not renderer:
            continue
        runs = (renderer.get("message") or {}).get("runs") or []
        parts: list[str] = []
        emotes: list[str] = []
        for run_ in runs:
            if "text" in run_:
                parts.append(str(run_["text"]))
            elif "emoji" in run_:
                emoji = run_["emoji"]
                shortcut = (emoji.get("shortcuts") or [""])[0]
                name = _YT_EMOJI.sub(r"\1", shortcut) or str(emoji.get("emojiId") or "emoji")
                emotes.append(name)
                parts.append(shortcut or name)
        author = (renderer.get("authorName") or {}).get("simpleText") or "anon"
        return ChatMessage(
            t=float(offset_ms) / 1000.0,
            user=str(author),
            text=" ".join(parts).strip(),
            emotes=emotes,
        )
    return None
