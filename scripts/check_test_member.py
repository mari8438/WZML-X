#!/usr/bin/env python3
import asyncio
from pyrogram import Client
from pyrogram.raw import functions

with open('/home/administrator/starfall/data/test_user_session.txt') as f:
    sess = f.read().strip()

async def check():
    app = Client('check_test_member', session_string=sess, in_memory=True)
    async with app:
        try:
            chat = await app.get_chat(-1004486743997)
            print(f"ALREADY_IN_CHAT: {chat.title}")
        except Exception as e:
            print(f"NOT_IN_CHAT: {e}")
            try:
                await app.invoke(functions.messages.ImportChatInvite(hash='7Otj6fq34O0zZWJl'))
                print("JOINED_SUCCESSFULLY!")
            except Exception as e2:
                print(f"JOIN_ERROR: {e2}")

if __name__ == '__main__':
    asyncio.run(check())
