"""Configuration: a tiny .env reader plus typed settings.

Deliberately dependency-free (no python-dotenv) so that the hot path stays
lean and there is one less thing to install on the server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines. Real environment variables win over the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "da")


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _integer(name: str, default: int) -> int:
    return int(_number(name, float(default)))


def _chat_ids(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "")
    ids: set[int] = set()
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if part and part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


@dataclass(slots=True)
class Settings:
    api_id: int
    api_hash: str
    session_string: str
    session_file: Path
    state_file: Path
    words: tuple[str, ...]
    min_interval: float
    max_per_minute: int
    auto_join: bool
    no_webpage: bool
    command_chat_ids: frozenset[int]
    log_level: str
    gc_disable: bool
    connection_timeout: float = 5.0

    @property
    def uses_string_session(self) -> bool:
        return bool(self.session_string)


def load_settings(env_file: Path | None = None) -> Settings:
    load_env_file(env_file or PROJECT_ROOT / ".env")

    api_id_raw = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    if not api_id_raw.isdigit() or int(api_id_raw) <= 0:
        raise SystemExit(
            "API_ID yo'q yoki noto'g'ri.\n"
            "my.telegram.org -> API development tools bo'limidan oling va .env ichiga yozing."
        )
    if len(api_hash) < 32:
        raise SystemExit(
            "API_HASH yo'q yoki noto'g'ri (32 belgidan iborat bo'lishi kerak).\n"
            "my.telegram.org -> API development tools."
        )

    session_string = os.environ.get("TELEGRAM_SESSION", "").strip()
    session_file = Path(
        os.environ.get("SESSION_FILE", str(PROJECT_ROOT / "sniper.session"))
    ).expanduser()
    state_file = Path(
        os.environ.get("STATE_FILE", str(PROJECT_ROOT / "state.json"))
    ).expanduser()

    from .words import parse_words  # local import: keeps config import cheap

    words = parse_words(os.environ.get("WORDS", ""))

    return Settings(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        session_string=session_string,
        session_file=session_file,
        state_file=state_file,
        words=words,
        min_interval=max(0.0, _number("MIN_INTERVAL", 0.0)),
        max_per_minute=max(0, _integer("MAX_PER_MINUTE", 20)),
        auto_join=_flag("AUTO_JOIN", True),
        no_webpage=_flag("NO_WEBPAGE", True),
        command_chat_ids=_chat_ids("COMMAND_CHAT_IDS"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        gc_disable=_flag("GC_DISABLE", False),
    )
