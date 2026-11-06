"""Chat command interface.

Every command is typed from your own account, so replies are sent back to the
same chat. All command work happens off the hot path: it may do RPCs.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from telethon import errors
from telethon.tl import functions, types

from .classify import CommandHit, normalize_channel
from .config import Settings
from .metrics import Metrics, summarize
from .store import Channel, Store
from .words import parse_words

log = logging.getLogger("sniper.cmd")

HELP = """COMMENT SNIPER - buyruqlar

.ping                - Telegram DC'gacha real RTT (ms)
.stats               - tezlik va faoliyat statistikasi
.info                - holat, kanallar, komment matnlari
.word a|b|c          - komment matnlarini yangilash (restart kerak emas)
.auto | .resume      - komment yozishni yoqish
.pause               - komment yozishni to'xtatish (kuzatish davom etadi)
.channels            - kuzatilayotgan kanallar
.add @kanal          - kanal + uning muhokama guruhini qo'shish
.remove @kanal       - kanalni ro'yxatdan chiqarish
.help                - shu ro'yxat"""


def _n(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


class Commands:
    __slots__ = ("client", "settings", "store", "metrics", "sniper")

    def __init__(
        self,
        client: Any,
        settings: Settings,
        store: Store,
        metrics: Metrics,
        sniper: Any,
    ) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.metrics = metrics
        self.sniper = sniper

    # ---------------------------------------------------------------- dispatch
    async def dispatch(self, hit: CommandHit) -> None:
        text = hit.text.strip()
        command, _, argument = text.partition(" ")
        handler = self._handlers().get(command.lower())
        if handler is None:
            await self._say(hit, "Noma'lum buyruq. .help ni yuboring.")
            return
        try:
            await handler(hit, argument.strip())
        except errors.RPCError as exc:
            await self._say(hit, f"Telegram xatosi: {exc.__class__.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - command layer must never die
            log.exception("buyruq bajarilmadi: %s", command)
            await self._say(hit, f"Xato: {exc.__class__.__name__}: {exc}")

    def _handlers(self) -> dict[str, Any]:
        return {
            ".ping": self._ping,
            ".stats": self._stats,
            ".info": self._info,
            ".word": self._word,
            ".auto": self._resume,
            ".resume": self._resume,
            ".pause": self._pause,
            ".channels": self._channels,
            ".add": self._add,
            ".remove": self._remove,
            ".help": self._help,
            ".start": self._help,
        }

    # ------------------------------------------------------------------- utils
    async def _say(self, hit: CommandHit, text: str) -> None:
        try:
            await self.client.send_message(
                hit.msg.peer_id,
                text,
                parse_mode=None,
                link_preview=False,
                reply_to=hit.msg.id,
            )
        except Exception:
            log.exception("javob yuborilmadi")

    # --------------------------------------------------------------- .ping
    async def _ping(self, hit: CommandHit, argument: str) -> None:
        samples: list[float] = []
        dc = "?"
        failures = 0
        for _ in range(7):
            started = time.perf_counter_ns()
            try:
                result = await self.client(functions.help.GetNearestDcRequest())
                dc = getattr(result, "nearest_dc", dc)
            except errors.RPCError:
                failures += 1
                continue
            samples.append((time.perf_counter_ns() - started) / 1_000_000.0)

        if not samples:
            await self._say(hit, "PING: javob yo'q (ulanish uzilgan?)")
            return
        stats = summarize(samples)
        jitter = stats["max"] - stats["min"]
        await self._say(
            hit,
            "PING - MTProto RTT (Telegram DC)\n"
            f"min  {stats['min']:.2f} ms\n"
            f"p50  {stats['p50']:.2f} ms\n"
            f"p95  {stats['p95']:.2f} ms\n"
            f"max  {stats['max']:.2f} ms\n"
            f"jitter ±{jitter:.2f} ms · namunalar {stats['n']}"
            + (f" · xatolar {failures}" if failures else "")
            + f"\nDC: {dc}",
        )

    # --------------------------------------------------------------- .stats
    async def _stats(self, hit: CommandHit, argument: str) -> None:
        metrics = self.metrics
        reaction = summarize(metrics.reaction_ms)
        local = summarize(metrics.local_us)
        uptime = metrics.uptime_seconds
        hours, rest = divmod(int(uptime), 3600)
        minutes, seconds = divmod(rest, 60)

        if reaction["n"]:
            reaction_block = (
                f"reaksiya (post -> server ACK)\n"
                f"  p50 {reaction['p50']:.2f} ms · p95 {reaction['p95']:.2f} ms · "
                f"max {reaction['max']:.2f} ms\n"
                f"kod ishi (RPC'gacha)\n"
                f"  o'rtacha {local['mean'] * 1000:.0f} µs · p95 {local['p95'] * 1000:.0f} µs · "
                f"max {local['max'] * 1000:.0f} µs\n"
            )
        else:
            reaction_block = "reaksiya: hali namuna yo'q\n"

        skipped = metrics.skipped
        skip_line = (
            ", ".join(f"{name} {_n(value)}" for name, value in sorted(skipped.items()))
            or "yo'q"
        )

        await self._say(
            hit,
            "STATS\n"
            f"ish vaqti {hours:02d}:{minutes:02d}:{seconds:02d}\n"
            f"kelgan update {_n(metrics.updates)} · post {_n(metrics.posts)} · "
            f"komment {_n(metrics.comments)}\n"
            f"xato {_n(metrics.failures)} · flood/slow-mode {_n(metrics.flood_waits)}\n"
            f"o'tkazib yuborilgan: {skip_line}\n"
            f"{reaction_block}"
            f"rejim: {'AUTO' if self.store.state.enabled else 'PAUSED'}",
        )

    # ---------------------------------------------------------------- .info
    async def _info(self, hit: CommandHit, argument: str) -> None:
        state = self.store.state
        words = state.words
        word_block = (
            "\n".join(f"  {index + 1}. {text}" for index, text in enumerate(words)) or "  (bo'sh)"
        )
        channels = list(state.channels.values())
        if channels:
            channel_block = "\n".join(
                f"  @{rec.key}"
                + (f" -> guruh {rec.group_id}" if rec.group_id else " -> guruh YO'Q")
                + (f" · {_n(rec.comments)} komment" if rec.comments else "")
                for rec in channels
            )
        else:
            channel_block = "  (bo'sh - .add @kanal)"

        await self._say(
            hit,
            "INFO\n"
            f"rejim: {'AUTO (komment yoziladi)' if state.enabled else 'PAUSED'}\n"
            f"kanallar: {len(channels)} · matnlar: {len(words)}\n"
            f"min interval: {self.settings.min_interval:.2f}s · "
            f"limit: {self.settings.max_per_minute or 'cheklovsiz'}/daq\n"
            f"kutish (flood): "
            f"{max(0.0, self.sniper.frozen_until - time.monotonic()):.1f}s\n"
            f"\nkomment matnlari:\n{word_block}\n\nkanallar:\n{channel_block}",
        )

    # ---------------------------------------------------------------- .word
    async def _word(self, hit: CommandHit, argument: str) -> None:
        words = parse_words(argument)
        if not words:
            await self._say(
                hit,
                "Foydalanish: .word salom|birinchi!|yana bir variant\n"
                "Variantlar | bilan ajratiladi, navbat bilan ishlatiladi.",
            )
            return
        self.store.set_words(words)
        self.store.save()
        listing = "\n".join(f"  {index + 1}. {text}" for index, text in enumerate(words))
        await self._say(hit, f"Matnlar yangilandi ({len(words)} ta):\n{listing}")

    # ----------------------------------------------------------- .auto/.pause
    async def _resume(self, hit: CommandHit, argument: str) -> None:
        self.store.state.enabled = True
        self.store.save()
        await self._say(hit, "AUTO: komment yozish yoqildi.")

    async def _pause(self, hit: CommandHit, argument: str) -> None:
        self.store.state.enabled = False
        self.store.save()
        await self._say(hit, "PAUSE: komment yozish to'xtatildi (kuzatish davom etadi).")

    # ------------------------------------------------------------- .channels
    async def _channels(self, hit: CommandHit, argument: str) -> None:
        channels = list(self.store.state.channels.values())
        if not channels:
            await self._say(hit, "Kanallar yo'q. .add @kanal")
            return
        lines = []
        for rec in channels:
            group = f"guruh {rec.group_id}" if rec.group_id else "guruh YO'Q"
            last = f" · oxirgi {rec.last_ms:.2f} ms" if rec.last_ms else ""
            lines.append(
                f"@{rec.key} · {rec.title or '—'} · {group} · "
                f"{_n(rec.comments)} komment{last}"
            )
        await self._say(hit, "KANALLAR\n" + "\n".join(lines))

    # ---------------------------------------------------------------- .add
    async def _add(self, hit: CommandHit, argument: str) -> None:
        key = normalize_channel(argument)
        if not key:
            await self._say(
                hit,
                "Foydalanish: .add @kanal\n"
                "Faqat public kanal username'i (yopiq invite havolalar emas).",
            )
            return
        if key in self.store.state.channels:
            await self._say(hit, f"@{key} allaqachon ro'yxatda.")
            return

        try:
            entity = await self.client.get_entity(key)
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            await self._say(hit, f"@{key} topilmadi.")
            return
        except errors.RPCError as exc:
            await self._say(hit, f"@{key} ochilmadi: {exc.__class__.__name__}")
            return

        if not getattr(entity, "broadcast", False):
            await self._say(hit, f"@{key} kanal emas (guruh yoki bot). Kanal username'i kerak.")
            return

        # InputChannel for channel= fields, InputPeerChannel for peer= fields:
        # they are distinct TL types and Telegram expects the right one.
        channel_input = types.InputChannel(entity.id, entity.access_hash)
        channel_peer = types.InputPeerChannel(entity.id, entity.access_hash)
        notes: list[str] = []

        full = await self.client(
            functions.channels.GetFullChannelRequest(channel=channel_input)
        )
        linked_id = int(getattr(full.full_chat, "linked_chat_id", 0) or 0)

        if not await self._ensure_participant(channel_input, notes, "kanal"):
            await self._say(
                hit,
                f"@{key} kanaliga obuna bo'lish kerak: Telegram yangilanishlarni faqat "
                "a'zolarga yuboradi. AUTO_JOIN=0 turibdi.",
            )
            return

        group_id = 0
        group_hash = 0
        group_title = ""
        if linked_id:
            group_id, group_hash, group_title = await self._find_group(
                channel_peer, linked_id
            )
            if not group_id:
                notes.append("muhokama guruhi topilmadi (guruhga a'zo bo'ling)")
            elif not await self._ensure_participant(
                types.InputChannel(group_id, group_hash), notes, "guruh"
            ):
                notes.append("guruhga a'zo bo'lmadim - yangilanish kelmaydi")

        rec = Channel(
            key=key,
            title=getattr(entity, "title", "") or "",
            channel_id=entity.id,
            channel_hash=entity.access_hash,
            group_id=group_id,
            group_hash=group_hash,
        )
        self.store.add(rec)
        self.store.save()

        fast = "yoq (post paytida 1 qo'shimcha RPC)" if not group_id else "yoq"
        lines = [
            f"@{key} qo'shildi.",
            f"kanal id: {entity.id}",
            f"guruh: {group_id or '—'} {group_title}".strip(),
            f"rejim: {'AUTO' if self.store.state.enabled else 'PAUSED'}",
            f"tez yo'l: {'bor (0 qo\'shimcha RPC)' if group_id else fast}",
        ]
        if notes:
            lines.append("izoh: " + "; ".join(notes))
        await self._say(hit, "\n".join(lines))

    async def _remove(self, hit: CommandHit, argument: str) -> None:
        key = normalize_channel(argument)
        if not key:
            await self._say(hit, "Foydalanish: .remove @kanal")
            return
        rec = self.store.remove(key)
        if rec is None:
            await self._say(hit, f"@{key} ro'yxatda yo'q.")
            return
        self.store.save()
        await self._say(hit, f"@{key} ro'yxatdan chiqarildi.")

    async def _help(self, hit: CommandHit, argument: str) -> None:
        await self._say(hit, HELP)

    # ------------------------------------------------------------- membership
    async def _ensure_participant(
        self, channel_input: Any, notes: list[str], label: str
    ) -> bool:
        try:
            await self.client(
                functions.channels.GetParticipantRequest(
                    channel=channel_input, participant=types.InputPeerSelf()
                )
            )
            return True
        except errors.UserNotParticipantError:
            pass
        except errors.RPCError:
            return True  # cannot tell - assume yes rather than joining blindly

        if not self.settings.auto_join:
            return False
        try:
            await self.client(functions.channels.JoinChannelRequest(channel=channel_input))
            notes.append(f"{label}ga qo'shildim")
            return True
        except errors.RPCError as exc:
            notes.append(f"{label}ga qo'shilmadi: {exc.__class__.__name__}")
            return False

    async def _find_group(
        self, channel_peer: Any, linked_id: int
    ) -> tuple[int, int, str]:
        """(group_id, group_access_hash, title) for the linked discussion group."""
        try:
            entity = await self.client.get_entity(types.PeerChannel(linked_id))
            if getattr(entity, "access_hash", None):
                return (
                    entity.id,
                    entity.access_hash,
                    getattr(entity, "title", "") or "",
                )
        except Exception:
            pass

        # Fallback that always works: ask for the discussion message of the
        # newest post - its Updates payload contains the group with its hash.
        try:
            history = await self.client(
                functions.messages.GetHistoryRequest(
                    peer=channel_peer,
                    offset_id=0,
                    offset_date=None,
                    add_offset=0,
                    limit=1,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
            messages = getattr(history, "messages", None) or ()
            if not messages:
                return 0, 0, ""
            updates = await self.client(
                functions.messages.GetDiscussionMessageRequest(
                    peer=channel_peer, msg_id=messages[0].id
                )
            )
        except errors.RPCError:
            return 0, 0, ""

        for chat in getattr(updates, "chats", None) or ():
            if chat.id == linked_id:
                return (
                    chat.id,
                    chat.access_hash,
                    getattr(chat, "title", "") or "",
                )
        return 0, 0, ""
