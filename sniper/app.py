"""Process wiring: client, handlers, socket tuning, background upkeep."""

from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import socket as socket_module
import sys
import time
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon.network.connection import ConnectionTcpAbridged
from telethon.sessions import StringSession
from telethon.tl.types import UpdateNewChannelMessage, UpdateNewMessage

from . import __version__
from .commands import Commands
from .config import Settings, load_settings
from .hotpath import Sniper
from .metrics import Metrics, summarize
from .store import Store

log = logging.getLogger("sniper")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)


def select_event_loop() -> str:
    try:
        import uvloop  # type: ignore[import-not-found]
    except Exception:
        return "asyncio"
    try:
        uvloop.install()
        return "uvloop"
    except Exception:
        return "asyncio"


def build_client(settings: Settings) -> TelegramClient:
    session = (
        StringSession(settings.session_string)
        if settings.uses_string_session
        else str(settings.session_file)
    )
    return TelegramClient(
        session,
        settings.api_id,
        settings.api_hash,
        connection=ConnectionTcpAbridged,  # smallest framing, no TLS/HTTP layer
        use_ipv6=False,
        timeout=settings.connection_timeout,
        request_retries=1,  # fail fast, never retry a comment silently
        connection_retries=None,  # but reconnect forever
        retry_delay=1,
        auto_reconnect=True,
        sequential_updates=False,  # never serialize update handling
        flood_sleep_threshold=0,  # do not auto-sleep; we handle it ourselves
        catch_up=False,  # no backlog replay on start: we want "now", not history
        receive_updates=True,
        device_model="CommentSniper",
        system_version="Linux",
        app_version=__version__,
        lang_code="en",
    )


def tune_socket(client: TelegramClient) -> bool:
    """TCP_NODELAY / keepalive / QUICKACK on the live MTProto socket."""
    try:
        connection = client._sender._connection  # noqa: SLF001 - no public API for this
        reader = getattr(connection, "_reader", None)
        transport = getattr(reader, "_transport", None)
        if transport is None:
            return False
        sock = transport.get_extra_info("socket")
        if sock is None:
            return False
        sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1)
        sock.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_KEEPALIVE, 1)
        for name, value in (
            ("TCP_KEEPIDLE", 30),
            ("TCP_KEEPINTVL", 10),
            ("TCP_KEEPCNT", 3),
            ("TCP_QUICKACK", 1),
        ):
            option = getattr(socket_module, name, None)
            if option is None:
                continue
            try:
                sock.setsockopt(socket_module.IPPROTO_TCP, option, value)
            except OSError:
                pass
        return True
    except Exception:
        return False


async def socket_keeper(client: TelegramClient) -> None:
    """Re-apply socket options after every reconnect."""
    while True:
        try:
            await asyncio.sleep(30)
            tune_socket(client)
        except asyncio.CancelledError:
            return
        except Exception:
            continue


async def run(settings: Settings) -> None:
    store = Store(settings.state_file)
    store.load()
    if not store.state.words and settings.words:
        store.set_words(settings.words)
        store.save()

    metrics = Metrics()
    client = build_client(settings)
    keeper: asyncio.Task[None] | None = None
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit(
                "Session yaroqsiz yoki muddati tugagan.\n"
                "Yangi session string olish uchun serverda: python scripts/login.py"
            )

        me = await client.get_me()
        sniper = Sniper(client, settings, store, metrics, self_id=me.id)
        sniper.commands = Commands(client, settings, store, metrics, sniper)

        await sniper.warmup()

        client.add_event_handler(
            sniper.on_update,
            events.Raw(types=[UpdateNewChannelMessage, UpdateNewMessage]),
        )

        tuned = tune_socket(client)
        keeper = asyncio.create_task(socket_keeper(client))
        if settings.gc_disable:
            gc.disable()
        else:
            gc.freeze()  # everything loaded so far never gets scanned again
            gc.set_threshold(20000, 50, 50)

        dc = "?"
        try:
            config = await client(functions.help.GetConfigRequest())
            dc = getattr(config, "this_dc", "?")
        except Exception:
            pass

        log.info(
            "sniper %s tayyor | akkaunt %s (id %s) | DC %s | kanal %d | matn %d | "
            "tcp_tuned=%s | session=%s",
            __version__,
            getattr(me, "username", None) or "—",
            me.id,
            dc,
            len(store.state.channels),
            len(store.state.words),
            tuned,
            "string" if settings.uses_string_session else str(settings.session_file),
        )
        log.info("buyruqlar uchun Saved Messages'ga .help yuboring")

        await client.run_until_disconnected()
    finally:
        if keeper is not None:
            keeper.cancel()
        await client.disconnect()


async def probe(settings: Settings, samples: int) -> None:
    """Measure the real MTProto RTT to the Telegram DC handling this account."""
    client = build_client(settings)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Session yaroqsiz - avval python scripts/login.py")
        dc = "?"
        try:
            config = await client(functions.help.GetConfigRequest())
            dc = getattr(config, "this_dc", "?")
        except Exception:
            pass

        results: list[float] = []
        for _ in range(max(1, samples)):
            started = time.perf_counter_ns()
            await client(functions.help.GetNearestDcRequest())
            results.append((time.perf_counter_ns() - started) / 1_000_000.0)

        stats = summarize(results)
        print(f"MTProto RTT -> Telegram DC {dc}  ({stats['n']} namuna)")
        print(f"  min  {stats['min']:7.2f} ms")
        print(f"  p50  {stats['p50']:7.2f} ms")
        print(f"  p95  {stats['p95']:7.2f} ms")
        print(f"  max  {stats['max']:7.2f} ms")
        print(f"  mean {stats['mean']:7.2f} ms")
        print(f"  jitter ±{stats['max'] - stats['min']:.2f} ms")
        print("\nEslatma: kommentning umumiy kechikishi ~ shu RTT + Telegram serverining"
              " o'z ishlashi (odatda yana 3-20 ms).")
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sniper", description="Comment Sniper - Telegram kanal komment userbot"
    )
    parser.add_argument(
        "--probe",
        nargs="?",
        type=int,
        const=15,
        default=0,
        metavar="N",
        help="o'lchov rejimi: DC'gacha N marta RTT o'lchab chiqadi",
    )
    parser.add_argument("--env", default=None, help="boshqa .env fayli yo'li")
    args = parser.parse_args(argv)

    settings = load_settings(Path(args.env) if args.env else None)
    setup_logging(settings.log_level)
    loop_name = select_event_loop()
    log.info("event loop: %s | python %s", loop_name, sys.version.split()[0])

    if args.probe:
        asyncio.run(probe(settings, args.probe))
    else:
        asyncio.run(run(settings))
