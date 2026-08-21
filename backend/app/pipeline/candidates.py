"""Fusion de senales, deteccion de picos y seleccion de ventanas candidatas."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import settings
from ..utils import log
from .signals import SignalSet

# z minimo para considerar que una senal "esta caliente" a efectos del bonus de combo.
HOT_Z = 2.0
# Escalones de percentil a los que bajamos si el umbral estricto deja muy pocos picos.
PERCENTILE_FALLBACKS = (92.0, 85.0, 70.0, 50.0)


@dataclass(slots=True)
class Candidate:
    t_peak: float
    t_start: float
    t_end: float
    signal_score: float
    chat_z: float
    audio_z: float
    unique_users: int
    msg_count: int
    chat_ratio: float
    combo: bool


def shift_earlier(x: np.ndarray, seconds: float, bin_seconds: float) -> np.ndarray:
    """Adelanta una senal en el eje temporal: `out[i] = x[i + seconds/bin]`.

    El chat reacciona 3-15 s *despues* del evento. Si centramos el clip en el pico de
    chat, el momento interesante se queda fuera por delante. Adelantar el chat alinea
    su pico con el instante en que ocurrio la cosa.
    """
    if x.size == 0 or seconds <= 0:
        return x
    k = int(round(seconds / max(bin_seconds, 0.1)))
    if k <= 0:
        return x
    tail = np.full(min(k, x.size), x[-1] if x.size else 0.0)
    return np.concatenate([x[k:], tail])[: x.size]


def _window_max(x: np.ndarray, win_bins: int) -> np.ndarray:
    if x.size == 0 or win_bins <= 1:
        return x
    win_bins = min(win_bins, x.size)
    pad = win_bins // 2
    padded = np.pad(x, (pad, win_bins - 1 - pad), mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, win_bins)
    return view.max(axis=1)[: x.size]


def fuse(signals: SignalSet) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Devuelve (score por bin, mascara de combo, z de chat ya alineado)."""
    bs = signals.bin_seconds
    lag = settings.chat_lag_s

    if signals.chat_available:
        w_chat, w_users, w_emote, w_audio = (
            settings.w_chat, settings.w_users, settings.w_emote, settings.w_audio
        )
    else:
        # Sin chat, el audio es la unica senal: se lleva todo el peso.
        w_chat = w_users = w_emote = 0.0
        w_audio = 1.0

    z_chat = shift_earlier(signals.z_chat, lag, bs)
    z_users = shift_earlier(signals.z_users, lag, bs)
    z_emote = shift_earlier(signals.z_emote, lag, bs)
    z_audio = signals.z_audio

    score = w_chat * z_chat + w_users * z_users + w_emote * z_emote + w_audio * z_audio

    combo = np.zeros(signals.n_bins, dtype=bool)
    if signals.chat_available and signals.audio_available:
        win_bins = max(1, int(settings.combo_window_s / max(bs, 0.1)))
        chat_hot = _window_max(z_chat, win_bins) >= HOT_Z
        audio_hot = _window_max(z_audio, win_bins) >= HOT_Z
        combo = chat_hot & audio_hot
        # La coincidencia de dos senales independientes es mucho mas predictiva que un
        # pico grande en una sola.
        score = np.where(combo, score * settings.combo_bonus, score)

    return score, combo, z_chat


def _edge_mask(signals: SignalSet) -> np.ndarray:
    """Descarta el arranque ("starting soon") y el cierre del VOD."""
    trim = settings.edge_trim_s
    n = signals.n_bins
    mask = np.ones(n, dtype=bool)
    if signals.duration <= 4 * trim:
        return mask
    lo = int(trim / signals.bin_seconds)
    hi = n - int(trim / signals.bin_seconds)
    mask[:lo] = False
    mask[max(lo, hi):] = False
    return mask


def pick_peaks(
    score: np.ndarray, allowed: np.ndarray, percentile: float, min_gap_bins: int
) -> list[int]:
    """Picos por encima del percentil, con non-maximum suppression greedy."""
    usable = score[allowed]
    if usable.size == 0:
        return []
    threshold = float(np.percentile(usable, percentile))
    cand = np.flatnonzero(allowed & (score >= threshold) & (score > 0))
    if cand.size == 0:
        return []
    order = cand[np.argsort(-score[cand], kind="stable")]
    chosen: list[int] = []
    for idx in order:
        if all(abs(int(idx) - c) >= min_gap_bins for c in chosen):
            chosen.append(int(idx))
    return chosen


def select_candidates(signals: SignalSet, *, min_wanted: int = 5) -> list[Candidate]:
    score, combo, z_chat_aligned = fuse(signals)
    allowed = _edge_mask(signals)
    bs = signals.bin_seconds
    min_gap_bins = max(1, int(settings.min_gap_s / max(bs, 0.1)))

    percentiles = (settings.peak_percentile, *PERCENTILE_FALLBACKS)
    peaks: list[int] = []
    for pct in percentiles:
        peaks = pick_peaks(score, allowed, pct, min_gap_bins)
        if len(peaks) >= min_wanted:
            if pct != settings.peak_percentile:
                log.info("umbral de picos relajado al percentil %.0f", pct)
            break

    peaks = peaks[: settings.max_candidates]
    out: list[Candidate] = []
    for idx in peaks:
        t_peak = signals.bin_time(idx)
        # Ventana asimetrica: el evento ocurre *antes* del pico de reaccion.
        t_start = max(0.0, t_peak - settings.window_before_s)
        t_end = min(signals.duration, t_peak + settings.window_after_s)
        if t_end - t_start < settings.min_clip_s:
            t_end = min(signals.duration, t_start + settings.min_clip_s)
        lo = signals.index_at(max(0.0, t_peak - settings.combo_window_s))
        hi = signals.index_at(min(signals.duration, t_peak + settings.combo_window_s)) + 1
        msgs = float(signals.msg_count[lo:hi].sum()) if hi > lo else 0.0
        base = (
            float(signals.msg_baseline[lo:hi].sum())
            if hi > lo and signals.msg_baseline.size
            else 0.0
        )
        ratio = msgs / base if base >= 0.5 else 0.0
        out.append(
            Candidate(
                t_peak=t_peak,
                t_start=t_start,
                t_end=t_end,
                signal_score=float(score[idx]),
                # El z de chat que se reporta es el alineado: es el que decidio el pico.
                chat_z=float(z_chat_aligned[idx]),
                audio_z=float(signals.z_audio[idx]),
                unique_users=int(signals.unique_users[lo:hi].max() if hi > lo else 0),
                msg_count=int(msgs),
                chat_ratio=round(ratio, 2),
                combo=bool(combo[idx]),
            )
        )
    out.sort(key=lambda c: -c.signal_score)
    return out


def normalize_scores(values: list[float]) -> list[float]:
    """Min-max a [0, 1]; si todos son iguales devuelve 0.5 para no falsear el ranking."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]
