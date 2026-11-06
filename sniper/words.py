"""Comment text handling - pure functions, no I/O."""

from __future__ import annotations

SEPARATOR = "|"


def parse_words(raw: str) -> tuple[str, ...]:
    """'a|b | c|' -> ('a', 'b', 'c'), order preserved, duplicates dropped."""
    if not raw:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(SEPARATOR):
        text = part.strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def pick(words: tuple[str, ...] | list[str], cursor: int) -> tuple[str, int]:
    """Round-robin over the variants without ever repeating back-to-back.

    Returns (text, next_cursor). Pure and allocation-cheap: this is the only
    word logic on the hot path.
    """
    count = len(words)
    if count == 0:
        raise ValueError("no comment words configured")
    if count == 1:
        return words[0], 0
    index = cursor % count
    return words[index], (index + 1) % count
