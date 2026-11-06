"""The hot path: one raw update in, one messages.sendMessage out.

Design rules that keep the reaction time network-bound instead of CPU-bound:

1. No entity resolution here. Channel/group ids AND access hashes are cached
   in state, so a comment never needs get_entity / getFullChannel.
2. No mapping RPC when avoidable. The discussion-group auto-forward update
   already contains the reply target id, so we skip messages.getDiscussionMessage
   entirely (~1 extra round trip saved on every post).
3. No logging, no string formatting, no stats aggregation, no locks: integers,
   dict lookups, one request object.
4. Dedupe by (channel, post) so the channel update and the group update for the
   same post can never produce two comments.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from telethon import errors
from telethon.tl import types
from telethon.tl.functions.messages import GetDiscussionMessageRequest, SendMessageRequest

from .classify import CommandHit, PostHit, classify
from .config import Settings
from .metrics import Metrics
from .ratelimit import Gate
from .store import Store
from .words import pick

log = logging.getLogger("sniper.hot")

DONE_TTL_NS = 300 * 1_000_000_000  # forget a handled post after 5 minutes
DONE_MAX = 8192


class Sniper:
    __slots__ = (
        "client",
        "settings",
        "store",
        "metrics",
        "self_id",
        "commands",
        "gate",
        "_done",
        "frozen_until",
    )

    def __init__(
        self,
        client: Any,
        settings: Settings,
        store: Store,
        metrics: Metrics,
        self_id: int,
        gate: Gate | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.metrics = metrics
        self.self_id = self_id
        self.commands: Any = None
        self.gate = gate or Gate(settings.min_interval, settings.max_per_minute)
        self._done: dict[tuple[int, int], int] = {}
        self.frozen_until = 0.0

    # ------------------------------------------------------------- entry point
    async def on_update(self, update: Any) -> None:
        """Registered for UpdateNewChannelMessage + UpdateNewMessage."""
        started = time.monotonic_ns()
        self.metrics.updates += 1

        hit = classify(
            update,
            self.store.by_channel,
            self.store.by_group,
            self.self_id,
            self.settings.command_chat_ids,
        )
        if hit is None:
            return
        if hit.__class__ is CommandHit:
            await self.commands.dispatch(hit)
            return
        await self._comment(hit, started)

    # -------------------------------------------------------------- the sniper
    async def _comment(self, hit: PostHit, started: int) -> None:
        metrics = self.metrics
        rec = self.store.by_channel.get(hit.channel_id)
        if rec is None:  # channel removed between update and handling
            return

        metrics.posts += 1

        done_key = (hit.channel_id, hit.post_id)
        if done_key in self._done:
            return
        self._done[done_key] = started
        if len(self._done) > DONE_MAX:
            self._prune(started)

        state = self.store.state
        if not state.enabled:
            metrics.skip("paused")
            return
        if not state.words:
            metrics.skip("no_words")
            return

        now = time.monotonic()
        if now < self.frozen_until:
            metrics.skip("flood")
            return

        allowed, reason = self.gate.allow(hit.channel_id, now)
        if not allowed:
            metrics.skip(reason)
            return

        # Fast path: the group update carried the reply target itself.
        # group_id must match the cached one, otherwise a channel that swapped
        # its discussion group would send us out with a stale access_hash.
        group_peer = None
        reply_to = 0
        if hit.kind == "discussion" and rec.group_hash and hit.group_id == rec.group_id:
            group_peer = types.InputPeerChannel(hit.group_id, rec.group_hash)
            reply_to = hit.reply_to_id

        if group_peer is None or not reply_to:
            group_peer, reply_to = await self._resolve(rec, hit.post_id)
            if group_peer is None or not reply_to:
                metrics.skip("unresolved")
                rec.last_error = rec.last_error or "muhokama guruhi aniqlanmadi"
                return

        text, cursor = pick(state.words, state.cursor)
        state.cursor = cursor

        request = SendMessageRequest(
            peer=group_peer,
            message=text,
            random_id=random.getrandbits(63),
            reply_to=types.InputReplyToMessage(reply_to_msg_id=reply_to),
            no_webpage=self.settings.no_webpage,
        )

        prepared = time.monotonic_ns()
        try:
            await self.client(request)
        except (errors.FloodWaitError, errors.SlowModeWaitError) as exc:
            # NOTE: in telethon 1.45 SlowModeWaitError is NOT a FloodWaitError
            # subclass, so both must be listed explicitly.
            metrics.flood_waits += 1
            seconds = float(getattr(exc, "seconds", 0) or 0)
            if isinstance(exc, errors.SlowModeWaitError):
                metrics.skip("slow_mode")
                self.frozen_until = max(self.frozen_until, now + seconds)
                rec.last_error = f"slow mode {int(seconds)}s"
                log.warning("sekin rejim: %s guruhi %ss kutish", rec.key, int(seconds))
            else:
                metrics.skip("flood")
                rec.last_error = f"flood {int(seconds)}s"
                self.frozen_until = now + seconds + 1.0
                log.warning(
                    "FLOOD_WAIT %ss (%s) - kommentlash %ss to'xtatildi",
                    int(seconds),
                    rec.key,
                    int(seconds) + 1,
                )
            return
        except errors.RPCError as exc:
            metrics.failures += 1
            rec.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
            log.warning("komment yuborilmadi (%s#%s): %s", rec.key, hit.post_id, exc)
            return
        except Exception as exc:  # network hiccup, never kill the handler
            metrics.failures += 1
            rec.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
            log.exception("komment yuborishda kutilmagan xato (%s)", rec.key)
            return

        finished = time.monotonic_ns()
        reaction_ms = (finished - started) / 1_000_000.0
        metrics.ok(hit.channel_id, reaction_ms, (prepared - started) / 1000.0)
        rec.comments += 1
        rec.last_ms = reaction_ms
        rec.last_error = ""

    # ------------------------------------------------------------- slow helpers
    async def _resolve(self, rec: Any, post_id: int):
        """Post id -> (InputPeerChannel, discussion message id). One RPC."""
        try:
            updates = await self.client(
                GetDiscussionMessageRequest(
                    peer=types.InputPeerChannel(rec.channel_id, rec.channel_hash),
                    msg_id=post_id,
                )
            )
        except errors.RPCError as exc:
            log.warning("discussion topilmadi (%s#%s): %s", rec.key, post_id, exc)
            rec.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
            return None, 0

        chats = {chat.id: chat for chat in (getattr(updates, "chats", None) or ())}
        for message in getattr(updates, "messages", None) or ():
            group_id = getattr(message.peer_id, "channel_id", 0)
            if not group_id:
                continue
            chat = chats.get(group_id)
            if chat is None:
                continue
            # Cache what we just learned: the next post needs no RPC at all.
            rec.group_id = group_id
            rec.group_hash = chat.access_hash
            self.store.reindex()
            rec.last_error = ""
            return types.InputPeerChannel(group_id, chat.access_hash), message.id
        rec.last_error = "guruh xabari topilmadi (kommentlar o'chirilgan?)"
        return None, 0

    def _prune(self, now_ns: int) -> None:
        horizon = now_ns - DONE_TTL_NS
        stale = [key for key, stamp in self._done.items() if stamp < horizon]
        for key in stale:
            del self._done[key]

    async def warmup(self) -> None:
        """Warm Telethon's entity cache so the first post costs no resolution."""
        for rec in list(self.store.by_channel.values()):
            try:
                entity = await self.client.get_entity(
                    types.InputPeerChannel(rec.channel_id, rec.channel_hash)
                )
                rec.channel_hash = getattr(entity, "access_hash", 0) or rec.channel_hash
                rec.title = getattr(entity, "title", "") or rec.title
            except Exception as exc:
                log.warning("warmup: @%s kanali ochilmadi: %s", rec.key, exc)
            if rec.group_id and rec.group_hash:
                try:
                    await self.client.get_entity(
                        types.InputPeerChannel(rec.group_id, rec.group_hash)
                    )
                except Exception as exc:
                    log.warning("warmup: guruh %s ochilmadi: %s", rec.group_id, exc)
        self.store.reindex()
