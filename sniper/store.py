"""Persistent state: watched channels, comment texts, on/off flag.

Atomic writes (tmp + os.replace) and only on command - never on the hot path.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Channel:
    key: str
    title: str = ""
    channel_id: int = 0
    channel_hash: int = 0
    group_id: int = 0
    group_hash: int = 0
    comments: int = 0
    last_ms: float = 0.0


@dataclass
class State:
    enabled: bool = True
    cursor: int = 0
    words: list[str] = field(default_factory=list)
    channels: dict[str, Channel] = field(default_factory=dict)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state = State()
        self.by_channel: dict[int, Channel] = {}
        self.by_group: dict[int, Channel] = {}

    # ---------------------------------------------------------------- loading
    def load(self) -> None:
        if not self.path.is_file():
            self.reindex()
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.reindex()
            return

        state = State()
        state.enabled = bool(payload.get("enabled", True))
        state.cursor = int(payload.get("cursor", 0) or 0)
        state.words = [str(word) for word in payload.get("words") or []]
        known = set(Channel.__dataclass_fields__)
        for key, raw in (payload.get("channels") or {}).items():
            if not isinstance(raw, dict):
                continue
            try:
                state.channels[str(key)] = Channel(**{k: v for k, v in raw.items() if k in known})
            except TypeError:
                continue
        self.state = state
        self.reindex()

    # --------------------------------------------------------------- indexing
    def reindex(self) -> None:
        """Rebuild the two lookup dicts the hot path reads."""
        self.by_channel = {
            rec.channel_id: rec for rec in self.state.channels.values() if rec.channel_id
        }
        self.by_group = {
            rec.group_id: rec for rec in self.state.channels.values() if rec.group_id
        }

    # ----------------------------------------------------------------- saving
    def save(self) -> None:
        payload = {
            "version": 1,
            "enabled": self.state.enabled,
            "cursor": self.state.cursor,
            "words": self.state.words,
            "channels": {key: asdict(rec) for key, rec in self.state.channels.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic on POSIX

    # ------------------------------------------------------------- mutators
    def set_words(self, words: tuple[str, ...] | list[str]) -> None:
        self.state.words = list(words)
        self.state.cursor = 0

    def add(self, rec: Channel) -> None:
        self.state.channels[rec.key] = rec
        self.reindex()

    def remove(self, key: str) -> Channel | None:
        rec = self.state.channels.pop(key, None)
        self.reindex()
        return rec

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(self.state.words)
