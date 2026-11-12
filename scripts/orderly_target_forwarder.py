#!/usr/bin/env python3
"""
Starfall Pipeline Phase 2: Orderly Target Channel Forwarder.
Takes all 160 seasons staged in the Main Dump Channel (-1003887117402)
and publishes them into the Target Channel (-1004415413346) in clean
alphabetical and chronological narrative order with Template 11 Hero Cards,
4GB Merged Season Packs, and Single Episodes.
"""

import os
import re
import sys
import html
import json
import time
import asyncio
import logging
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, "/home/administrator/starfall")

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

from bot.helper.poster_engine.template_eleven import _template_eleven
from bot.modules.starfall_pipeline.anilist_client import AniListClient
from bot.modules.starfall_pipeline.config import (
    TELEGRAM_API,
    TELEGRAM_HASH,
    USER_SESSION_STRING,
)
from bot.modules.starfall_pipeline.release_pipeline import find_anime_landscape_thumbnail

# Logging setup
os.makedirs("/home/administrator/starfall/logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/home/administrator/starfall/logs/target_forwarder.log", mode="a", encoding="utf-8")
    ]
)
LOGGER = logging.getLogger("TargetForwarder")

# Operational Constants
DUMP_CHANNEL_ID = -1003887117402       # Main Dump (-1003887117402)
TARGET_CHANNEL_ID = -1004415413346     # Target Channel (-1004415413346)
TARGET_CLEAN_ID = "4415413346"
DUMP_CLEAN_ID = "3887117402"

PUBLISHED_SEASONS_PATH = "/home/administrator/starfall/data/published_seasons.json"
THUMBS_DIR = "/home/administrator/starfall/thumbnails"
POST_SPACING_SEC = 1.5
INTER_SEASON_COOLDOWN = 4.0

class OrderlyTargetForwarder:
    def __init__(self):
        self.anilist = AniListClient()
        self.client: Optional[Client] = None
        self.published_data: Dict[str, Any] = {}

    async def initialize(self):
        LOGGER.info("🚀 Initializing Phase 2 Orderly Target Forwarder...")
        self.client = Client(
            "hsin_target_forwarder",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            session_string=USER_SESSION_STRING,
            max_concurrent_transmissions=8,
        )
        await self.client.start()
        me = await self.client.get_me()
        LOGGER.info(f"✅ User Client online: @{me.username} (ID: {me.id}, Premium={me.is_premium})")

        with open(PUBLISHED_SEASONS_PATH, "r", encoding="utf-8") as f:
            self.published_data = json.load(f)

        LOGGER.info(f"Loaded {len(self.published_data)} total staged records from published_seasons.json.")

    def save_state(self):
        try:
            with open(PUBLISHED_SEASONS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.published_data, f, indent=2)
        except Exception as e:
            LOGGER.error(f"Failed to save state to {PUBLISHED_SEASONS_PATH}: {e}")

    def prepare_poster_and_caption(
        self,
        title: str,
        season: int,
        year: str,
        total_eps: int,
        encoder: str,
        landscape_thumb: str,
        quality: str = "BluRay",
    ) -> Tuple[str, str]:
        """Generate official Template 11 Hero Poster Card and HTML Caption."""
        rating = "8.2/10"
        genres = "Action, Animation, Fantasy"
        synopsis = f"High-definition media release of {title} Season {season}."
        codec = "AV1" if encoder.lower() in ("breeze", "sokudo", "trix") else "x265"

        try:
            anilist_data = self.anilist.search_anime(f"{title} Season {season}")
            if not anilist_data:
                anilist_data = self.anilist.search_anime(title)
            if anilist_data:
                score = anilist_data.get("averageScore")
                if score:
                    rating = f"{score / 10:.1f}/10"
                if anilist_data.get("genres"):
                    genres = ", ".join(anilist_data["genres"][:3])
                desc = anilist_data.get("description", "")
                if desc:
                    clean_desc = re.sub(r"<[^>]+>", "", desc).strip()
                    synopsis = clean_desc[:400] + "..." if len(clean_desc) > 400 else clean_desc
                if anilist_data.get("seasonYear"):
                    year = str(anilist_data["seasonYear"])
        except Exception as e:
            LOGGER.warning(f"AniList query failed for {title}: {e}")

        slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
        hero_card_path = os.path.join(THUMBS_DIR, f"target_hero_{slug}_S{season:02d}_{encoder}.jpg")
        poster_path = landscape_thumb

        try:
            t11_data = {
                "title": title,
                "year": year,
                "season": season,
                "episodes": total_eps,
                "rating": rating,
                "genres": genres,
                "plot": synopsis,
                "resolution": "1080P",
                "codec": f"HEVC {codec}".upper() if codec == "x265" else "AV1",
                "bit": "10bit",
                "quality": quality,
                "audio": "Tamil-AAC 2.0",
            }
            img = _template_eleven(bg=landscape_thumb, side=landscape_thumb, data=t11_data)
            img.save(hero_card_path, "JPEG", quality=95)
            poster_path = hero_card_path
        except Exception as e:
            LOGGER.error(f"Failed to generate Template 11 Hero Card: {e}")

        caption = (
            f"<b>「 {html.escape(title)} - {year} 」</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "╔════◇═══════════◇════\n"
            f"║ Season ➤ {season}\n"
            f"║ AniList ➤ {rating} Rating\n"
            f"║ Genres ➤ {html.escape(genres)}\n"
            f"║ Quality ➤ 1080p 10bit {quality} {codec}\n"
            f"║ Audio ➤ Tamil-AAC 2.0 ~ ESub\n"
            f"║ Encoder ➤ {html.escape(encoder)}\n"
            "╚════◇═══════════◇════\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🌟 Powered by <b>@Anime_Starfall🥰</b>"
        )
        return poster_path, caption

    async def forward_season(self, key: str, record: Dict[str, Any]) -> bool:
        title = record.get("title", "")
        season = record.get("season", 1)
        encoder = record.get("encoder", "Judas")
        parts = record.get("parts_msg_ids", [])
        eps = record.get("episode_msg_ids", [])

        if not parts and not eps:
            LOGGER.warning(f"No message IDs found for {key}. Skipping.")
            return False

        LOGGER.info("=" * 70)
        LOGGER.info(f"🎬 [Target Publish] Forwarding {title} S{season:02d} ({encoder}) -> Target Channel")
        LOGGER.info(f"   Parts: {len(parts)} | Episodes: {len(eps)}")
        LOGGER.info("=" * 70)

        # 1. Resolve Thumbnail
        landscape_thumb = find_anime_landscape_thumbnail(title, season)
        if not landscape_thumb or not os.path.exists(landscape_thumb):
            landscape_thumb = "/home/administrator/starfall/data/intro_preroll.jpg"

        year = "2024"
        total_eps = len(eps) if eps else len(parts)

        # 2. Template 11 Poster Card
        poster_img, poster_caption = self.prepare_poster_and_caption(
            title=title,
            season=season,
            year=year,
            total_eps=total_eps,
            encoder=encoder,
            landscape_thumb=landscape_thumb,
        )

        card_msg = None
        for attempt in range(1, 4):
            try:
                card_msg = await self.client.send_photo(
                    chat_id=TARGET_CHANNEL_ID,
                    photo=poster_img,
                    caption=poster_caption,
                    parse_mode=ParseMode.HTML,
                )
                LOGGER.info(f"✅ Poster Card published -> Msg #{card_msg.id} (https://t.me/c/{TARGET_CLEAN_ID}/{card_msg.id})")
                break
            except FloodWait as fw:
                LOGGER.warning(f"FloodWait on poster send: sleeping {fw.value + 5}s...")
                await asyncio.sleep(fw.value + 5)
            except Exception as e:
                LOGGER.error(f"Error sending poster card (attempt {attempt}): {e}")
                await asyncio.sleep(2.0 * attempt)

        await asyncio.sleep(POST_SPACING_SEC)

        # 3. Forward Merged 4GB Parts
        for p_idx, part_id in enumerate(parts, 1):
            LOGGER.info(f"📦 Forwarding Merged Part {p_idx}/{len(parts)} (Msg #{part_id})...")
            for attempt in range(1, 4):
                try:
                    p_msg = await self.client.copy_message(
                        chat_id=TARGET_CHANNEL_ID,
                        from_chat_id=DUMP_CHANNEL_ID,
                        message_id=part_id,
                    )
                    LOGGER.info(f"✅ Merged Part {p_idx} forwarded -> Msg #{p_msg.id}")
                    break
                except FloodWait as fw:
                    LOGGER.warning(f"FloodWait: sleeping {fw.value + 5}s...")
                    await asyncio.sleep(fw.value + 5)
                except Exception as e:
                    LOGGER.error(f"Error copying part {part_id}: {e}")
                    await asyncio.sleep(2.0 * attempt)
            await asyncio.sleep(POST_SPACING_SEC)

        # 4. Forward Single Episodes in Strict Chronological Order
        for ep_idx, ep_id in enumerate(eps, 1):
            LOGGER.info(f"🎬 Forwarding Episode {ep_idx:02d}/{len(eps):02d} (Msg #{ep_id})...")
            for attempt in range(1, 4):
                try:
                    ep_msg = await self.client.copy_message(
                        chat_id=TARGET_CHANNEL_ID,
                        from_chat_id=DUMP_CHANNEL_ID,
                        message_id=ep_id,
                    )
                    LOGGER.info(f"✅ Episode {ep_idx:02d} forwarded -> Msg #{ep_msg.id}")
                    break
                except FloodWait as fw:
                    LOGGER.warning(f"FloodWait: sleeping {fw.value + 5}s...")
                    await asyncio.sleep(fw.value + 5)
                except Exception as e:
                    LOGGER.error(f"Error copying episode {ep_id}: {e}")
                    await asyncio.sleep(2.0 * attempt)
            await asyncio.sleep(POST_SPACING_SEC)

        # 5. Mark Completed
        record["forwarded_to_target"] = True
        record["target_poster_msg_id"] = card_msg.id if card_msg else 0
        record["target_forwarded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.save_state()

        LOGGER.info(f"🎉 {title} S{season:02d} ({encoder}) FULLY FORWARDED TO TARGET CHANNEL!")
        await asyncio.sleep(INTER_SEASON_COOLDOWN)
        return True

    async def run(self):
        await self.initialize()

        # Sort all records in clean alphabetical and chronological order
        sorted_keys = sorted(
            self.published_data.keys(),
            key=lambda k: (
                self.published_data[k].get("title", "").lower().strip(),
                self.published_data[k].get("season", 1),
                self.published_data[k].get("encoder", "")
            )
        )

        LOGGER.info(f"Arranged {len(sorted_keys)} staged seasons in clean chronological order.")

        forwarded_count = 0
        skipped_count = 0

        for key in sorted_keys:
            record = self.published_data[key]
            if record.get("forwarded_to_target", False):
                skipped_count += 1
                continue

            success = await self.forward_season(key, record)
            if success:
                forwarded_count += 1

        LOGGER.info("=" * 70)
        LOGGER.info(f"🏁 PHASE 2 TARGET CHANNEL FORWARDING COMPLETED!")
        LOGGER.info(f"Total Forwarded: {forwarded_count} | Previously Staged: {skipped_count}")
        LOGGER.info("=" * 70)

        if self.client:
            await self.client.stop()

def main():
    forwarder = OrderlyTargetForwarder()
    asyncio.run(forwarder.run())

if __name__ == "__main__":
    main()
