#!/usr/bin/env python3
"""Yangi session string olish (interaktiv, serverda bir marta ishlatiladi).

    cd /opt/comment-sniper
    .venv/bin/python scripts/login.py

Telefon raqam, Telegram'dan kelgan kod va (bo'lsa) 2FA parolini so'raydi.
Oxirida chiqqan 1B... qatorini .env faylidagi TELEGRAM_SESSION= ga yozing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from sniper.config import load_settings  # noqa: E402


async def main() -> None:
    settings = load_settings()
    client = TelegramClient(StringSession(), settings.api_id, settings.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            # Telefon raqamni terminalga yozasiz: +998...
            await client.start()
        me = await client.get_me()
        session_string = StringSession.save(client.session)
        print("\n=== SESSION STRING (hech kimga bermang) ===")
        print(session_string)
        print("==========================================")
        print(f"Akkaunt: {me.first_name or ''} @{me.username or '—'} (id {me.id})")
        print("Bu qatorni .env faylidagi TELEGRAM_SESSION= ga yozing.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
