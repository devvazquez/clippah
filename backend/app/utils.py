"""Utilidades compartidas: subprocesos, localizacion de binarios, formateo de tiempo."""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

log = logging.getLogger("clipper")


class ToolMissing(RuntimeError):
    """Un binario externo obligatorio no esta disponible."""


class CommandFailed(RuntimeError):
    def __init__(self, cmd: Sequence[str], code: int, stderr: str) -> None:
        self.cmd = list(cmd)
        self.code = code
        self.stderr = stderr
        tail = stderr.strip().splitlines()[-4:]
        super().__init__(f"{cmd[0]} salio con codigo {code}: {' | '.join(tail)}")


@dataclass(slots=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise ToolMissing("ffmpeg no esta instalado o no esta en el PATH")
    return path


def ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise ToolMissing("ffprobe no esta instalado o no esta en el PATH")
    return path


def ytdlp_cmd() -> list[str]:
    """yt-dlp como binario si existe; si no, como modulo del interprete actual."""
    path = shutil.which("yt-dlp")
    if path:
        return [path]
    return [sys.executable, "-m", "yt_dlp"]


def ytdlp_base(*, playlist: bool = False) -> list[str]:
    """Comando de yt-dlp con las opciones globales (cookies, extras) ya aplicadas."""
    from .config import settings

    cmd = [*ytdlp_cmd(), "--no-warnings"]
    if not playlist:
        cmd.append("--no-playlist")
    if settings.ytdlp_cookies_from_browser:
        cmd += ["--cookies-from-browser", settings.ytdlp_cookies_from_browser]
    if settings.ytdlp_cookies_file:
        cmd += ["--cookies", settings.ytdlp_cookies_file]
    if settings.ytdlp_extra_args:
        cmd += shlex.split(settings.ytdlp_extra_args)
    return cmd


# Mensajes de yt-dlp que conviene traducir a algo accionable para el usuario.
_YTDLP_HINTS: tuple[tuple[str, str], ...] = (
    (
        "sign in to confirm",
        "YouTube esta pidiendo verificacion anti-bot para esta IP. Pon "
        "YTDLP_COOKIES_FROM_BROWSER=firefox (o chrome/brave) en backend/.env, o "
        "YTDLP_COOKIES_FILE=/ruta/cookies.txt.",
    ),
    (
        "does not exist",
        "Ese VOD no existe o ya no esta disponible. Los VODs de cuentas normales de "
        "Twitch caducan a los 60 dias.",
    ),
    ("private video", "El video es privado."),
    ("members-only", "El video es solo para miembros del canal."),
    (
        "unable to download webpage",
        "No se pudo alcanzar la plataforma. Revisa la conexion o el proxy.",
    ),
)


def friendly_ytdlp_error(stderr: str) -> str | None:
    """Devuelve un mensaje accionable si el error de yt-dlp es uno de los conocidos."""
    low = (stderr or "").lower()
    for needle, hint in _YTDLP_HINTS:
        if needle in low:
            return hint
    return None


def ytdlp_error_line(stderr: str) -> str:
    """Ultima linea `ERROR:` de yt-dlp, sin la ruta del interprete ni el resto del ruido."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.upper().startswith("ERROR")]
    raw = (errors[-1] if errors else (lines[-1] if lines else "")).removeprefix("ERROR:").strip()
    return raw[:300] or "yt-dlp no dio detalles"


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def have_ytdlp() -> bool:
    if shutil.which("yt-dlp"):
        return True
    try:
        import yt_dlp  # noqa: F401

        return True
    except ImportError:
        return False


def have_faster_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except ImportError:
        return False


async def run(
    cmd: Sequence[str],
    *,
    timeout: float | None = 600,
    check: bool = True,
    stdin_data: bytes | None = None,
) -> CommandResult:
    """Ejecuta un comando y devuelve stdout/stderr decodificados."""
    log.debug("run: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_data), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise CommandFailed(cmd, -1, f"timeout tras {timeout}s") from None
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    result = CommandResult(
        code=proc.returncode or 0,
        stdout=(out or b"").decode("utf-8", "replace"),
        stderr=(err or b"").decode("utf-8", "replace"),
    )
    if check and result.code != 0:
        raise CommandFailed(cmd, result.code, result.stderr)
    return result


async def run_streaming(cmd: Sequence[str], *, timeout: float | None = None) -> AsyncIterator[str]:
    """Ejecuta un comando y va emitiendo sus lineas de stdout (para progreso de yt-dlp)."""
    log.debug("run_streaming: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    tail: list[str] = []
    try:
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            tail.append(line)
            del tail[:-40]
            yield line
        code = await proc.wait()
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    if code != 0:
        raise CommandFailed(cmd, code, "\n".join(tail))


def hhmmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def twitch_time(seconds: float) -> str:
    """Formato `1h24m07s` que usa el reproductor de Twitch."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
