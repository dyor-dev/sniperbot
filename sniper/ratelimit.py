"""Per-channel and global send budget.

This is the only deliberate brake in the system: Telegram punishes bursty
sending with FLOOD_WAIT (and eventually bans). Two knobs, both optional:

  * min_interval  - seconds between two comments in the SAME channel
  * max_per_minute - comments per minute across all channels

Both default to "no artificial delay" (0) because latency is the whole point;
the flood handler in hotpath.py then reacts when Telegram pushes back.
"""

from __future__ import annotations

import time


class Gate:
    __slots__ = (
        "min_interval",
        "max_per_minute",
        "_clock",
        "_last",
        "_window_start",
        "_window_count",
    )

    def __init__(
        self,
        min_interval: float = 0.0,
        max_per_minute: int = 0,
        clock=time.monotonic,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.max_per_minute = max(0, int(max_per_minute))
        self._clock = clock
        self._last: dict[int, float] = {}
        self._window_start = 0.0
        self._window_count = 0

    def allow(self, key: int, now: float | None = None) -> tuple[bool, str]:
        """True if a comment may be sent now. Also books the slot."""
        moment = self._clock() if now is None else now

        if self.min_interval > 0.0:
            previous = self._last.get(key)
            if previous is not None and moment - previous < self.min_interval:
                return False, "rate_channel"

        if self.max_per_minute > 0:
            if moment - self._window_start >= 60.0:
                self._window_start = moment
                self._window_count = 0
            if self._window_count >= self.max_per_minute:
                return False, "rate_global"
            self._window_count += 1

        self._last[key] = moment
        return True, ""

    def freeze_until(self) -> float:
        return self._window_start
