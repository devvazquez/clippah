"""Senales sobre una rejilla comun de bins: RMS de audio, densidad de chat y z-scores.

Todo se normaliza con z-score *local* (mediana y MAD moviles) porque cada streamer
tiene un baseline distinto: un umbral absoluto no generaliza.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import numpy as np
import soundfile as sf

from ..config import settings
from ..utils import log
from .chat import ChatMessage

# dBFS por debajo del cual consideramos el audio "digitalmente muteado".
# Twitch mutea tramos con musica con copyright y eso da RMS ~ 0: si no se excluyen,
# hunden el baseline y generan falsos positivos alrededor.
MUTE_DBFS = -68.0
FLOOR_DBFS = -90.0


@dataclass(slots=True)
class SignalSet:
    bin_seconds: float
    n_bins: int
    duration: float
    audio_db: np.ndarray
    z_audio: np.ndarray
    msg_count: np.ndarray
    unique_users: np.ndarray
    emote_burst: np.ndarray
    z_chat: np.ndarray
    z_users: np.ndarray
    z_emote: np.ndarray
    audio_available: bool = False
    chat_available: bool = False
    valid_mask: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    # Mediana movil de mensajes/bin: el "ritmo normal" del chat en ese tramo. Sirve para
    # dar un multiplicador honesto en la descripcion, en vez de reciclar el z-score.
    msg_baseline: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=float))

    def bin_time(self, index: int) -> float:
        return (index + 0.5) * self.bin_seconds

    def index_at(self, t: float) -> int:
        return int(np.clip(int(t / self.bin_seconds), 0, max(0, self.n_bins - 1)))


def n_bins_for(duration: float, bin_seconds: float) -> int:
    return max(1, int(math.ceil(max(duration, bin_seconds) / bin_seconds)))


# ------------------------------------------------------------------ z-score robusto


def rolling_median_mad(
    x: np.ndarray, win_bins: int, valid: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Mediana y MAD moviles, ignorando los bins invalidos (audio muteado)."""
    n = x.size
    win_bins = max(3, min(win_bins | 1, n if n % 2 else n - 1)) if n >= 3 else 1
    xv = x.astype(float, copy=True)
    if valid is not None:
        xv[~valid] = np.nan
    if win_bins <= 1 or n < 3:
        base = float(np.nanmedian(xv)) if np.any(np.isfinite(xv)) else 0.0
        spread = float(np.nanmedian(np.abs(xv - base))) if np.any(np.isfinite(xv)) else 0.0
        return np.full(n, base), np.full(n, spread)

    pad_left = win_bins // 2
    pad_right = win_bins - 1 - pad_left
    padded = np.pad(xv, (pad_left, pad_right), mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(padded, win_bins)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        med = np.nanmedian(win, axis=1)
        mad = np.nanmedian(np.abs(win - med[:, None]), axis=1)
    fallback = float(np.nanmedian(xv)) if np.any(np.isfinite(xv)) else 0.0
    med = np.nan_to_num(med, nan=fallback)
    mad = np.nan_to_num(mad, nan=0.0)
    return med, mad


def zscore(
    x: np.ndarray,
    *,
    bin_seconds: float,
    window_s: float,
    floor: float,
    valid: np.ndarray | None = None,
) -> np.ndarray:
    """z-score local acotado a [-3, 15]. `floor` evita explosiones cuando el MAD es 0."""
    if x.size == 0:
        return x.astype(float)
    win_bins = max(3, int(window_s / max(bin_seconds, 0.1)))
    med, mad = rolling_median_mad(x, win_bins, valid)
    # 1.4826 * MAD es el estimador consistente de sigma para una normal: asi los
    # z-scores que se muestran en la UI se leen como "sigmas".
    spread = np.maximum(1.4826 * mad, floor)
    z = (x.astype(float) - med) / spread
    return np.clip(z, -3.0, 15.0)


# ------------------------------------------------------------------------- audio


def audio_rms_bins(wav_path, duration: float, bin_seconds: float) -> tuple[np.ndarray, int]:
    """RMS por bin en dBFS leyendo el WAV en bloques (sin cargarlo entero en memoria)."""
    info = sf.info(str(wav_path))
    samplerate = info.samplerate
    block = max(1, int(round(bin_seconds * samplerate)))
    total = n_bins_for(duration, bin_seconds)
    out = np.full(total, np.nan, dtype=float)

    idx = 0
    for chunk in sf.blocks(str(wav_path), blocksize=block, dtype="float32", always_2d=True):
        if idx >= total:
            break
        mono = chunk.mean(axis=1) if chunk.shape[1] > 1 else chunk[:, 0]
        if mono.size == 0:
            break
        rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
        out[idx] = 20.0 * math.log10(rms) if rms > 1e-9 else FLOOR_DBFS
        idx += 1

    covered = idx
    if covered == 0:
        raise RuntimeError("El WAV de audio esta vacio")
    if covered < total:
        # El audio puede ser algo mas corto que la duracion que reporta yt-dlp.
        out[covered:] = FLOOR_DBFS
    return np.nan_to_num(out, nan=FLOOR_DBFS), covered


def smooth(x: np.ndarray, win: int = 3) -> np.ndarray:
    if x.size == 0 or win <= 1:
        return x
    win = min(win, x.size)
    kernel = np.ones(win) / win
    pad = win // 2
    padded = np.pad(x, (pad, pad), mode="edge")
    return np.convolve(padded, kernel, mode="valid")[: x.size]


def muted_mask(audio_db: np.ndarray, bin_seconds: float, min_gap_s: float) -> np.ndarray:
    """True en los bins que pertenecen a un tramo muteado/silencioso largo."""
    quiet = audio_db <= MUTE_DBFS
    if not quiet.any():
        return np.zeros_like(quiet)
    min_run = max(1, int(min_gap_s / max(bin_seconds, 0.1)))
    out = np.zeros_like(quiet)
    start = None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if i - start >= min_run:
                out[start:i] = True
            start = None
    if start is not None and quiet.size - start >= min_run:
        out[start:] = True
    return out


# -------------------------------------------------------------------------- chat


def _tokenize(text: str) -> list[str]:
    return [t for t in text.split() if t]


def chat_bins(
    messages: list[ChatMessage], duration: float, bin_seconds: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(msg_count, unique_users, emote_burst) por bin."""
    total = n_bins_for(duration, bin_seconds)
    msg_count = np.zeros(total, dtype=float)
    emote_burst = np.zeros(total, dtype=float)
    users: list[set[str]] = [set() for _ in range(total)]

    hype_emotes = settings.hype_emote_set
    keywords = settings.hype_keyword_list

    for m in messages:
        b = int(m.t / bin_seconds)
        if b < 0 or b >= total:
            continue
        msg_count[b] += 1.0
        users[b].add(m.user.lower())
        tokens = _tokenize(m.text)
        low = m.text.lower()
        emote_tokens = sum(1 for t in tokens if t.lower() in hype_emotes)
        has_hype = bool(m.emotes) or emote_tokens > 0 or any(k in low for k in keywords)
        if has_hype:
            # Un mensaje que es *solo* emotes pesa mas que uno que los menciona de paso.
            mostly_emotes = tokens and (emote_tokens + len(m.emotes)) >= max(1, len(tokens) // 2)
            emote_burst[b] += 1.5 if mostly_emotes else 1.0

    unique_users = np.array([float(len(s)) for s in users], dtype=float)
    return msg_count, unique_users, emote_burst


# --------------------------------------------------------------------- agregacion


def compute_signals(
    *,
    duration: float,
    audio_path=None,
    messages: list[ChatMessage] | None = None,
) -> SignalSet:
    bs = settings.bin_seconds
    total = n_bins_for(duration, bs)
    zeros = np.zeros(total, dtype=float)

    audio_db = np.full(total, FLOOR_DBFS)
    z_audio = zeros.copy()
    valid = np.ones(total, dtype=bool)
    audio_available = False

    if audio_path is not None:
        try:
            audio_db, _covered = audio_rms_bins(audio_path, duration, bs)
            audio_db = smooth(audio_db, 3)
            muted = muted_mask(audio_db, bs, settings.mute_gap_s)
            valid = ~muted
            if valid.sum() < max(3, total // 20):
                # Practicamente todo muteado: mejor no fiarse de la mascara.
                valid = np.ones(total, dtype=bool)
            z_audio = zscore(
                audio_db, bin_seconds=bs, window_s=settings.baseline_window_s,
                floor=0.5, valid=valid,
            )
            z_audio[~valid] = 0.0
            audio_available = True
            if muted.any():
                log.info(
                    "audio: %d bins muteados excluidos del baseline",
                    int(muted.sum()),
                )
        except Exception as exc:  # noqa: BLE001 - el audio es best-effort
            log.warning("no se pudieron calcular senales de audio: %s", exc)

    msg_count = zeros.copy()
    unique_users = zeros.copy()
    emote_burst = zeros.copy()
    z_chat = zeros.copy()
    z_users = zeros.copy()
    z_emote = zeros.copy()
    chat_available = False

    msg_baseline = zeros.copy()
    if messages:
        msg_count, unique_users, emote_burst = chat_bins(messages, duration, bs)
        win_bins = max(3, int(settings.baseline_window_s / max(bs, 0.1)))
        msg_baseline, _ = rolling_median_mad(msg_count, win_bins)
        z_chat = zscore(
            msg_count, bin_seconds=bs, window_s=settings.baseline_window_s, floor=1.0
        )
        z_users = zscore(
            unique_users, bin_seconds=bs, window_s=settings.baseline_window_s, floor=1.0
        )
        z_emote = zscore(
            emote_burst, bin_seconds=bs, window_s=settings.baseline_window_s, floor=1.0
        )
        chat_available = True

    return SignalSet(
        bin_seconds=bs,
        n_bins=total,
        duration=duration,
        audio_db=audio_db,
        z_audio=z_audio,
        msg_count=msg_count,
        unique_users=unique_users,
        emote_burst=emote_burst,
        z_chat=z_chat,
        z_users=z_users,
        z_emote=z_emote,
        audio_available=audio_available,
        chat_available=chat_available,
        valid_mask=valid,
        msg_baseline=msg_baseline,
    )
