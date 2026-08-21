#!/usr/bin/env python
"""Comprueba, sin tocar la red, las dos decisiones que mas se fallan en este problema:

1. El chat reacciona con retraso, asi que el pico detectado debe caer en el *evento*,
   no en la reaccion (CHAT_LAG_S).
2. El bonus de combo se aplica cuando coinciden pico de audio y pico de chat.

    backend/.venv/bin/python scripts/check_alignment.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.pipeline.candidates import fuse, select_candidates  # noqa: E402
from app.pipeline.chat import ChatMessage  # noqa: E402
from app.pipeline.signals import compute_signals  # noqa: E402
from app.pipeline.transcribe import refine_bounds  # noqa: E402
from app.providers.base import Word  # noqa: E402

DURATION = 1200.0
EVENTS = (400.0, 800.0)
LAG = 8.0


def synthetic_chat() -> list[ChatMessage]:
    msgs = [
        ChatMessage(t=float(t), user=f"u{t % 7}", text="hola", emotes=[])
        for t in range(0, int(DURATION), 2)
    ]
    for event in EVENTS:
        for i in range(60):
            # La rafaga llega LAG segundos DESPUES del evento.
            msgs.append(
                ChatMessage(
                    t=event + LAG + i * 0.15,
                    user=f"b{i}",
                    text="KEKW jajaja",
                    emotes=["KEKW"],
                )
            )
    return sorted(msgs, key=lambda m: m.t)


def main() -> int:
    failures: list[str] = []

    signals = compute_signals(duration=DURATION, audio_path=None, messages=synthetic_chat())
    score, combo, z_aligned = fuse(signals)
    cands = select_candidates(signals)

    print(f"CHAT_LAG_S={settings.chat_lag_s:g}  eventos sinteticos en {EVENTS}")
    print(f"{len(cands)} candidatos:")
    for c in cands[:4]:
        print(
            f"  pico={c.t_peak:7.1f}s  ventana=[{c.t_start:.0f},{c.t_end:.0f}]  "
            f"score={c.signal_score:6.2f}  msgs={c.msg_count:3d}  ratio={c.chat_ratio:.2f}"
        )

    print("\n1. El pico debe caer en el evento, no en la reaccion:")
    for event in EVENTS:
        near = [c for c in cands if abs(c.t_peak - event) <= 4.0]
        reaction = [c for c in cands if abs(c.t_peak - (event + LAG)) <= 2.0]
        if near:
            print(f"   evento {event:.0f}s -> pico en {near[0].t_peak:.1f}s  OK")
        elif reaction:
            failures.append(f"el pico de {event:.0f}s se detecto en la reaccion, no en el evento")
        else:
            failures.append(f"no se detecto ningun pico cerca de {event:.0f}s")

    print("\n2. Bonus de combo con audio + chat simultaneos:")
    audio_only = compute_signals(duration=DURATION, audio_path=None, messages=None)
    print(f"   sin chat -> chat_available={audio_only.chat_available} (solo audio, W_AUDIO=1)")
    fake = compute_signals(duration=DURATION, audio_path=None, messages=synthetic_chat())
    fake.audio_available = True
    idx = fake.index_at(EVENTS[0])
    fake.z_audio = np.zeros(fake.n_bins)
    fake.z_audio[idx - 2 : idx + 3] = 4.0
    score2, combo2, _ = fuse(fake)
    if combo2[idx] and score2[idx] > score[idx]:
        print(f"   combo activo en el evento: score {score[idx]:.2f} -> {score2[idx]:.2f}  OK")
    else:
        failures.append("el bonus de combo no se aplico con audio y chat simultaneos")

    print("\n3. Refinado de bordes con los timestamps de palabra:")
    words = [Word(text="palabra", start=t, end=t + 0.4) for t in np.arange(370.0, 420.0, 0.5)]
    words = [w for w in words if not (376.0 < w.start < 379.0)]  # silencio artificial
    bounds = refine_bounds(words, 400.0, 375.0, 415.0, DURATION)
    length = bounds.t_end - bounds.t_start
    print(f"   ventana refinada [{bounds.t_start:.2f}, {bounds.t_end:.2f}]  duracion={length:.2f}s")
    if not (settings.min_clip_s - 0.01 <= length <= settings.max_clip_s + 0.01):
        failures.append(f"la duracion {length:.2f}s se sale de [MIN_CLIP_S, MAX_CLIP_S]")
    else:
        print("   dentro de [MIN_CLIP_S, MAX_CLIP_S]  OK")

    print()
    if failures:
        for f in failures:
            print(f"FALLO: {f}")
        return 1
    print("Todas las comprobaciones OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
