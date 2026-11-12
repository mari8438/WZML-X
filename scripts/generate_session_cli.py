#!/usr/bin/env python3
import asyncio
import sys
import os

sys.path.insert(0, '/home/administrator/starfall')
try:
    from config import TELEGRAM_API, TELEGRAM_HASH
except ImportError:
    TELEGRAM_API = 32226674
    TELEGRAM_HASH = "c03defc25ce571541f214b07ed51ce6a"

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired

async def main():
    print("=" * 60)
    print("      PYROGRAM STRING SESSION GENERATOR (CLI)")
    print("=" * 60)
    print(f"Using default API_ID: {TELEGRAM_API}")
    print(f"Using default API_HASH: {TELEGRAM_HASH[:6]}...")
    
    use_default = input("Use these API credentials? (y/n, default: y): ").strip().lower()
    if use_default in ('n', 'no'):
        api_id = int(input("Enter API_ID: ").strip())
        api_hash = input("Enter API_HASH: ").strip()
    else:
        api_id = TELEGRAM_API
        api_hash = TELEGRAM_HASH

    phone = input("\nEnter Phone number with country code (e.g. +919876543210): ").strip()
    
    app = Client(
        name="session_gen",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True
    )
    
    await app.connect()
    print(f"\nSending login code to {phone}...")
    sent_code = await app.send_code(phone)
    phone_code_hash = sent_code.phone_code_hash
    
    code = input("Enter the OTP code received on Telegram: ").strip().replace(" ", "")
    
    try:
        await app.sign_in(phone, phone_code_hash, phone_code=code)
    except SessionPasswordNeeded:
        password = input("2-Step Verification password required. Enter password: ")
        await app.check_password(password)
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        print(f"\n[!] Error: {e}")
        await app.disconnect()
        return

    session_string = await app.export_session_string()
    
    print("\n" + "=" * 60)
    print("SUCCESS! YOUR STRING SESSION IS:")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    
    out_file = "/home/administrator/starfall/data/new_session_string.txt"
    with open(out_file, "w") as f:
        f.write(session_string + "\n")
    print(f"[+] Saved to: {out_file}")
    
    try:
        await app.send_message("me", f"**Your Pyrogram Session String:**\n\n`{session_string}`")
        print("[+] Also sent to your Telegram Saved Messages.")
    except Exception:
        pass
        
    await app.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
