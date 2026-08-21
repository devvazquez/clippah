"""Render del clip: recorte vertical 9:16 con subtitulos quemados.

Formato elegido a partir de lo que de verdad se publica. Medido sobre los 30 clips mas
vistos de auronplay e ibai via el GQL de Twitch: mediana de 26 s los dos, con el 60% de
los clips entre 16 y 30 s. Los del propio canal pequeno que usa esto: mediana de 20 s.
De ahi TARGET_CLIP_S y el recorte al hueco 20-30 s.

Del formato en si: 1080x1920, subtitulos grandes centrados y por encima del 20% inferior
(donde las plataformas ponen su propia interfaz), y nada de perder contenido del
fotograma original -- el layout por defecto rellena con una copia desenfocada en vez de
recortar, porque en estos directos la webcam va compuesta dentro del 16:9 y un recorte
central se la come.
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import settings
from ..models import SfxCue, Word
from ..utils import CommandFailed, clamp, ffmpeg_bin, ffprobe_bin, log, run
from . import branding

ProgressCb = Callable[[float, str], Awaitable[None]]

OUT_W, OUT_H = 1080, 1920
# Los subtitulos se colocan por encima de este margen inferior: ahi van el texto del
# post, el @ del autor y los botones de la plataforma.
SUB_MARGIN_V = 300
# Un 16:9 a lo ancho de un 9:16 solo da 608 px de alto: queda un tercio de lienzo. El
# bloque se centra ligeramente por encima del medio y el subtitulo se pega justo debajo,
# para que la composicion se lea como intencionada y no como un video perdido en el
# centro. Subir el zoom recorta los lados (ahi suele ir la webcam compuesta): por eso el
# defecto es 1.0, sin perder nada.
VIDEO_Y_FRACTION = 0.34

_STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
_EVENT_FORMAT = (
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
)
ASS_HEADER = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\n"
    "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
    "[V4+ Styles]\n" + _STYLE_FORMAT + "\n"
    # BorderStyle 4 dibuja una caja detras de cada linea: sobre el HUD del juego un
    # borde solo no basta, el texto pelea con los corazones y la barra de objetos.
    "Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H55101010,"
    "-1,0,0,0,100,100,0,0,4,{outline},0,2,60,60,{marginv},1\n\n"

    "[Events]\n" + _EVENT_FORMAT + "\n"
)


@dataclass(slots=True)
class RenderResult:
    path: Path
    width: int
    height: int
    duration: float
    layout: str
    captions: int
    size_bytes: int = 0
    sfx: int = 0
    music: str = ""
    social: bool = False


@dataclass(slots=True)
class SubtitleCue:
    start: float
    end: float
    text: str


@dataclass(slots=True)
class RenderOptions:
    layout: str = ""              # blur | crop | split (vacio = el de config)
    show_title: bool = False
    title: str = ""
    uppercase: bool | None = None
    focus_x: float = 0.5          # centro del recorte en modo crop (0-1)
    facecam: tuple[float, float, float, float] | None = None  # x,y,w,h en fraccion
    cam_layout: dict[str, list[float]] | None = None  # detectado por vision.py
    words: list[Word] = field(default_factory=list)
    sfx: list[SfxCue] = field(default_factory=list)
    music: str = ""               # nombre del fichero en assets/music (vacio = ninguna)
    social: bool | None = None


def _zoom_keep() -> float:
    """Fraccion del ancho original que se conserva. 1.0 = no se recorta nada."""
    return clamp(1.0 / max(1.0, settings.render_zoom), 0.5, 1.0)


_SFX_DURATIONS: dict[str, float] = {}


async def sfx_duration(path: Path) -> float:
    """Duracion del efecto, cacheada (se usa para que el riser muera en el pico)."""
    key = str(path)
    if key not in _SFX_DURATIONS:
        res = await run(
            [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            timeout=30,
        )
        try:
            _SFX_DURATIONS[key] = float(res.stdout.strip())
        except ValueError:
            _SFX_DURATIONS[key] = 1.0
    return _SFX_DURATIONS[key]


async def plan_sfx(moment: dict[str, Any]) -> list[SfxCue]:
    """Donde van los efectos: el riser sube hasta el pico y el golpe cae sobre el.

    El pico es el instante que detectaron las senales o la vision, y la ventana es
    asimetrica (empieza 25 s antes), asi que el pico cae hacia el final del clip: el
    riser tiene sitio de sobra para construir.
    """
    if not settings.render_sfx:
        return []
    # El scorer decide si el efecto pega: un riser sobre una conversacion tranquila se
    # nota mas que su ausencia.
    fit = str(moment.get("sfx_fit") or "ninguno").strip().lower()
    if fit not in ("golpe", "riser_golpe"):
        return []
    t_start = float(moment["t_start"])
    t_end = float(moment["t_end"])
    peak = clamp(float(moment["t_peak"]) - t_start, 0.0, max(0.1, t_end - t_start))

    riser = settings.sfx_dir / settings.render_sfx_riser
    boom = settings.sfx_dir / settings.render_sfx_boom
    cues: list[SfxCue] = []
    if fit == "riser_golpe" and riser.exists():
        rd = await sfx_duration(riser)
        start = peak - rd
        if start >= -0.2:  # si no cabe entero, mejor no ponerlo
            cues.append(SfxCue(t=max(0.0, start), name=riser.name,
                               gain_db=settings.render_sfx_riser_db))
    if boom.exists():
        cues.append(SfxCue(t=peak, name=boom.name, gain_db=settings.render_sfx_boom_db))
    return cues


MUSIC_FILES = {
    "fluffing_a_duck": "fluffing-a-duck.mp3",
    "sneaky_snitch": "sneaky-snitch.mp3",
    "sneaky_adventure": "sneaky-adventure.mp3",
}


def music_file(choice: str) -> Path | None:
    """Fichero de la pista elegida por el scorer, si existe."""
    if not settings.render_music:
        return None
    key = (choice or "").strip().lower()
    if key in ("", "ninguna", "none"):
        return None
    name = MUSIC_FILES.get(key, key if key.endswith(".mp3") else "")
    if not name:
        return None
    path = settings.music_dir / name
    return path if path.exists() else None


def clips_dir() -> Path:
    d = settings.data_dir / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clip_path(moment_id: str) -> Path:
    return clips_dir() / f"{moment_id}.mp4"


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").strip()


def group_words(
    words: list[Word], t_start: float, t_end: float, *, max_words: int, max_chars: int
) -> list[SubtitleCue]:
    """Agrupa las palabras en frases cortas, con tiempos relativos al inicio del clip.

    Dos o tres palabras por linea es lo que se lee de un vistazo en vertical; una frase
    entera obliga a parar el scroll para leer, que es justo lo contrario de lo que se
    busca.
    """
    inside = [w for w in words if w.end > t_start and w.start < t_end and w.text.strip()]
    cues: list[SubtitleCue] = []
    chunk: list[Word] = []

    def flush() -> None:
        if not chunk:
            return
        text = " ".join(w.text.strip() for w in chunk).strip()
        if not text:
            chunk.clear()
            return
        start = max(0.0, chunk[0].start - t_start)
        end = max(start + 0.35, min(t_end, chunk[-1].end) - t_start)
        cues.append(SubtitleCue(start=start, end=end, text=text))
        chunk.clear()

    for w in inside:
        prospective = " ".join([*(x.text.strip() for x in chunk), w.text.strip()])
        gap = w.start - chunk[-1].end if chunk else 0.0
        if chunk and (len(chunk) >= max_words or len(prospective) > max_chars or gap > 0.7):
            flush()
        chunk.append(w)
    flush()

    # Sin huecos raros: una linea se queda hasta que empieza la siguiente si el hueco es
    # menor de 0,4 s (evita el parpadeo).
    for a, b in zip(cues, cues[1:], strict=False):
        if 0 < b.start - a.end < 0.4:
            a.end = b.start
    return cues


def build_ass(
    cues: list[SubtitleCue], *, uppercase: bool, sub_marginv: int = SUB_MARGIN_V
) -> str:
    head = ASS_HEADER.format(
        w=OUT_W, h=OUT_H,
        font=settings.render_font, size=settings.render_font_size,
        outline=settings.render_outline, marginv=sub_marginv,
    )
    lines: list[str] = []
    for c in cues:
        text = _ass_escape(c.text)
        if uppercase:
            text = text.upper()
        lines.append(
            f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Caption,,0,0,0,,{text}"
        )
    return head + "\n".join(lines) + "\n"


def _layout_filters(layout: str, opts: RenderOptions) -> list[str]:
    """Cadena de filtros que lleva el 16:9 de origen a 1080x1920."""
    if layout == "crop":
        # Recorte 9:16 del propio fotograma. Encuadra lo que interesa, pero se come lo
        # que quede fuera (incluida la webcam si esta en una esquina).
        fx = clamp(opts.focus_x, 0.0, 1.0)
        return [
            f"[0:v]crop=w=ih*9/16:h=ih:x='(iw-ih*9/16)*{fx:.3f}':y=0,"
            f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1[comp]"
        ]

    if layout == "split" and opts.facecam:
        # Webcam arriba, juego abajo: el layout clasico cuando la camara se puede aislar.
        x, y, w, h = opts.facecam
        cam_h = int(OUT_H * 0.38) // 2 * 2
        game_h = OUT_H - cam_h
        return [
            "[0:v]split=2[cam][game]",
            f"[cam]crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f},"
            f"scale={OUT_W}:{cam_h}:flags=lanczos,setsar=1[camv]",
            f"[game]crop=w=ih*9/16:h=ih:x='(iw-ih*9/16)*0.5':y=0,"
            f"scale={OUT_W}:{game_h}:flags=lanczos,setsar=1[gamev]",
            "[camv][gamev]vstack=inputs=2,setsar=1[comp]",
        ]

    if layout == "cams":
        # El gameplay al centro y ampliado, con una camara arriba y otra abajo. El
        # recorte del juego se toma del hueco entre las dos camaras: asi no se cuelan
        # por los lados, que es lo que pasaba recortando el centro a ciegas.
        det = opts.cam_layout or {}
        top = tuple(det["top"]) if det.get("top") else settings.cam_rect(settings.render_cam_top)
        bottom = (
            tuple(det["bottom"]) if det.get("bottom")
            else settings.cam_rect(settings.render_cam_bottom)
        )
        band = max(120, min(settings.render_cam_band, (OUT_H - 400) // 2))
        game_h = OUT_H - 2 * band
        anchor = clamp(settings.render_cam_anchor, 0.0, 1.0)
        if not (top and bottom):
            log.warning("coordenadas de camara invalidas: se cae al layout blur")
        else:
            tx, ty, tw, th = top
            bx, by, bw, bh = bottom
            gx0, gx1 = det.get("game_x") or (tx + tw, bx)
            gx0, gx1 = clamp(gx0, 0.0, 1.0), clamp(gx1, 0.0, 1.0)
            if gx1 - gx0 < 0.12:      # hueco irreal: mejor el centro
                gx0, gx1 = 0.30, 0.70
            safe_w = gx1 - gx0
            # Recorte con la proporcion de la banda de juego, tan grande como quepa en
            # el hueco, y centrado en el (ligeramente por encima del medio vertical,
            # donde esta la accion).
            target = OUT_W / game_h
            crop_w = safe_w
            crop_h = crop_w * (16 / 9) / target      # en fraccion de altura
            if crop_h > 1.0:
                crop_h = 1.0
                crop_w = target * crop_h * (9 / 16)
            cx = gx0 + (safe_w - crop_w) / 2
            cy = clamp(0.46 - crop_h / 2, 0.0, 1.0 - crop_h)
            return [
                "[0:v]split=3[ct][cg][cb]",
                f"[ct]crop=w=iw*{tw:.4f}:h=ih*{th:.4f}:x=iw*{tx:.4f}:y=ih*{ty:.4f},"
                f"scale={OUT_W}:{band}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={OUT_W}:{band}:0:'(ih-{band})*{anchor:.3f}',setsar=1[topv]",
                f"[cg]crop=w=iw*{crop_w:.4f}:h=ih*{crop_h:.4f}:"
                f"x=iw*{cx:.4f}:y=ih*{cy:.4f},"
                f"scale={OUT_W}:{game_h}:flags=lanczos,setsar=1[gamev]",
                f"[cb]crop=w=iw*{bw:.4f}:h=ih*{bh:.4f}:x=iw*{bx:.4f}:y=ih*{by:.4f},"
                f"scale={OUT_W}:{band}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={OUT_W}:{band}:0:'(ih-{band})*{anchor:.3f}',setsar=1[botv]",
                "[topv][gamev][botv]vstack=inputs=3,setsar=1[comp]",
            ]

    # blur (por defecto): el 16:9 completo a lo ancho, sobre una copia ampliada y
    # desenfocada de si mismo. No pierde nada del fotograma original.
    return [
        "[0:v]split=2[bg][fg]",
        f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},gblur=sigma={settings.render_blur_sigma},"
        f"eq=brightness=-0.06:saturation=0.85,setsar=1[bgv]",
        f"[fg]crop=w=iw*{_zoom_keep():.4f}:h=ih:x='iw*(1-{_zoom_keep():.4f})/2':y=0,"
        f"scale={OUT_W}:-2:flags=lanczos,setsar=1[fgv]",
        f"[bgv][fgv]overlay=x=(W-w)/2:y=(H-h)*{VIDEO_Y_FRACTION}[comp]",
    ]


async def render_clip(
    source: str,
    moment: dict[str, Any],
    opts: RenderOptions,
    *,
    out: Path | None = None,
    progress: ProgressCb | None = None,
) -> RenderResult:
    """Renderiza el clip vertical. `source` es un fichero local o una URL de stream."""
    t_start = float(moment["t_start"])
    t_end = float(moment["t_end"])
    duration = max(1.0, t_end - t_start)
    out = out or clip_path(str(moment["id"]))
    out.parent.mkdir(parents=True, exist_ok=True)

    layout = (opts.layout or settings.render_layout).strip().lower()
    uppercase = settings.render_uppercase if opts.uppercase is None else opts.uppercase
    show_social = settings.render_show_social if opts.social is None else opts.social

    # Con camaras arriba y abajo, los textos tienen que caer sobre la franja del juego:
    # encima de una cara no se lee y tapa lo que se quiere ver.
    if layout == "cams":
        band = max(120, min(settings.render_cam_band, (OUT_H - 400) // 2))
        social_y = band + 14
        title_marginv = social_y + branding.BAR_H + 16
        sub_marginv = band + 90
    else:
        social_y = OUT_H - branding.BAR_H - 40
        title_marginv = 130
        sub_marginv = SUB_MARGIN_V

    cues = group_words(
        opts.words, t_start, t_end,
        max_words=settings.render_words_per_line,
        max_chars=settings.render_chars_per_line,
    )
    filters = _layout_filters(layout, opts)
    last = "[comp]"

    inputs: list[str] = []
    extra_index = 1

    if show_social:
        links = branding.default_links()
        if links:
            bar = branding.build_social_bar(links, out.with_suffix(".bar.png"))
            inputs += ["-i", str(bar)]
            filters.append(
                f"{last}[{extra_index}:v]overlay=x=(W-w)/2:y={social_y}[social]"
            )
            last = "[social]"
            extra_index += 1
        else:
            show_social = False

    # El titulo va como PNG y no por ASS: libass rasteriza los emojis en monocromo.
    title = opts.title.strip() if opts.show_title else ""
    if title:
        card, _cw, _ch = branding.build_title_card(
            title, out.with_suffix(".title.png"), size=settings.render_title_size
        )
        inputs += ["-i", str(card)]
        filters.append(
            f"{last}[{extra_index}:v]overlay=x=(W-w)/2:y={title_marginv}[titled]"
        )
        last = "[titled]"
        extra_index += 1

    ass_path = out.with_suffix(".ass")
    if cues:
        ass_path.write_text(
            build_ass(cues, uppercase=uppercase, sub_marginv=sub_marginv),
            encoding="utf-8",
        )
        escaped = str(ass_path).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
        filters.append(f"{last}ass=filename='{escaped}'[subbed]")
        last = "[subbed]"
    if not cues:
        log.info("clip %s sin palabras alineadas: se renderiza sin subtitulos", moment["id"])

    # --- audio: voz original + riser hasta el pico + golpe en el pico ---
    sfx = opts.sfx
    audio_map = "0:a?"
    audio_filters: list[str] = []
    mixed = []
    for cue in sfx:
        path = settings.sfx_dir / cue.name
        if not path.exists():
            continue
        inputs += ["-i", str(path)]
        label = f"sfx{extra_index}"
        delay_ms = int(max(0.0, cue.t) * 1000)
        audio_filters.append(
            f"[{extra_index}:a]adelay={delay_ms}|{delay_ms},volume={cue.gain_db}dB,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[{label}]"
        )
        mixed.append(f"[{label}]")
        extra_index += 1
    music_path = music_file(opts.music)
    music_name = ""
    if music_path is not None:
        inputs += ["-stream_loop", "-1", "-i", str(music_path)]
        fade = max(0.2, settings.render_music_fade_s)
        audio_filters.append(
            f"[{extra_index}:a]atrim=0:{duration:.3f},asetpts=N/SR/TB,"
            f"volume={settings.render_music_db}dB,"
            f"afade=t=in:st=0:d={fade:.2f},"
            f"afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.2f},"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[music]"
        )
        mixed.append("[music]")
        music_name = music_path.name
        extra_index += 1

    if mixed:
        audio_filters.insert(
            0,
            "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[voice]",
        )
        audio_filters.append(
            f"[voice]{''.join(mixed)}amix=inputs={len(mixed) + 1}:duration=first:"
            f"dropout_transition=0:normalize=0,alimiter=limit=0.97[aout]"
        )
        audio_map = "[aout]"

    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{t_start:.3f}", "-t", f"{duration:.3f}", "-i", source,
        *inputs,
        "-filter_complex", ";".join([*filters, *audio_filters]),
        "-map", last, "-map", audio_map,
        "-c:v", "libx264", "-preset", settings.render_preset, "-crf", str(settings.render_crf),
        "-pix_fmt", "yuv420p", "-r", str(settings.render_fps),
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        str(out),
    ]
    if progress:
        await progress(0.1, f"Renderizando {duration:.0f} s en vertical")
    await run(cmd, timeout=settings.render_timeout_s)
    ass_path.unlink(missing_ok=True)
    out.with_suffix(".bar.png").unlink(missing_ok=True)
    out.with_suffix(".title.png").unlink(missing_ok=True)

    if not out.exists() or out.stat().st_size < 4096:
        raise CommandFailed(cmd, 0, "ffmpeg no produjo un mp4 valido")
    if progress:
        await progress(1.0, "Clip listo")
    return RenderResult(
        path=out, width=OUT_W, height=OUT_H, duration=duration, layout=layout,
        captions=len(cues), size_bytes=out.stat().st_size,
        sfx=len([m for m in mixed if m != "[music]"]), music=music_name,
        social=show_social,
    )


def have_render_deps() -> bool:
    return shutil.which("ffmpeg") is not None
