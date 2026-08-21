"""Barra de redes sociales quemada en el clip.

Los logos se dibujan aqui en vez de descargar assets: son formas simples y asi el
render no depende de ficheros externos ni de la red. La silueta de Twitch es su trazado
real sobre la rejilla 14x16 del logo oficial; YouTube y TikTok son aproximaciones
reconocibles a 44 px.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import settings
from ..utils import log

BAR_W = 1080
BAR_H = 84
ICON = 46
GAP_ICON_TEXT = 12
FONT_SIZE = 27

TWITCH_PURPLE = (145, 70, 255, 255)
YOUTUBE_RED = (255, 0, 0, 255)
TIKTOK_CYAN = (37, 244, 238, 255)
TIKTOK_PINK = (254, 44, 85, 255)
WHITE = (255, 255, 255, 255)


@dataclass(slots=True)
class SocialLink:
    platform: str   # twitch | youtube | tiktok
    label: str
    url: str


def default_links() -> list[SocialLink]:
    raw = settings.social_links.strip()
    out: list[SocialLink] = []
    for chunk in raw.split(";"):
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) >= 2 and parts[0]:
            out.append(
                SocialLink(platform=parts[0].lower(), label=parts[1],
                           url=parts[2] if len(parts) > 2 else "")
            )
    return out


def _twitch(d: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
    # Trazado oficial sobre rejilla 14x16.
    def p(px: float, py: float) -> tuple[float, float]:
        return (x + px / 14 * s, y + py / 16 * s)

    body = [p(2, 0), p(0, 2), p(0, 14), p(4, 14), p(4, 16), p(6, 14), p(9, 14),
            p(14, 9), p(14, 0)]
    d.polygon(body, fill=TWITCH_PURPLE)
    d.rectangle([p(5, 4), p(6.4, 9)], fill=(10, 10, 12, 255))
    d.rectangle([p(8, 4), p(9.4, 9)], fill=(10, 10, 12, 255))


def _youtube(d: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
    h = int(s * 0.70)
    top = y + (s - h) // 2
    d.rounded_rectangle([x, top, x + s, top + h], radius=int(h * 0.28), fill=YOUTUBE_RED)
    cx, cy = x + s * 0.42, top + h / 2
    tri = [(cx - s * 0.06, cy - h * 0.22), (cx - s * 0.06, cy + h * 0.22),
           (cx + s * 0.20, cy)]
    d.polygon(tri, fill=WHITE)


def _tiktok_note(d: ImageDraw.ImageDraw, x: int, y: int, s: int, colour) -> None:
    """Nota de TikTok: cabeza redonda, mastil y banderilla enganchada al mastil.

    La banderilla se dibuja como un arco *trazado* que arranca justo en el mastil. Antes
    era un sector de circulo suelto y salia flotando al lado, como si el logo estuviera
    cortado.
    """
    pad = s * 0.04
    y += pad
    s -= 2 * pad
    r = s * 0.255
    stem_w = max(2.0, s * 0.16)
    stem_x = s * 0.60
    d.ellipse([x, y + s - 2 * r, x + 2 * r, y + s], fill=colour)
    d.rectangle([x + stem_x - stem_w, y + s * 0.06, x + stem_x, y + s - r], fill=colour)
    box = [
        x + stem_x - stem_w,
        y + s * 0.06,
        x + stem_x - stem_w + s * 0.62,
        y + s * 0.06 + s * 0.62,
    ]
    d.arc(box, start=180, end=310, fill=colour, width=int(round(stem_w)))


def _tiktok(d: ImageDraw.ImageDraw, x: int, y: int, s: int) -> None:
    off = max(2, int(s * 0.05))
    _tiktok_note(d, x - off, y - off, s, TIKTOK_CYAN)
    _tiktok_note(d, x + off, y + off, s, TIKTOK_PINK)
    _tiktok_note(d, x, y, s, WHITE)


_DRAWERS = {"twitch": _twitch, "youtube": _youtube, "tiktok": _tiktok}


def build_social_bar(links: list[SocialLink], out: Path) -> Path:
    """Genera el PNG con transparencia de la barra de redes."""
    img = Image.new("RGBA", (BAR_W, BAR_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, BAR_W - 1, BAR_H - 1], radius=BAR_H // 2,
                        fill=(10, 10, 12, 205))
    try:
        font = ImageFont.truetype(settings.render_font_file, FONT_SIZE)
    except OSError:
        font = ImageFont.load_default()
        log.warning("fuente no encontrada para la barra de redes: se usa la de defecto")

    widths = []
    for link in links:
        tw = d.textlength(link.label, font=font)
        widths.append(ICON + GAP_ICON_TEXT + tw)
    total = sum(widths)
    gap = (BAR_W - total) / (len(links) + 1) if links else 0

    x = gap
    for link, w in zip(links, widths, strict=True):
        drawer = _DRAWERS.get(link.platform)
        icon_y = (BAR_H - ICON) // 2
        if drawer:
            drawer(d, int(x), icon_y, ICON)
        d.text((x + ICON + GAP_ICON_TEXT, BAR_H / 2), link.label, font=font,
               fill=WHITE, anchor="lm")
        x += w + gap

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# --------------------------------------------------------------------- titulo quemado

# libass rasteriza los emojis en monocromo: para tenerlos en color hay que pintar el
# titulo aparte. Pillow si soporta fuentes de color (`embedded_color`), pero no hace
# fallback entre fuentes, asi que el texto se parte en tramos y cada uno se dibuja con
# la suya.
_EMOJI_RE = re.compile(
    "([\U0001F000-\U0001FAFF\u2600-\u27BF\U0001F1E6-\U0001F1FF\u2B00-\u2BFF"
    "\uFE0F\u200D\u2190-\u21FF\u2900-\u297F]+)"
)
# Modificadores que no forman un emoji por si mismos: selector de variacion, ZWJ,
# tonos de piel y las teclas combinantes.
_EMOJI_MODIFIERS = {0xFE0F, 0xFE0E, 0x200D, 0x20E3}


def _emoji_units(chunk: str) -> list[str]:
    """Parte una racha de emojis en unidades: una secuencia ZWJ cuenta como una sola."""
    units: list[str] = []
    current = ""
    for ch in chunk:
        cp = ord(ch)
        joins = cp in _EMOJI_MODIFIERS or 0x1F3FB <= cp <= 0x1F3FF
        if current and not joins and not current.endswith("\u200d"):
            units.append(current)
            current = ch
        else:
            current += ch
    if current:
        units.append(current)
    return units


def apple_emoji_png(unit: str) -> Path | None:
    """PNG de Apple para ese emoji, si el artwork esta instalado.

    Se instala con `make setup-emoji`: son 27 MB y es artwork de Apple, asi que no va
    versionado. Sin el, se cae a la fuente de emoji del sistema.
    """
    base = settings.emoji_dir
    if not base.is_dir():
        return None
    cps = [f"{ord(c):04x}" for c in unit]
    candidates = ["-".join(cps)]
    stripped = [c for c in cps if c not in ("fe0f", "fe0e")]
    if stripped and stripped != cps:
        candidates.append("-".join(stripped))
    if len(stripped) == 1:
        candidates.append(f"{stripped[0]}-fe0f")
    for name in candidates:
        path = base / f"{name}.png"
        if path.exists():
            return path
    log.info("emoji sin artwork de Apple: %s", "-".join(cps))
    return None


EMOJI_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji-Regular.ttf",
)


def _emoji_font(size: int):
    for path in EMOJI_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                # Las fuentes CBDT solo traen una talla fija; Pillow la reescala.
                return ImageFont.truetype(path, size)
            except OSError:
                try:
                    return ImageFont.truetype(path, 109)
                except OSError:
                    continue
    return None


def _segments(text: str) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    for chunk in _EMOJI_RE.split(text):
        if chunk:
            out.append((bool(_EMOJI_RE.fullmatch(chunk)), chunk))
    return out


def build_title_card(
    text: str, out: Path, *, max_w: int = 940, size: int = 58, max_lines: int = 2
) -> tuple[Path, int, int]:
    """Pinta el titulo (con emojis en color) sobre PNG transparente."""
    text_font = ImageFont.truetype(settings.render_font_file, size)
    emo_font = _emoji_font(size)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    def seg_w(is_emoji: bool, chunk: str) -> float:
        if is_emoji:
            return size * 1.15 * len(chunk.replace("\ufe0f", "").replace("\u200d", ""))
        return probe.textlength(chunk, font=text_font)

    # Palabras con su tramo, para poder partir en lineas sin romper un emoji.
    tokens: list[tuple[bool, str]] = []
    for is_emoji, chunk in _segments(text):
        if is_emoji:
            tokens.append((True, chunk))
        else:
            parts = chunk.split(" ")
            for i, w in enumerate(parts):
                if w:
                    tokens.append((False, w))
                if i < len(parts) - 1:
                    tokens.append((False, " "))

    lines: list[list[tuple[bool, str]]] = [[]]
    width = 0.0
    for is_emoji, tok in tokens:
        w = seg_w(is_emoji, tok)
        if width + w > max_w and lines[-1] and len(lines) < max_lines:
            lines.append([])
            width = 0.0
            if not is_emoji and tok == " ":
                continue
        lines[-1].append((is_emoji, tok))
        width += w

    line_h = int(size * 1.28)
    img_h = line_h * len(lines) + 20
    img = Image.new("RGBA", (max_w + 80, img_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        total = sum(seg_w(e, t) for e, t in line)
        x = (img.width - total) / 2
        y = 10 + i * line_h
        for is_emoji, tok in line:
            if is_emoji:
                cursor = x
                for unit in _emoji_units(tok):
                    png = apple_emoji_png(unit)
                    box = int(size * 1.05)
                    if png is not None:
                        art = Image.open(png).convert("RGBA").resize(
                            (box, box), Image.LANCZOS
                        )
                        img.alpha_composite(art, (int(cursor), int(y + size * 0.10)))
                    elif emo_font is not None:
                        d.text((cursor, y + size * 0.08), unit, font=emo_font,
                               embedded_color=True)
                    cursor += size * 1.15
            else:
                d.text((x, y), tok, font=text_font, fill=WHITE,
                       stroke_width=max(3, size // 12), stroke_fill=(8, 8, 10, 235))
            x += seg_w(is_emoji, tok)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out, img.width, img.height
