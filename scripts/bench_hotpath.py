#!/usr/bin/env python3
"""Offline microbrenchmark: per-update CPU cost of the hot path (no network).

    .venv/bin/python scripts/bench_hotpath.py            # 200k iteratsiya
    .venv/bin/python scripts/bench_hotpath.py 1000000

Bu skript tarmoqni o'lchamaydi - u faqat KODNING o'z ishini (klassifikatsiya,
matn tanlash, MTProto so'rov obyektini yig'ish) mikrosekundlarda o'lchaydi.
Haqiqiy kechikish uchun: python -m sniper --probe 15  va  .ping
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.tl import types  # noqa: E402
from telethon.tl.functions.messages import SendMessageRequest  # noqa: E402

from sniper.classify import classify  # noqa: E402
from sniper.words import pick  # noqa: E402


def make_update(channel_id: int = 111, group_id: int = 222, post_id: int = 777):
    """Sintetik 'discussion group auto-forward' update."""
    fwd = SimpleNamespace(
        channel_post=post_id, from_id=SimpleNamespace(channel_id=channel_id)
    )
    message = SimpleNamespace(
        peer_id=SimpleNamespace(channel_id=group_id),
        post=False,
        fwd_from=fwd,
        message="yangi post matni",
        out=False,
        id=999,
        action=None,
    )
    return SimpleNamespace(message=message)


def bench(label: str, func, iterations: int) -> None:
    samples = [0] * iterations
    started = time.perf_counter_ns()
    for index in range(iterations):
        before = time.perf_counter_ns()
        func()
        samples[index] = time.perf_counter_ns() - before
    total = time.perf_counter_ns() - started
    samples.sort()
    print(
        f"{label:<34} mean {total / iterations / 1000:7.2f} µs | "
        f"p50 {samples[iterations // 2] / 1000:6.2f} µs | "
        f"p99 {samples[int(iterations * 0.99)] / 1000:6.2f} µs"
    )


def main() -> None:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000

    channels = {
        111: SimpleNamespace(channel_id=111, group_id=222, group_hash=1)
    }
    groups = {222: channels[111]}
    update = make_update()
    words = ("birinchi!", "salom", "zo'r post")
    reply = types.InputReplyToMessage(reply_to_msg_id=999)
    peer = types.InputPeerChannel(222, 1)

    print(f"Comment Sniper - kod ishi (iteratsiya: {iterations:,})\n".replace(",", " "))
    bench(
        "classify() (har update uchun)",
        lambda: classify(update, channels, groups, 1, frozenset()),
        iterations,
    )
    bench("pick() matn tanlash", lambda: pick(words, 0), iterations)
    bench(
        "SendMessageRequest yig'ish",
        lambda: SendMessageRequest(
            peer=peer,
            message="birinchi!",
            random_id=123456789,
            reply_to=reply,
            no_webpage=True,
        ),
        iterations,
    )


if __name__ == "__main__":
    main()
