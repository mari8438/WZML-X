#!/usr/bin/env python3
"""
Upgrade recent 223 anime posters in Target Channel to Rich Text formatting
Wraps synopsis into an expandable blockquote, cleans typography, and preserves inline buttons.
"""
import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

from bot.core.config_manager import Config
Config.load()
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

TARGET_CHANNEL_ID = -1004415413346
CATALOGUE_PATH = "/home/administrator/starfall/data/archival_published_catalogue.json"
PROGRESS_PATH = "/home/administrator/starfall/data/archival_posters_upgraded.json"

def transform_to_rich(caption_text: str) -> str:
    if not caption_text or "<blockquote expandable>" in caption_text:
        return caption_text

    # Match synopsis header and content
    m = re.search(r"(💎\s*𝗦𝗬𝗡𝗢𝗣𝗦𝗜𝗦\s*:\s*\n)(.*?)(?=\n━|\n🌟|$)", caption_text, re.DOTALL)
    if not m:
        # Fallback without emoji
        m = re.search(r"(𝗦𝗬𝗡𝗢𝗣𝗦𝗜𝗦\s*:\s*\n)(.*?)(?=\n━|\n🌟|$)", caption_text, re.DOTALL)
    
    if not m:
        return caption_text

    syn_header = m.group(1).strip()
    syn_body = m.group(2).strip()

    rich_syn = f"<blockquote expandable>\n<b>{syn_header}</b>\n{syn_body}\n</blockquote>"
    start, end = m.span()
    new_caption = caption_text[:start] + rich_syn + "\n" + caption_text[end:].lstrip()
    return new_caption

async def main():
    if not os.path.exists(CATALOGUE_PATH):
        print(f"Error: Catalogue not found at {CATALOGUE_PATH}")
        return

    with open(CATALOGUE_PATH, "r", encoding="utf-8") as f:
        catalogue = json.load(f)

    upgraded_ids = set()
    if os.path.exists(PROGRESS_PATH):
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                upgraded_ids = set(json.load(f))
        except Exception:
            pass

    items = []
    for gid, data in catalogue.items():
        pid = data.get("poster_msg_id")
        if pid and pid > 0:
            items.append({
                "gid": gid,
                "title": data.get("franchise_title", "Unknown"),
                "poster_msg_id": pid,
            })

    # Sort in reverse chronological order (newest first)
    items.sort(key=lambda x: x["poster_msg_id"], reverse=True)
    total = len(items)
    print(f"Found {total} total posters in catalogue. {len(upgraded_ids)} already recorded as upgraded.")

    app = Client(
        "poster_rich_upgrader",
        api_id=Config.TELEGRAM_API,
        api_hash=Config.TELEGRAM_HASH,
        session_string=Config.USER_SESSION_STRING,
        in_memory=True,
    )

    async with app:
        me = await app.get_me()
        print(f"Logged in as @{me.username} (ID: {me.id}, Premium={me.is_premium})")

        success_count = 0
        skipped_count = 0

        for idx, item in enumerate(items, 1):
            pid = item["poster_msg_id"]
            title = item["title"]

            if pid in upgraded_ids:
                skipped_count += 1
                continue

            try:
                msg = await app.get_messages(TARGET_CHANNEL_ID, pid)
                if not msg or not msg.caption:
                    print(f"[{idx}/{total}] Msg #{pid} ({title}): No caption found, skipping.")
                    skipped_count += 1
                    continue

                if "<blockquote expandable>" in msg.caption:
                    print(f"[{idx}/{total}] Msg #{pid} ({title}): Already formatted with expandable blockquote.")
                    upgraded_ids.add(pid)
                    skipped_count += 1
                    continue

                new_caption = transform_to_rich(msg.caption)
                if new_caption == msg.caption:
                    print(f"[{idx}/{total}] Msg #{pid} ({title}): Could not parse synopsis, skipped.")
                    skipped_count += 1
                    continue

                # Edit message caption
                for attempt in range(1, 4):
                    try:
                        await app.edit_message_caption(
                            chat_id=TARGET_CHANNEL_ID,
                            message_id=pid,
                            caption=new_caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=msg.reply_markup,
                        )
                        print(f"[{idx}/{total}] ✅ Upgraded Msg #{pid}: {title}")
                        upgraded_ids.add(pid)
                        success_count += 1
                        break
                    except FloodWait as fw:
                        wait_sec = int(fw.value) + 3
                        print(f"FloodWait on #{pid}: Sleeping {wait_sec}s...")
                        await asyncio.sleep(wait_sec)
                    except Exception as e:
                        print(f"Attempt {attempt} failed on #{pid}: {e}")
                        await asyncio.sleep(2.0 * attempt)

                # Safe pacing between edits (2.5s) to avoid FloodWait
                await asyncio.sleep(2.5)

                # Periodically save state
                if len(upgraded_ids) % 5 == 0:
                    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
                        json.dump(list(upgraded_ids), f)

            except Exception as e:
                print(f"[{idx}/{total}] Error processing #{pid} ({title}): {e}")

        # Final save
        with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
            json.dump(list(upgraded_ids), f)

        print("\n" + "=" * 60)
        print(f"FINISHED! Successfully upgraded: {success_count} | Skipped/Already upgraded: {skipped_count} | Total: {total}")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
