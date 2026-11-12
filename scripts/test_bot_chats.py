#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

import asyncio
from bot.core.config_manager import Config
Config.load()
from pyrogram import Client, raw

async def main():
    session = Config.USER_SESSION_STRING
    app = Client('finder_raw', api_id=Config.TELEGRAM_API, api_hash=Config.TELEGRAM_HASH, session_string=session, in_memory=True)
    async with app:
        for q in ["Nothing", "Nothing Do", "Do"]:
            try:
                res = await app.invoke(raw.functions.contacts.Search(q=q, limit=20))
                print(f"Query '{q}': found {len(res.chats)} chats, {len(res.users)} users")
                for c in res.chats:
                    print("  Chat:", getattr(c, 'title', ''), "| ID:", getattr(c, 'id', ''))
                for u in res.users:
                    print("  User/Bot:", getattr(u, 'username', '') or getattr(u, 'first_name', ''), "| ID:", getattr(u, 'id', ''))
            except Exception as e:
                print(f"Error for '{q}': {e}")

if __name__ == "__main__":
    asyncio.run(main())
