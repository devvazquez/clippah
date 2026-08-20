"""Modelos pydantic compartidos por la API y el pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Category = Literal[
    "reaccion", "gracioso", "habilidad", "fail", "polemica", "informativo", "otro"
]
CATEGORIES: tuple[str, ...] = (
    "reaccion",
    "gracioso",
    "habilidad",
    "fail",
    "polemica",
    "informativo",
    "otro",
)

Platform = Literal["twitch", "youtube"]
JobStatus = Literal["queued", "running", "done", "error", "cancelled"]


class JobCreate(BaseModel):
    url: str = Field(min_length=8, max_length=500)


class VideoOut(BaseModel):
    id: str
    platform: Platform
    ext_id: str
    url: str
    title: str = ""
    duration: float = 0.0
    uploader: str = ""
    upload_date: str = ""
    thumbnail: str = ""


class MomentSignals(BaseModel):
    chat_z: float = 0.0
    audio_z: float = 0.0
    unique_users: int = 0
    msg_count: int = 0
    combo: bool = False


class Word(BaseModel):
    text: str
    start: float
    end: float


class MomentOut(BaseModel):
    id: str
    video_id: str
    t_start: float
    t_end: float
    t_peak: float
    duration: float
    title: str
    description: str
    category: Category
    final_score: float
    signals: MomentSignals
    transcript: str
    enriched: bool
    thumbnail_url: str


class JobOut(BaseModel):
    id: str
    url: str
    status: JobStatus
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    chat_available: bool = False
    chat_messages: int = 0
    enriched: bool = False
    transcribed: bool = False
    warnings: list[str] = Field(default_factory=list)
    providers: dict[str, Any] = Field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float | None = None
    video: VideoOut | None = None
    moments: list[MomentOut] = Field(default_factory=list)


class JobCreated(BaseModel):
    job_id: str
    video: VideoOut | None = None


class JobListItem(BaseModel):
    id: str
    url: str
    status: JobStatus
    stage: str = ""
    progress: float = 0.0
    title: str = ""
    thumbnail: str = ""
    uploader: str = ""
    duration: float = 0.0
    moments: int = 0
    created_at: float = 0.0


class JobList(BaseModel):
    items: list[JobListItem]
    total: int
    limit: int
    offset: int


class ProviderHealth(BaseModel):
    name: str
    configured: bool
    available: bool
    requests_today: int = 0
    requests_limit: int | None = None
    units_today: float = 0.0
    units_limit: float | None = None
    note: str = ""


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    mode: Literal["cloud", "hybrid", "local"] = "local"
    ffmpeg: bool = False
    ytdlp: bool = False
    faster_whisper: bool = False
    transcriber: str = "none"
    scorer: str = "heuristic"
    providers: list[ProviderHealth] = Field(default_factory=list)


class SfxCue(BaseModel):
    t: float
    name: str
    gain_db: float = 0.0


class RenderSpec(BaseModel):
    """Contrato de la fase 2 (render del clip). Se construye completo, no se ejecuta."""

    moment_id: str
    source_url: str
    t_start: float
    t_end: float
    aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    title: str
    captions: list[Word] = Field(default_factory=list)
    sfx_cues: list[SfxCue] = Field(default_factory=list)
