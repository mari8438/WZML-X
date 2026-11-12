#!/usr/bin/env python3
"""
Upgrade Tamil Muxed Anime Posters in Target Channel (-1004415413346) to Rich Text Formatting.
Uses Telegram Rich Text Editor capabilities:
- Native Blockquotes (<blockquote>) for media specs
- Expandable Blockquotes (<blockquote expandable>) for AniList synopsis
- Robust caching and FloodWait handling
"""
import asyncio
import html
import json
import os
import re
import sys
import time
import requests

sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

from bot.core.config_manager import Config
Config.load()
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

TARGET_CHANNEL_ID = -1004415413346
MANIFEST_PATH = "/home/administrator/starfall/data/tamil_muxed_forward_manifest.json"
PROGRESS_PATH = "/home/administrator/starfall/data/tamil_posters_upgraded.json"
CACHE_PATH = "/home/administrator/starfall/data/tamil_anilist_cache.json"

def fetch_synopsis(title: str, cache: dict) -> str:
    clean_title = re.sub(r"\[.*?\]|\(.*?\)|Season\s*\d+|S\d+", "", title).strip()
    clean_title = re.sub(r"[-_]", " ", clean_title).strip()
    key = clean_title.lower()

    if key in cache:
        return cache[key]

    query = """
    query ($search: String) {
      Media (search: $search, type: ANIME) {
        description(asHtml: false)
      }
    }
    """
    try:
        r = requests.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"search": clean_title}},
            headers={"User-Agent": "StarfallRichEditor/1.0", "Content-Type": "application/json"},
            timeout=8
        )
        if r.status_code == 200:
            desc = r.json().get("data", {}).get("Media", {}).get("description")
            if desc:
                # Clean html tags
                desc = re.sub(r"<br\s*/?>", "\n", desc)
                desc = re.sub(r"<.*?>", "", desc)
                desc = html.unescape(desc).strip()
                # Trim trailing Crunchyroll/AniList source notes
                desc = re.sub(r"\s*\(Source:.*?\)\s*$", "", desc, flags=re.IGNORECASE).strip()
                # Limit length to 750 characters to stay well within limits
                if len(desc) > 750:
                    desc = desc[:747].rsplit(" ", 1)[0] + "..."
                cache[key] = desc
                return desc
    except Exception as e:
        print(f"    [AniList] Error fetching synopsis for '{clean_title}': {e}")

    cache[key] = ""
    return ""

def transform_to_rich(caption_text: str, synopsis: str = "") -> str:
    if not caption_text or "<blockquote expandable>" in caption_text:
        return caption_text

    m_title = re.search(r"「\s*(.*?)\s*-\s*(\d{4})\s*」", caption_text)
    if not m_title:
        return caption_text
    title, year = m_title.group(1).strip(), m_title.group(2).strip()

    m_season = re.search(r"║\s*Season\s*➤\s*(.*)", caption_text)
    season = m_season.group(1).strip() if m_season else "1"

    m_rating = re.search(r"║\s*AniList\s*➤\s*(.*)", caption_text)
    rating = m_rating.group(1).strip() if m_rating else "N/A"
    rating = rating.replace(" Rating", "").strip()

    m_genres = re.search(r"║\s*Genres\s*➤\s*(.*)", caption_text)
    genres = m_genres.group(1).strip() if m_genres else "Anime"

    m_quality = re.search(r"║\s*Quality\s*➤\s*(.*)", caption_text)
    quality = m_quality.group(1).strip() if m_quality else "1080p 10bit"

    m_audio = re.search(r"║\s*Audio\s*➤\s*(.*)", caption_text)
    audio = m_audio.group(1).strip() if m_audio else "Tamil-AAC 2.0 ~ ESub"

    m_encoder = re.search(r"║\s*Encoder\s*➤\s*(.*)", caption_text)
    encoder = m_encoder.group(1).strip() if m_encoder else "Judas"

    lines = [
        f"📺 <b>「 {title} - Season {season} ({year}) 」</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "<blockquote>" + "\n".join([
            f"<b>Season :</b> {season}",
            f"<b>AniList :</b> ★ {rating}",
            f"<b>Genres :</b> {genres}",
            f"<b>Quality :</b> {quality}",
            f"<b>Audio :</b> {audio}",
            f"<b>Encoder :</b> {encoder}",
        ]) + "</blockquote>"
    ]

    if synopsis:
        lines.append(f"\n<blockquote expandable><b>💎 𝗦𝗬𝗡𝗢𝗣𝗦𝗜𝗦 :</b>\n{synopsis}</blockquote>")

    lines.append("━━━━━━━━━━━━━━━━━━━━\n🌟 Powered by @Anime_Starfall 🥰")
    return "\n".join(lines)

async def main():
    if not os.path.exists(MANIFEST_PATH):
        print(f"Error: Manifest not found at {MANIFEST_PATH}")
        return

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    upgraded_ids = set()
    if os.path.exists(PROGRESS_PATH):
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                upgraded_ids = set(json.load(f))
        except Exception:
            pass

    anilist_cache = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                anilist_cache = json.load(f)
        except Exception:
            pass

    # Extract all seasons to upgrade
    tasks = []
    for anime in manifest.get("anime", []):
        anime_title = anime["title"]
        for s in anime.get("seasons", []):
            pid = s.get("target_poster_msg_id")
            if pid and pid > 0:
                tasks.append({
                    "anime_title": anime_title,
                    "season": s["season"],
                    "poster_msg_id": pid,
                    "ep_count": s.get("ep_count", 0),
                })

    total = len(tasks)
    print(f"Found {total} Tamil poster tasks. {len(upgraded_ids)} already recorded as upgraded.")

    app = Client(
        "tamil_poster_rich_upgrader",
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

        for idx, item in enumerate(tasks, 1):
            pid = item["poster_msg_id"]
            title = item["anime_title"]
            s_num = item["season"]

            if pid in upgraded_ids:
                skipped_count += 1
                continue

            try:
                msg = await app.get_messages(TARGET_CHANNEL_ID, pid)
                if not msg or not msg.caption:
                    print(f"[{idx}/{total}] Msg #{pid} ({title} S{s_num}): No caption found, skipping.")
                    skipped_count += 1
                    continue

                if "<blockquote expandable>" in msg.caption:
                    print(f"[{idx}/{total}] Msg #{pid} ({title} S{s_num}): Already formatted with expandable blockquote.")
                    upgraded_ids.add(pid)
                    skipped_count += 1
                    continue

                # Fetch synopsis from AniList
                synopsis = fetch_synopsis(title, anilist_cache)
                # Persist cache periodically
                if len(anilist_cache) % 5 == 0:
                    with open(CACHE_PATH, "w", encoding="utf-8") as f:
                        json.dump(anilist_cache, f, indent=2)

                new_caption = transform_to_rich(msg.caption, synopsis)
                if new_caption == msg.caption:
                    print(f"[{idx}/{total}] Msg #{pid} ({title} S{s_num}): Caption unchanged or unparseable, skipping.")
                    skipped_count += 1
                    continue

                # Edit message caption with safe retries & FloodWait pacing
                for attempt in range(1, 4):
                    try:
                        await app.edit_message_caption(
                            chat_id=TARGET_CHANNEL_ID,
                            message_id=pid,
                            caption=new_caption,
                            parse_mode=ParseMode.HTML,
                        )
                        print(f"[{idx}/{total}] ✅ Upgraded Msg #{pid}: {title} (Season {s_num})")
                        upgraded_ids.add(pid)
                        success_count += 1
                        break
                    except FloodWait as fw:
                        wait_sec = int(fw.value) + 3
                        print(f"    FloodWait on #{pid}: Sleeping {wait_sec}s...")
                        await asyncio.sleep(wait_sec)
                    except Exception as e:
                        print(f"    Attempt {attempt} failed on #{pid}: {e}")
                        await asyncio.sleep(2.0 * attempt)

                # Safe pacing between edits (2.5s) to stay below Telegram Flood limits
                await asyncio.sleep(2.5)

                # Periodically save state
                if len(upgraded_ids) % 10 == 0:
                    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
                        json.dump(list(upgraded_ids), f)

            except Exception as e:
                print(f"[{idx}/{total}] Error processing #{pid} ({title} S{s_num}): {e}")

        # Final save
        with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
            json.dump(list(upgraded_ids), f)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(anilist_cache, f, indent=2)

        print("\n" + "=" * 60)
        print(f"FINISHED! Upgraded: {success_count} | Skipped/Already: {skipped_count} | Total: {total}")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
