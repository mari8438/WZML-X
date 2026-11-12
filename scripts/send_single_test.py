#!/usr/bin/env python3
import asyncio
from pyrogram import Client

async def test_send():
    with open('/home/administrator/starfall/data/test_user_session.txt') as f:
        s = f.read().strip()
    app = Client('test_sender', session_string=s, in_memory=True)
    async with app:
        await app.send_message(-1004486743997, '/bleech https://gdshare.top/download/Q172ayB9ETtFH1bxwcy1WQ')
        print("SENT_TEST_BLEECH")

if __name__ == '__main__':
    asyncio.run(test_send())
