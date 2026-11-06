"""Unit tests for the pure logic and the send path (no network, no session).

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import errors

from sniper.classify import (
    CommandHit,
    PostHit,
    classify,
    normalize_channel,
    peer_int_id,
)
from sniper.metrics import Metrics, summarize
from sniper.ratelimit import Gate
from sniper.store import Channel, Store
from sniper.words import parse_words, pick


# --------------------------------------------------------------------------- fakes
def fake_msg(
    *,
    channel_id: int = 0,
    user_id: int = 0,
    chat_id: int = 0,
    post: bool = False,
    fwd_channel: int = 0,
    fwd_post: int = 0,
    text: str = "",
    out: bool = False,
    msg_id: int = 999,
    action=None,
):
    fwd = None
    if fwd_channel or fwd_post:
        fwd = SimpleNamespace(
            channel_post=fwd_post,
            from_id=SimpleNamespace(channel_id=fwd_channel),
        )
    return SimpleNamespace(
        peer_id=SimpleNamespace(channel_id=channel_id, user_id=user_id, chat_id=chat_id),
        post=post,
        fwd_from=fwd,
        message=text,
        out=out,
        id=msg_id,
        action=action,
    )


def wrap(message):
    return SimpleNamespace(message=message)


def fake_settings(**overrides):
    base = dict(
        min_interval=0.0,
        max_per_minute=0,
        no_webpage=True,
        command_chat_ids=frozenset(),
        auto_join=False,
        words=("salom",),
        gc_disable=False,
        log_level="INFO",
        session_string="",
        session_file=Path("x.session"),
        state_file=Path("state.json"),
        api_id=1,
        api_hash="x" * 32,
        uses_string_session=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeClient:
    def __init__(self, error=None):
        self.requests = []
        self.messages = []
        self.error = error

    async def __call__(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace()

    async def send_message(self, entity, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(id=1)


# --------------------------------------------------------------------------- words
class TestWords(unittest.TestCase):
    def test_parse_words(self):
        self.assertEqual(parse_words("a|b | a|c|"), ("a", "b", "c"))
        self.assertEqual(parse_words(""), ())
        self.assertEqual(parse_words("   "), ())

    def test_rotation_never_repeats_back_to_back(self):
        words = ("a", "b", "c")
        cursor = 0
        seen = []
        for _ in range(9):
            text, cursor = pick(words, cursor)
            seen.append(text)
        self.assertEqual(seen, ["a", "b", "c"] * 3)

    def test_single_word_keeps_cursor(self):
        self.assertEqual(pick(("only",), 5), ("only", 0))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            pick((), 0)


# --------------------------------------------------------------------- normalize
class TestNormalize(unittest.TestCase):
    def test_forms(self):
        # username case is preserved (Telegram lookup is case-insensitive, but
        # the stored key should be exactly what the user typed)
        cases = {
            "@Foo": "Foo",
            "https://t.me/Foo": "Foo",
            "http://t.me/Foo?x=1": "Foo",
            "@lowercase": "lowercase",
            "lowercase": "lowercase",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_channel(raw), expected, raw)

    def test_rejects_invites_and_junk(self):
        for raw in ("", "   ", "https://t.me/+abc123", "t.me/joinchat/abc", "@a-b"):
            self.assertIsNone(normalize_channel(raw), raw)

    def test_peer_int_id(self):
        self.assertEqual(peer_int_id(SimpleNamespace(channel_id=123)), int("-100123"))
        self.assertEqual(peer_int_id(SimpleNamespace(chat_id=5)), -5)
        self.assertEqual(peer_int_id(SimpleNamespace(user_id=42)), 42)


# ---------------------------------------------------------------------- classify
class TestClassify(unittest.TestCase):
    def setUp(self):
        self.rec = SimpleNamespace(channel_id=111, group_id=222)
        self.channels = {111: self.rec}
        self.groups = {222: self.rec}

    def run_classify(self, message, self_id=1, chats=frozenset()):
        return classify(wrap(message), self.channels, self.groups, self_id, chats)

    def test_direct_channel_post(self):
        hit = self.run_classify(fake_msg(channel_id=111, post=True, msg_id=50))
        self.assertEqual(hit, PostHit("direct", 111, 0, 50, 0))

    def test_unwatched_channel_ignored(self):
        self.assertIsNone(self.run_classify(fake_msg(channel_id=999, post=True)))

    def test_discussion_fast_path(self):
        hit = self.run_classify(
            fake_msg(channel_id=222, fwd_channel=111, fwd_post=50, msg_id=777)
        )
        self.assertEqual(hit, PostHit("discussion", 111, 222, 50, 777))

    def test_discussion_from_other_channel_ignored(self):
        hit = self.run_classify(
            fake_msg(channel_id=222, fwd_channel=999, fwd_post=50, msg_id=777)
        )
        self.assertIsNone(hit)

    def test_group_message_without_forward_ignored(self):
        self.assertIsNone(self.run_classify(fake_msg(channel_id=222, text="oddiy xabar")))

    def test_service_message_ignored(self):
        hit = self.run_classify(
            fake_msg(channel_id=111, post=True, action=SimpleNamespace())
        )
        self.assertIsNone(hit)

    def test_command_in_saved_messages(self):
        hit = self.run_classify(fake_msg(user_id=1, text=".ping", out=True), self_id=1)
        self.assertIsInstance(hit, CommandHit)
        self.assertEqual(hit.text, ".ping")

    def test_command_from_foreign_chat_rejected(self):
        self.assertIsNone(self.run_classify(fake_msg(channel_id=333, text=".pause", out=True)))

    def test_command_requires_own_message(self):
        self.assertIsNone(
            self.run_classify(fake_msg(user_id=1, text=".pause", out=False), self_id=1)
        )

    def test_plain_text_is_not_command(self):
        self.assertIsNone(self.run_classify(fake_msg(user_id=1, text="salom", out=True)))

    def test_allowed_extra_chat(self):
        hit = self.run_classify(
            fake_msg(channel_id=444, text=".stats", out=True),
            chats=frozenset({int("-100444")}),
        )
        self.assertIsInstance(hit, CommandHit)


# -------------------------------------------------------------------------- gate
class TestGate(unittest.TestCase):
    def test_min_interval_per_channel(self):
        gate = Gate(min_interval=2.0, max_per_minute=0, clock=lambda: 0.0)
        self.assertEqual(gate.allow(111, now=100.0), (True, ""))
        self.assertEqual(gate.allow(111, now=101.0), (False, "rate_channel"))
        self.assertEqual(gate.allow(111, now=102.0), (True, ""))
        self.assertEqual(gate.allow(222, now=102.0), (True, ""))  # independent

    def test_global_cap(self):
        gate = Gate(min_interval=0.0, max_per_minute=2, clock=lambda: 0.0)
        self.assertTrue(gate.allow(1, now=10.0)[0])
        self.assertTrue(gate.allow(2, now=11.0)[0])
        self.assertEqual(gate.allow(3, now=12.0), (False, "rate_global"))
        self.assertTrue(gate.allow(4, now=71.0)[0])  # new window

    def test_unlimited_default(self):
        gate = Gate()
        for _ in range(500):
            self.assertTrue(gate.allow(1)[0])


# ---------------------------------------------------------------------- metrics
class TestMetrics(unittest.TestCase):
    def test_counters(self):
        metrics = Metrics()
        metrics.skip("paused")
        metrics.skip("paused")
        metrics.ok(1, 5.0, 120.0)
        metrics.ok(1, 7.0, 150.0)
        self.assertEqual(metrics.comments, 2)
        self.assertEqual(metrics.skipped["paused"], 2)
        self.assertEqual(metrics.per_channel[1][0], 2)
        self.assertEqual(metrics.per_channel[1][1], 7.0)

    def test_summarize(self):
        stats = summarize([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(stats["n"], 4)
        self.assertEqual(stats["min"], 1.0)
        self.assertEqual(stats["max"], 4.0)
        self.assertAlmostEqual(stats["mean"], 2.5)
        self.assertEqual(summarize([])["n"], 0)


# ------------------------------------------------------------------------ store
class TestStore(unittest.TestCase):
    def test_roundtrip_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = Store(path)
            store.set_words(("a", "b"))
            store.add(
                Channel(
                    key="kanal",
                    title="Kanal",
                    channel_id=111,
                    channel_hash=5,
                    group_id=222,
                    group_hash=6,
                )
            )
            store.state.enabled = False
            store.save()

            loaded = Store(path)
            loaded.load()
            self.assertEqual(loaded.words, ("a", "b"))
            self.assertFalse(loaded.state.enabled)
            rec = loaded.by_channel[111]
            self.assertEqual(rec.group_hash, 6)
            self.assertEqual(loaded.by_group[222].key, "kanal")
            self.assertIs(loaded.remove("kanal"), rec)
            self.assertEqual(loaded.by_channel, {})

    def test_corrupt_file_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            store = Store(path)
            store.load()
            self.assertEqual(store.state.channels, {})


# ------------------------------------------------------------------ send path
class TestCommentPath(unittest.TestCase):
    """Exercises Sniper._comment end to end against a fake client."""

    def build(self, error=None, enabled=True, words=("salom",)):
        from sniper.hotpath import Sniper

        store = Store(Path(tempfile.mkdtemp()) / "state.json")
        store.set_words(words)
        store.state.enabled = enabled
        store.add(
            Channel(
                key="kanal",
                channel_id=111,
                channel_hash=5,
                group_id=222,
                group_hash=6,
            )
        )
        client = FakeClient(error)
        metrics = Metrics()
        sniper = Sniper(client, fake_settings(), store, metrics, self_id=1)
        return sniper, client, metrics, store

    def run_hit(self, sniper, hit=None):
        hit = hit or PostHit("discussion", 111, 222, 50, 777)
        asyncio.run(sniper._comment(hit, time.monotonic_ns()))

    def test_comments_with_reply_to_post(self):
        sniper, client, metrics, store = self.build()
        self.run_hit(sniper)
        self.assertEqual(metrics.comments, 1)
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(request.peer.channel_id, 222)
        self.assertEqual(request.peer.access_hash, 6)
        self.assertEqual(request.message, "salom")
        self.assertEqual(request.reply_to.reply_to_msg_id, 777)
        self.assertTrue(request.no_webpage)
        self.assertIsNotNone(request.random_id)
        self.assertEqual(store.state.cursor, 0)

    def test_same_post_never_commented_twice(self):
        sniper, client, metrics, _ = self.build()
        self.run_hit(sniper)
        self.run_hit(sniper)
        self.assertEqual(metrics.comments, 1)
        self.assertEqual(len(client.requests), 1)

    def test_pause_blocks_but_counts_post(self):
        sniper, client, metrics, _ = self.build(enabled=False)
        self.run_hit(sniper)
        self.assertEqual(metrics.posts, 1)
        self.assertEqual(metrics.skipped["paused"], 1)
        self.assertEqual(client.requests, [])

    def test_no_words_skips(self):
        sniper, client, metrics, _ = self.build(words=())
        self.run_hit(sniper)
        self.assertEqual(metrics.skipped["no_words"], 1)
        self.assertEqual(client.requests, [])

    def test_generic_rpc_error_counted(self):
        sniper, _, metrics, _ = self.build(error=errors.RPCError(None, "INTERNAL"))
        self.run_hit(sniper)
        self.assertEqual(metrics.failures, 1)
        self.assertEqual(metrics.comments, 0)

    def test_flood_wait_freezes(self):
        sniper, _, metrics, _ = self.build(error=errors.FloodWaitError(None, capture=30))
        self.run_hit(sniper)
        self.assertEqual(metrics.flood_waits, 1)
        self.assertEqual(metrics.skipped.get("flood"), 1)
        self.assertGreater(sniper.frozen_until, time.monotonic())

    def test_slow_mode_counts_as_flood(self):
        sniper, _, metrics, _ = self.build(
            error=errors.SlowModeWaitError(None, capture=5)
        )
        self.run_hit(sniper)
        self.assertEqual(metrics.skipped.get("slow_mode"), 1)
        self.assertGreater(sniper.frozen_until, time.monotonic())

    def test_direct_hit_skips_when_resolution_fails(self):
        sniper, _, metrics, _ = self.build(error=errors.RPCError(None, "X"))
        self.run_hit(sniper, hit=PostHit("direct", 111, 0, 50, 0))
        self.assertEqual(metrics.skipped.get("unresolved"), 1)
        self.assertEqual(metrics.comments, 0)


# --------------------------------------------------------------------- commands
class TestCommands(unittest.TestCase):
    """The chat interface: every command must answer without touching network."""

    def build(self, words=("eski",)):
        from sniper.commands import Commands

        store = Store(Path(tempfile.mkdtemp()) / "state.json")
        store.set_words(words)
        client = FakeClient()
        commands = Commands(
            client, fake_settings(), store, Metrics(), SimpleNamespace(frozen_until=0.0)
        )
        return commands, client, store

    def send(self, commands, text):
        msg = SimpleNamespace(peer_id=SimpleNamespace(user_id=1), id=10)
        asyncio.run(commands.dispatch(CommandHit(msg, text)))
        return commands.client.messages[-1]

    def test_word_updates_without_restart(self):
        commands, _, store = self.build()
        reply = self.send(commands, ".word birinchi|ikkinchi|uchinchi")
        self.assertEqual(store.words, ("birinchi", "ikkinchi", "uchinchi"))
        self.assertIn("3 ta", reply)

    def test_word_without_argument_shows_usage(self):
        commands, _, store = self.build()
        reply = self.send(commands, ".word")
        self.assertIn("Foydalanish", reply)
        self.assertEqual(store.words, ("eski",))

    def test_pause_and_auto_toggle_flag(self):
        commands, _, store = self.build()
        self.assertIn("PAUSE", self.send(commands, ".pause"))
        self.assertFalse(store.state.enabled)
        self.assertIn("AUTO", self.send(commands, ".resume"))
        self.assertTrue(store.state.enabled)
        self.send(commands, ".pause")
        self.assertIn("AUTO", self.send(commands, ".auto"))

    def test_help_lists_every_command(self):
        commands, _, _ = self.build()
        reply = self.send(commands, ".help")
        for command in (
            ".ping",
            ".stats",
            ".info",
            ".word",
            ".auto",
            ".pause",
            ".channels",
            ".add",
            ".remove",
        ):
            self.assertIn(command, reply)

    def test_unknown_command_is_reported(self):
        commands, _, _ = self.build()
        self.assertIn("Noma'lum", self.send(commands, ".nonsense"))

    def test_info_and_channels_do_not_crash_on_empty_state(self):
        commands, _, _ = self.build()
        self.assertIn("INFO", self.send(commands, ".info"))
        self.assertIn("Kanallar yo'q", self.send(commands, ".channels"))

    def test_remove_missing_channel(self):
        commands, _, _ = self.build()
        self.assertIn("ro'yxatda yo'q", self.send(commands, ".remove @yoqkanal"))

    def test_add_rejects_invite_links(self):
        commands, _, _ = self.build()
        self.assertIn("Foydalanish", self.send(commands, ".add https://t.me/+secret"))


if __name__ == "__main__":
    unittest.main()
