#!/usr/bin/env python
"""Verificacion de las fases 3-5: imprime los picos de audio/chat y los candidatos.

Uso (desde la raiz del repo, con el venv del backend):
    backend/.venv/bin/python scripts/inspect_signals.py https://www.twitch.tv/videos/123456
    backend/.venv/bin/python scripts/inspect_signals.py --no-chat <url>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(ROOT))

from app.pipeline.candidates import fuse, select_candidates  # noqa: E402
from app.pipeline.chat import ChatUnavailable, fetch_chat  # noqa: E402
from app.pipeline.ingest import download_audio, probe, resolve_url  # noqa: E402
from app.pipeline.signals import compute_signals  # noqa: E402
from app.utils import hhmmss  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--no-chat", action="store_true", help="ignora el chat")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    resolved = resolve_url(args.url)
    info = await probe(resolved)
    print(f"{info.platform}:{info.ext_id}  {info.title}  ({hhmmss(info.duration)})")

    audio = await download_audio(
        info, progress=lambda p, m: asyncio.sleep(0, print(f"\r  {m}", end="", flush=True))
    )
    print()

    messages = []
    if not args.no_chat:
        try:
            messages = await fetch_chat(
                info.platform, info.ext_id, info.url, info.duration
            )
            print(f"chat: {len(messages)} mensajes")
        except ChatUnavailable as exc:
            print(f"chat no disponible: {exc}")

    signals = compute_signals(
        duration=info.duration, audio_path=audio, messages=messages or None
    )
    print(
        f"bins={signals.n_bins} audio={signals.audio_available} chat={signals.chat_available}"
    )

    score, combo = fuse(signals)
    order = sorted(range(signals.n_bins), key=lambda i: -score[i])[: args.top]
    print(f"\nTop {args.top} picos de score fusionado:")
    for i in order:
        print(
            f"  {hhmmss(signals.bin_time(i)):>9}  score={score[i]:6.2f} "
            f"z_audio={signals.z_audio[i]:5.2f} z_chat={signals.z_chat[i]:5.2f} "
            f"msgs={int(signals.msg_count[i]):3d} combo={'si' if combo[i] else 'no'}"
        )

    cands = select_candidates(signals)
    print(f"\n{len(cands)} candidatos tras NMS (MIN_GAP_S):")
    for c in cands:
        print(
            f"  {hhmmss(c.t_peak):>9}  [{hhmmss(c.t_start)} - {hhmmss(c.t_end)}] "
            f"score={c.signal_score:6.2f} msgs={c.msg_count:3d} users={c.unique_users:3d} "
            f"combo={'si' if c.combo else 'no'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
