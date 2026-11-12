#!/usr/bin/env python3
import asyncio
import base64
import struct
from pyrogram import Client

s = "BQHHXqgAtNqub65x0BVGYGY9amHiTwqEMyFz5W7R03YbMh0Jm3tCgu_lcVpIVHsdnRArmwr5NuSfAs_AZamSKCtVJFdkqhM6nD3ypT6D5u2nXnMwCkG20TF8TDViavr2Rte6IwbGrHIVJ8a3vO0JVSoWsu_8wrgOcKtzQwsPdUCoj5WUqFSD6pceg6j-022vczn9HsWewi4n5J5evT1LYkc8attWtIHRdlKNWTp8794HC1fIt7cMbyCE6QmLwKgkEn03iH7sVxdB6slPFb8c9eprio7-UGqYozuYbv8CytRW9gCYVxFmWfXUC5EzM0uSKj4cy9fiXR6B0avsqUkqyOcZnJwQAAAAINZOcNAA"

raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
dc_id = raw[0]
api_id = struct.unpack('>I', raw[1:5])[0]
test_mode = bool(raw[5])
auth_key = raw[6:262]

async def test_direct_key():
    app = Client(
        "test_direct",
        api_id=32226674,
        api_hash="c03defc25ce571541f214b07ed51ce6a",
        in_memory=True
    )
    
    await app.storage.open()
    await app.storage.dc_id(dc_id)
    await app.storage.api_id(32226674)
    await app.storage.test_mode(test_mode)
    await app.storage.auth_key(auth_key)
    await app.storage.user_id(1)
    await app.storage.is_bot(False)
    
    try:
        async with app:
            me = await app.get_me()
            print("\n🎉 SUCCESS! Connected directly using Auth Key!")
            print(f"Account ID: {me.id}")
            print(f"First Name: {me.first_name}")
            print(f"Last Name: {me.last_name}")
            print(f"Username: @{me.username}" if me.username else "Username: None")
            print(f"Phone: +{me.phone_number}" if me.phone_number else "Phone: Hidden")
            print(f"Is Premium: {getattr(me, 'is_premium', False)}")
            
            clean_str = await app.export_session_string()
            print(f"\nClean Working Session String:\n{clean_str}")
            return True
    except Exception as e:
        print(f"\nDirect connection result: {type(e).__name__} - {e}")
        return False

if __name__ == "__main__":
    asyncio.run(test_direct_key())
