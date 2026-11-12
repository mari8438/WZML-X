#!/usr/bin/env python3
import asyncio
from pyrogram import Client

async def main():
    session = 'AQHPBdcAnEZfnuHmmgKmAdnimSOkgiCrLiiWYYRVG9D_jHRWrfUaA369827pLO7ra1LFuUQEeym3pMBXn-RyJn5xyn92LZ3jnfP8fp2QsmCNi_esoERO9o1cY0moRB8r_ha8Sf3YYbuNOtLlb3qsGo-duNXe1JcQ2ktJn58mNzqsYr0A2a_cUehxTMNuUe_AESCe2svHzpYnRBl3CgJf1RsW-w5tyS4QIBqYg3V4DsO9P87tCZYDooeif0acCVuPU_8w0RquVVvg5_8tYBWKYbX1WAQ3XUHO-vglqAK07D5Ae9EEIsHT3jEFTDhheZekeyxe1m2jm7mfvheApmC6FqpfJHbRVgAAAAIPtT73AA'
    app = Client('finder', api_id=32226674, api_hash='c03defc25ce571541f214b07ed51ce6a', session_string=session, in_memory=True)
    async with app:
        count = 0
        async for dialog in app.get_dialogs():
            count += 1
            chat = dialog.chat
            title = chat.title or chat.first_name or ""
            print(f"[{count}] '{title}' (ID: {chat.id}, Type: {chat.type.name})")
            if "nothing" in title.lower() or "do" in title.lower():
                print(f"  >>> MATCH TARGET: '{title}' -> ID: {chat.id}")
            if count >= 35:
                break

if __name__ == "__main__":
    asyncio.run(main())
