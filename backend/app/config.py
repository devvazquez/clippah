"""Configuracion global leida de variables de entorno / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Raiz del backend (donde viven pyproject.toml y .env). Todas las rutas relativas se
# resuelven contra esto y no contra el cwd: si no, un script lanzado desde la raiz del
# repo crearia su propio ./data en otro sitio y no reutilizaria la cache del servidor.
SYSTEM_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Emotes y expresiones de hype. Se definen aqui (no en la logica de senales) para poder
# ajustarlos por streamer/idioma sin tocar el pipeline.
DEFAULT_HYPE_EMOTES = (
    "PogChamp POGGERS Pog KEKW LULW OMEGALOL EZ Clap CLIPIT monkaS Sadge KEKL LMAO "
    "PepeLaugh Pepega POGGIES WEIRDCHAMP OMEGALUL 5Head Copium MonkaW HYPERS"
)

# ES/CA: los streamers de aqui mezclan castellano y catalan, y las keywords en ingles
# no capturan nada. Estas expresiones son las que realmente aparecen en el chat.
DEFAULT_HYPE_KEYWORDS = (
    "jajaja jajajaja jaja jejeje madre mia que crack no me lo creo hostia flipa buah "
    "brutal olé ole increible wtf brutal_ ostia hostias bufff dios flipando vaya "
    "quin crack quina passada mare meva no fotis increible"
)

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gracioso": ("jajaja", "jaja", "kekw", "lmao", "lul", "risa", "jejeje", "xd", "meme"),
    "fail": ("fail", "manco", "muerto", "pierdo", "perdi", "he muerto", "ns", "cagada"),
    "habilidad": ("clutch", "insane", "crack", "ace", "highlight", "pogchamp", "brutal"),
    "reaccion": ("madre mia", "no me lo creo", "hostia", "wtf", "flipa", "buah", "dios"),
    "polemica": ("polemica", "drama", "cancelado", "pelea", "mierda", "toxico"),
    "informativo": ("explico", "os cuento", "noticia", "anuncio", "tutorial", "aviso"),
}


def _split_words(raw: str) -> list[str]:
    return [w for w in (raw.replace(",", " ").split()) if w]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_ROOT / ".env", BACKEND_ROOT.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Proveedores ---
    groq_api_key: str = ""
    gemini_api_key: str = ""

    # --- Pipeline ---
    download_mode: str = "stream_seek"
    max_vod_hours: float = 8.0
    bin_seconds: float = 2.0
    chat_lag_s: float = 8.0
    w_chat: float = 1.0
    w_users: float = 1.2
    w_emote: float = 1.5
    w_audio: float = 0.8
    peak_percentile: float = 97.0
    min_gap_s: float = 45.0
    max_candidates: int = 30
    top_n: int = 12
    min_clip_s: float = 12.0
    max_clip_s: float = 60.0

    # --- Ventanas / bordes ---
    edge_trim_s: float = 60.0
    window_before_s: float = 25.0
    window_after_s: float = 15.0
    baseline_window_s: float = 300.0
    combo_bonus: float = 1.4
    combo_window_s: float = 10.0
    # Por debajo de este ritmo el chat no discrimina: el z-score se satura en el suelo
    # y todos los bins con mensaje valen igual. Se avisa, no se descarta.
    min_chat_rate_per_min: float = 3.0
    mute_gap_s: float = 30.0

    # --- Local ---
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"
    whisper_compute_type: str = ""
    data_dir: Path = Path("./data")
    keep_media: bool = False

    # --- yt-dlp ---
    # YouTube pide verificacion anti-bot desde IPs de datacenter (y a veces desde
    # conexiones domesticas). Pasar cookies del navegador lo resuelve.
    ytdlp_cookies_from_browser: str = ""   # p.ej. "firefox", "chrome", "brave:Default"
    ytdlp_cookies_file: str = ""           # ruta a un cookies.txt en formato Netscape
    ytdlp_extra_args: str = ""             # argumentos extra, tal cual

    # --- Servidor ---
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    max_concurrent_jobs: int = 1

    # --- Cuotas Groq (tier gratuito, agosto 2026) ---
    groq_rpm: int = 20
    groq_rpd: int = 2000
    groq_ash: int = 7200
    groq_asd: int = 28800
    groq_model: str = "whisper-large-v3-turbo"
    groq_min_billed_seconds: float = 10.0
    groq_max_file_mb: float = 25.0

    # --- Cuotas Gemini (varian por region; el codigo lee el valor real de cabeceras) ---
    gemini_rpm: int = 15
    gemini_rpd: int = 1000
    gemini_tpm: int = 250_000
    # Los alias `-latest` evitan que un modelo retirado rompa la app: `gemini-2.5-flash-lite`
    # dejo de estar disponible para cuentas nuevas y devuelve 404.
    gemini_model: str = "gemini-flash-lite-latest"
    gemini_fallback_model: str = "gemini-flash-latest"
    gemini_batch_size: int = 10

    # --- Vision (proponente visual) ---
    # Las senales de audio y chat solo encuentran momentos con *reaccion*. Un hito
    # visual silencioso (equipo raro conseguido, construccion acabada) no levanta el
    # audio ni el chat, asi que nunca llega a ser candidato. Este proponente mira la
    # pantalla: muestrea fotogramas y le pregunta a Gemini cuales muestran algo.
    vision_enabled: bool = True          # requiere GEMINI_API_KEY; si no, se desactiva
    vision_sample_s: float = 20.0        # un fotograma cada N segundos
    vision_frame_width: int = 512
    vision_batch: int = 15               # fotogramas por peticion
    vision_min_confidence: float = 60.0
    vision_max_hits: int = 12            # candidatos visuales aceptados como maximo
    vision_model: str = ""               # vacio = usa gemini_model

    # --- Render del clip (vertical 9:16 con subtitulos quemados) ---
    # Medido sobre los 30 clips mas vistos de auronplay e ibai: mediana de 26 s los dos.
    # Los del canal pequeno que usa esto: mediana de 20 s. Ninguno pasa de 60 s (el tope
    # que impone Twitch), y el 60% cae entre 16 y 30 s.
    target_clip_s: float = 26.0
    render_layout: str = "blur"        # blur | crop | split
    # Dos fuentes a proposito, que las instala `make setup-font`: el titulo es el reclamo
    # del post y los subtitulos son la voz. Con la misma familia y el mismo peso los dos
    # bloques se leian como si fueran lo mismo. Sin instalarlas se cae a DejaVu.
    render_title_font: str = "Montserrat"     # geometrica, de cartel
    render_font: str = "Barlow"               # algo estrecha, de subtitulo
    render_font_size: int = 66
    render_title_size: int = 58
    # BorderStyle 4 = caja por linea. `render_outline` es el margen de la caja y
    # `render_box_alpha` lo transparente que queda (00 opaca, FF invisible): con 0x8C se
    # lee sobre cualquier fondo sin tapar el gameplay como el bloque negro de antes.
    render_outline: int = 8
    render_shadow: int = 0
    render_box_alpha: str = "8C"
    render_words_per_line: int = 3     # 2-3 palabras se leen de un vistazo en vertical
    render_chars_per_line: int = 22
    # En minusculas se lee mas tranquilo; TODO EN MAYUSCULAS grita y cansa.
    render_uppercase: bool = False
    render_blur_sigma: int = 26
    # Zoom del bloque de video en el layout blur. 1.0 no recorta nada; subirlo agranda la
    # imagen a costa de los laterales, donde estos directos suelen tener la webcam.
    render_zoom: float = 1.0
    render_crf: int = 20
    render_preset: str = "veryfast"
    render_fps: int = 30
    render_timeout_s: float = 900.0

    # --- Layout de dos camaras ---
    # Los rectangulos de las camaras no se configuran: los mide `detect_cam_layout` sobre
    # los fotogramas del propio VOD, porque una escena de OBS cambia de un canal a otro y
    # hasta de un directo a otro. Si no se localizan, el render usa el layout `blur`.
    # Alto de cada banda de camara. La webcam mide 566x319 en el fotograma, asi que para
    # llenar los 1080 de ancho hay que escalarla 1.91x y queda de 608 de alto: cuanto mas
    # baja sea la banda, mas se recorta por arriba y por abajo. Con 420 se tiraban 94 px
    # por lado y se comian la frente y la barbilla.
    render_cam_band: int = 520
    # De lo que sobra al recortar, que fraccion se quita por arriba. La cara vive en la
    # parte alta del encuadre de una webcam, asi que conviene tirar mas por abajo.
    render_cam_anchor: float = 0.30

    # --- Efectos de sonido ---
    render_sfx: bool = True
    render_sfx_riser: str = "riser-short.mp3"   # riser-short | riser-long
    render_sfx_boom: str = "vineboom.mp3"
    render_sfx_riser_db: float = -7.0
    render_sfx_boom_db: float = -9.0

    # --- Musica de fondo (Kevin MacLeod, CC BY 3.0: hay que acreditarla al publicar) ---
    render_music: bool = True
    # Muy por debajo de la voz: acompana, no interviene. -30 dB sobre el pico medido.
    render_music_db: float = -30.0
    render_music_fade_s: float = 1.5
    render_music_default: str = "fluffing-a-duck.mp3"

    # --- Redes del streamer que se queman en el clip ---
    # Formato: "plataforma|etiqueta|url" separados por ";". Plataformas con logo:
    # twitch, youtube, tiktok.
    social_links: str = (
        "tiktok|@rexxyconh|https://www.tiktok.com/@rexxyconh;"
        "youtube|REXXYCONH|https://www.youtube.com/channel/UCNI69ziiM4dUYL7N6Mi4I1g;"
        "twitch|/rexxyconh|https://www.twitch.tv/rexxyconh"
    )
    render_show_social: bool = True

    # --- Supabase: la cola compartida con la interfaz ---
    # Sin estas dos, el backend funciona igual que antes y el worker de la cola no
    # arranca. La clave es la `service_role`: se salta RLS, asi que no sale de aqui.
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_bucket: str = "clips"
    # Cada cuanto se pregunta por peticiones nuevas. El navegador va por Realtime; este
    # lado sondea, y dos segundos es suficiente para que se note instantaneo.
    supabase_poll_s: float = 2.0
    # Cuanto duran las URLs firmadas que la interfaz usa para ver y descargar.
    supabase_signed_url_s: int = 86400

    # --- Hype ---
    hype_emotes: str = Field(default=DEFAULT_HYPE_EMOTES)
    hype_keywords: str = Field(default=DEFAULT_HYPE_KEYWORDS)

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (BACKEND_ROOT / v).resolve()

    @field_validator("download_mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"stream_seek", "full"}:
            raise ValueError("DOWNLOAD_MODE debe ser 'stream_seek' o 'full'")
        return v

    @property
    def hype_emote_set(self) -> set[str]:
        return {w.lower() for w in _split_words(self.hype_emotes)}

    @property
    def hype_keyword_list(self) -> list[str]:
        return [w.lower() for w in _split_words(self.hype_keywords)]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.db"

    @property
    def fonts_dir(self) -> Path:
        """Fuentes propias del proyecto (las instala `make setup-font`)."""
        return BACKEND_ROOT / "assets" / "fonts"

    @property
    def font_file(self) -> Path:
        """El .ttf del titulo quemado (lo dibuja Pillow), con reserva del sistema."""
        montserrat = self.fonts_dir / "Montserrat-Bold.ttf"
        return montserrat if montserrat.exists() else SYSTEM_FONT

    @property
    def font_family(self) -> str:
        """Familia de los subtitulos para libass, segun lo que haya instalado."""
        return self.render_font if (self.fonts_dir / "Barlow-Bold.ttf").exists() \
            else "DejaVu Sans"

    @property
    def emoji_dir(self) -> Path:
        """Artwork de emojis de Apple (se instala con `make setup-emoji`)."""
        return BACKEND_ROOT / "assets" / "emoji" / "apple"

    @property
    def music_dir(self) -> Path:
        return BACKEND_ROOT / "assets" / "music"

    @property
    def sfx_dir(self) -> Path:
        return BACKEND_ROOT / "assets" / "sfx"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.thumbs_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
