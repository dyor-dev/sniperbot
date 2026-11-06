"""Counters and latency samples.

Hot path rule: only integer increments and a bounded deque append here.
Percentiles are computed lazily, when `.stats` is typed - never per post.
"""

from __future__ import annotations

import time
from collections import deque

SAMPLE_LIMIT = 1024


class Metrics:
    __slots__ = (
        "updates",
        "posts",
        "comments",
        "failures",
        "flood_waits",
        "skipped",
        "started",
        "reaction_ms",
        "local_us",
        "per_channel",
    )

    def __init__(self) -> None:
        self.updates = 0
        self.posts = 0
        self.comments = 0
        self.failures = 0
        self.flood_waits = 0
        self.skipped: dict[str, int] = {}
        self.started = time.monotonic()
        self.reaction_ms: deque[float] = deque(maxlen=SAMPLE_LIMIT)
        self.local_us: deque[float] = deque(maxlen=SAMPLE_LIMIT)
        self.per_channel: dict[int, list[float]] = {}

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def ok(self, channel_id: int, reaction_ms: float, local_us: float) -> None:
        self.comments += 1
        self.reaction_ms.append(reaction_ms)
        self.local_us.append(local_us)
        row = self.per_channel.get(channel_id)
        if row is None:
            # [count, last_reaction_ms]
            self.per_channel[channel_id] = [1.0, reaction_ms]
        else:
            row[0] += 1
            row[1] = reaction_ms

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    last = len(sorted_values) - 1
    index = int(round((p / 100.0) * last))
    if index < 0:
        index = 0
    elif index > last:
        index = last
    return sorted_values[index]


def summarize(samples: deque[float] | list[float]) -> dict[str, float]:
    """min / p50 / p95 / p99 / max / mean for a sample collection."""
    if not samples:
        return {"n": 0, "min": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    ordered = sorted(samples)
    total = sum(ordered)
    return {
        "n": len(ordered),
        "min": ordered[0],
        "p50": percentile(ordered, 50),
        "p95": percentile(ordered, 95),
        "p99": percentile(ordered, 99),
        "max": ordered[-1],
        "mean": total / len(ordered),
    }
