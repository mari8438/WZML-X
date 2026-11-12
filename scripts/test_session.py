#!/usr/bin/env python3
import asyncio
from pyrogram import Client

async def main():
    with open('/home/administrator/starfall/data/new_session_string.txt', 'r') as f:
        sess = f.read().strip()
    
    app = Client('test_session', session_string=sess, in_memory=True)
    try:
        await app.start()
        me = await app.get_me()
        print(f"VERIFIED_SUCCESS: User='{me.first_name}' (@{me.username}) ID={me.id} Phone={me.phone_number} Bot={me.is_bot}")
        await app.stop()
    except Exception as e:
        print(f"VERIFIED_FAILED: {type(e).__name__}: {e}")

if __name__ == '__main__':
    asyncio.run(main())
