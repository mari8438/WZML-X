#!/usr/bin/env python3
"""
Forward Tamil Muxed Anime Collection to another Telegram Channel
Reads from data/tamil_muxed_forward_manifest.json and copies sequentially.
Usage:
  python3 scripts/forward_tamil_to_channel.py --target-chat-id -100XXXXXXXXXX
"""
import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

from bot.core.config_manager import Config
Config.load()
from pyrogram import Client
from pyrogram.errors import FloodWait

MANIFEST_PATH = "/home/administrator/starfall/data/tamil_muxed_forward_manifest.json"
PROGRESS_PATH = "/home/administrator/starfall/data/tamil_forwarding_progress.json"
SOURCE_DUMP_ID = -1003887117402
SOURCE_TARGET_ID = -1004415413346

async def main():
    parser = argparse.ArgumentParser(description="Forward Tamil Muxed collection to another channel")
    parser.add_argument("--target-chat-id", type=int, required=True, help="Destination Telegram Channel ID (e.g. -100XXXXXXXXXX)")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of anime to forward (0 = all)")
    args = parser.parse_args()

    dest_channel = args.target_chat_id

    if not os.path.exists(MANIFEST_PATH):
        print(f"Error: Manifest not found at {MANIFEST_PATH}")
        return

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    progress = {}
    if os.path.exists(PROGRESS_PATH):
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                progress = json.load(f)
        except Exception:
            pass

    anime_list = manifest.get("anime", [])
    if args.limit > 0:
        anime_list = anime_list[:args.limit]

    print(f"🚀 Initializing Forwarder for {len(anime_list)} Tamil Muxed Franchises -> Destination: {dest_channel}")

    app = Client(
        "tamil_forwarder",
        api_id=Config.TELEGRAM_API,
        api_hash=Config.TELEGRAM_HASH,
        session_string=Config.USER_SESSION_STRING,
        in_memory=True,
    )

    async with app:
        me = await app.get_me()
        print(f"✅ Connected as @{me.username} (ID: {me.id})")

        for a_idx, anime in enumerate(anime_list, 1):
            title = anime["title"]
            seasons = anime.get("seasons", [])
            print(f"\n==================================================")
            print(f"[{a_idx}/{len(anime_list)}] Processing: {title} ({len(seasons)} seasons)")
            print(f"==================================================")

            for s_idx, season in enumerate(seasons, 1):
                s_num = season["season"]
                ep_ids = season.get("dump_ep_msg_ids", [])
                target_poster_id = season.get("target_poster_msg_id")
                encoder = season.get("encoder", f"rel{s_idx}")
                key = f"{title}_S{s_num:02d}_{encoder}"

                if progress.get(key, {}).get("completed"):
                    print(f"  • Season {s_num:02d} ({len(ep_ids)} eps) already completed. Skipping.")
                    continue

                print(f"  • Forwarding Season {s_num:02d}: {len(ep_ids)} episodes...")

                # 1. Forward Poster Card if present
                if target_poster_id:
                    for attempt in range(1, 4):
                        try:
                            await app.copy_message(
                                chat_id=dest_channel,
                                from_chat_id=SOURCE_TARGET_ID,
                                message_id=target_poster_id,
                            )
                            print(f"    ✅ Forwarded Poster Card #{target_poster_id}")
                            break
                        except FloodWait as fw:
                            print(f"    FloodWait ({fw.value}s) on poster, sleeping...")
                            await asyncio.sleep(fw.value + 3)
                        except Exception as e:
                            print(f"    Warning: Could not copy poster #{target_poster_id}: {e}")
                            break
                    await asyncio.sleep(2.0)

                # 2. Forward Individual Episodes sequentially from Dump
                forwarded_eps = progress.get(key, {}).get("forwarded_ep_ids", [])
                for ep_idx, mid in enumerate(ep_ids, 1):
                    if mid in forwarded_eps:
                        continue

                    for attempt in range(1, 4):
                        try:
                            await app.copy_message(
                                chat_id=dest_channel,
                                from_chat_id=SOURCE_DUMP_ID,
                                message_id=mid,
                            )
                            forwarded_eps.append(mid)
                            print(f"    [{ep_idx:02d}/{len(ep_ids):02d}] Forwarded Ep Msg #{mid}")
                            break
                        except FloodWait as fw:
                            print(f"    FloodWait ({fw.value}s), sleeping...")
                            await asyncio.sleep(fw.value + 3)
                        except Exception as e:
                            print(f"    Error copying ep #{mid} (attempt {attempt}): {e}")
                            await asyncio.sleep(2.0 * attempt)

                    await asyncio.sleep(1.5)

                progress[key] = {
                    "title": title,
                    "season": s_num,
                    "total_eps": len(ep_ids),
                    "forwarded_ep_ids": forwarded_eps,
                    "completed": (len(forwarded_eps) >= len(ep_ids)),
                    "updated_at": time.time(),
                }
                with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
                    json.dump(progress, f, indent=2)

                await asyncio.sleep(3.0)

    print("\n🎉 ALL TAMIL MUXED RELEASES FORWARDED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(main())
