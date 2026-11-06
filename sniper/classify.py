"""Update classification - the first thing that touches every MTProto packet.

This module imports nothing from telethon and builds no telethon objects, so it
is (a) unit-testable without a network or a session and (b) as cheap as a few
attribute lookups. It runs on every incoming update, including the thousands
that are irrelevant to us, therefore it must stay branch-light and alloc-light.

Returned values are immutable tuples - one small object per *interesting*
update, nothing at all for the rest.
"""

from __future__ import annotations

from typing import Any, NamedTuple

CMD_PREFIX = "."


class CommandHit(NamedTuple):
    """A `.command` typed by us, in Saved Messages or an allowed chat."""

    msg: Any
    text: str


class PostHit(NamedTuple):
    """A channel post worth commenting on.

    kind == "discussion": the linked group's auto-forward arrived first.
        Both the group peer and the reply target are already known, so the
        only network call left is messages.sendMessage itself.
    kind == "direct": we saw the channel post itself and still need to map
        post_id -> discussion message id (one extra RPC).
    """

    kind: str
    channel_id: int
    group_id: int
    post_id: int
    reply_to_id: int


def peer_int_id(peer: Any) -> int:
    """Telethon-style integer id for a Peer, without importing telethon."""
    channel_id = getattr(peer, "channel_id", 0)
    if channel_id:
        return int("-100%d" % channel_id)
    chat_id = getattr(peer, "chat_id", 0)
    if chat_id:
        return -chat_id
    return getattr(peer, "user_id", 0) or 0


def normalize_channel(raw: str) -> str | None:
    """'@Foo', 'https://t.me/Foo', 't.me/Foo/' -> 'Foo'. None if unusable."""
    text = (raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            lowered = text.lower()
            break
    for prefix in ("www.t.me/", "www.telegram.me/", "t.me/", "telegram.me/"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.split("?", 1)[0].split("/", 1)[0].strip().lstrip("@")
    if not text or text in ("joinchat", "c", "s", "addstickers"):
        return None
    if not all(ch.isalnum() or ch == "_" for ch in text):
        return None
    return text


def _is_command(msg: Any, self_id: int, command_chat_ids: frozenset[int]) -> bool:
    text = msg.message
    if not text or text[0] != CMD_PREFIX:
        return False
    if not getattr(msg, "out", False):
        return False
    peer = msg.peer_id
    if getattr(peer, "user_id", 0) == self_id:
        return True
    return bool(command_chat_ids) and peer_int_id(peer) in command_chat_ids


def classify(
    update: Any,
    channels: dict[int, Any],
    groups: dict[int, Any],
    self_id: int,
    command_chat_ids: frozenset[int],
) -> CommandHit | PostHit | None:
    """Route one raw update. Returns None for everything we do not care about."""
    msg = getattr(update, "message", None)
    if msg is None:
        return None

    # Service messages (joins, pins, edits of service data) are never posts.
    if getattr(msg, "action", None) is not None:
        return None

    peer = msg.peer_id
    channel_id = getattr(peer, "channel_id", 0)
    if channel_id:
        if getattr(msg, "post", False):
            # A post inside a watched channel -> we must resolve the group msg.
            if channel_id in channels:
                return PostHit("direct", channel_id, 0, msg.id, 0)
        elif channel_id in groups:
            # Auto-forward of a channel post into its discussion group.
            # Fast path: the update itself carries everything we need.
            fwd = msg.fwd_from
            if fwd is not None:
                post_id = getattr(fwd, "channel_post", 0) or 0
                if post_id:
                    from_id = fwd.from_id
                    source = getattr(from_id, "channel_id", 0) if from_id is not None else 0
                    if source and source in channels:
                        return PostHit("discussion", source, channel_id, post_id, msg.id)

    if _is_command(msg, self_id, command_chat_ids):
        return CommandHit(msg, msg.message)
    return None
