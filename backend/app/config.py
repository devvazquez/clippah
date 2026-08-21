"""Configuracion global leida de variables de entorno / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Raiz del backend (donde viven pyproject.toml y .env). Todas las rutas relativas se
# resuelven contra esto y no contra el cwd: si no, un script lanzado desde la raiz del
# repo crearia su propio ./data en otro sitio y no reutilizaria la cache del servidor.
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
    gemini_model: str = "gemini-2.5-flash-lite"
    gemini_fallback_model: str = "gemini-2.5-flash"
    gemini_batch_size: int = 10

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

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.thumbs_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
