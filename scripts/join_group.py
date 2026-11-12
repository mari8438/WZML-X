#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

import asyncio
from bot.core.config_manager import Config
Config.load()
from pyrogram import Client

async def main():
    session = Config.USER_SESSION_STRING
    chat_id = -1004486743997
    app = Client('joiner', api_id=Config.TELEGRAM_API, api_hash=Config.TELEGRAM_HASH, session_string=session, in_memory=True)
    async with app:
        try:
            await app.add_chat_members(chat_id, "Nothing_leech_robot")
            print("Successfully added @Nothing_leech_robot to Nothing Do!")
        except Exception as e:
            print(f"Adding bot status: {e}")

if __name__ == "__main__":
    asyncio.run(main())
