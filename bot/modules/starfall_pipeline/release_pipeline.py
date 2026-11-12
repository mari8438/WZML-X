#!/usr/bin/env python3
"""
Starfall v1.7 High-Speed Multi-Bot Staging & Sequential Release Pipeline
========================================================================
- Stage A: High-Speed Parallel Bot Upload to Auto Rename Dump (-1002211546440)
- Stage B: Strict Sequential Target Publishing to Tamil Muxed Collection (-1004415413346)
- Stage C: Post-Release Verification & Local NVMe Storage Sweep
"""

import os
import re
import sys
import time
import glob
import asyncio
import logging
import subprocess
from typing import Dict, List, Optional, Any, Tuple

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
)
LOGGER = logging.getLogger("StarfallReleasePipeline")

# Silence noisy Pyrogram loggers
for _log in ("pyrogram", "pyrogram.session", "pyrogram.connection", "pyrogram.dispatcher", "pyrogram.client"):
    logging.getLogger(_log).setLevel(logging.ERROR)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from bot.modules.starfall_pipeline.config import (
    TELEGRAM_API,
    TELEGRAM_HASH,
    BOT_TOKEN,
    USER_SESSION_STRING,
    HELPER_TOKENS,
    DATA_DIR,
    USER_SETTINGS,
)
from bot.modules.starfall_pipeline.merge_planner import SeasonMergePlanner, format_merged_filename

DUMP_CHANNEL_ID = -1002211546440       # Auto Rename Dump
TARGET_CHANNEL_ID = -1004415413346     # Tamil Muxed Collection
TARGET_CLEAN_ID = "4415413346"         # Clean ID for private channel links
POST_SPACING_SEC = 3.5                 # 3.5s pacing for Zero-FloodWait sequential commits


_META_CACHE: Dict[str, Dict[str, Any]] = {}


def probe_file_meta(filepath: str) -> Dict[str, Any]:
    """Probe video duration, width, height using ffprobe with in-memory caching."""
    if filepath in _META_CACHE:
        return _META_CACHE[filepath]
    meta = {"duration": 1440, "width": 1920, "height": 1080}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            filepath
        ]
        import json
        raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode()
        data = json.loads(raw)
        streams = data.get("streams", [])
        if streams:
            meta["width"] = int(streams[0].get("width", 1920))
            meta["height"] = int(streams[0].get("height", 1080))
        fmt_dur = data.get("format", {}).get("duration")
        if fmt_dur and fmt_dur != "N/A":
            d_val = float(fmt_dur)
            if 30 <= d_val <= 172800:
                meta["duration"] = int(d_val)
    except Exception as e:
        LOGGER.warning(f"ffprobe fallback on {os.path.basename(filepath)}: {e}")
    _META_CACHE[filepath] = meta
    return meta


def find_anime_landscape_thumbnail(title: str, season: int) -> Optional[str]:
    """
    Find official 16:9 landscape thumbnail in data/thumbnails/ strictly for this season.
    If not found, dynamically queries AniList for the official bannerImage and caches it.
    NEVER falls back to a different season of the same anime.
    """
    dt_dir = os.path.join(BASE_DIR, "data", "thumbnails")
    os.makedirs(dt_dir, exist_ok=True)
    clean_t = re.sub(r'[^a-zA-Z0-9]', '', title).lower()
    s_tag = f"s{season:02d}"
    s_tag_alt = f"s{season}"

    is_ds = "demonslayer" in clean_t or "kimetsu" in clean_t
    is_demon_lord = "demonlord" in clean_t
    is_archdemon = "archdemon" in clean_t

    def _thumb_matches_title(clean_f: str) -> bool:
        if is_ds:
            if not ("demonslayer" in clean_f or "kimetsu" in clean_f):
                return False
            if "demonlord" in clean_f or "archdemon" in clean_f:
                return False
            return True
        elif is_demon_lord:
            return "demonlord" in clean_f and not ("demonslayer" in clean_f or "kimetsu" in clean_f)
        elif is_archdemon:
            return "archdemon" in clean_f and not ("demonslayer" in clean_f or "kimetsu" in clean_f)
        else:
            if "demonslayer" in clean_f or "kimetsu" in clean_f:
                return False
            return clean_t in clean_f or any(w in clean_f for w in title.lower().split() if len(w) > 4)

    # Priority 1: Exact Title + Exact Season + landscape
    for f in os.listdir(dt_dir):
        clean_f = re.sub(r'[^a-zA-Z0-9]', '', f).lower()
        if _thumb_matches_title(clean_f) and (s_tag in clean_f or s_tag_alt in clean_f) and "landscape" in clean_f:
            return os.path.join(dt_dir, f)

    # Priority 2: Single-season anime with no season tag
    if season == 1:
        for f in os.listdir(dt_dir):
            clean_f = re.sub(r'[^a-zA-Z0-9]', '', f).lower()
            if clean_t in clean_f and "landscape" in clean_f and not any(f"s{x:02d}" in clean_f or f"s{x}" in clean_f for x in range(2, 10)):
                return os.path.join(dt_dir, f)

    # Priority 3: Dynamic AniList GraphQL Banner Fetcher
    try:
        from bot.modules.starfall_pipeline.anilist_client import AniListClient
        anilist = AniListClient()
        search_query = f"{title} Season {season}" if season > 1 else title
        res = anilist.search_anime(search_query)
        banner_url = res.get("bannerImage") if res else None
        if not banner_url and res and res.get("title", {}).get("romaji"):
            rom_search = f"{res['title']['romaji']} Season {season}" if season > 1 else res['title']['romaji']
            res_rom = anilist.search_anime(rom_search)
            banner_url = res_rom.get("bannerImage") if res_rom else None

        if banner_url:
            clean_slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
            dest_file = os.path.join(dt_dir, f"{clean_slug}_S{season:02d}_landscape.jpg")
            resp = requests.get(banner_url, timeout=12)
            if resp.status_code == 200 and len(resp.content) > 1000:
                from PIL import Image
                import io
                with Image.open(io.BytesIO(resp.content)) as im:
                    im = im.convert("RGB")
                    # Center crop to 16:9
                    w, h = im.size
                    target_ratio = 16 / 9
                    if w / h > target_ratio:
                        new_w = int(h * target_ratio)
                        left = (w - new_w) // 2
                        im = im.crop((left, 0, left + new_w, h))
                    elif w / h < target_ratio:
                        new_h = int(w / target_ratio)
                        top = (h - new_h) // 2
                        im = im.crop((0, top, w, top + new_h))
                    im = im.resize((1280, 720), Image.Resampling.LANCZOS)
                    im.save(dest_file, "JPEG", quality=92, optimize=True)
                    LOGGER.info(f"🎨 Auto-fetched and cached AniList landscape banner: {dest_file}")
                    return dest_file
    except Exception as e:
        LOGGER.warning(f"Could not auto-fetch AniList banner for {title} S{season:02d}: {e}")

    return None


def prepare_hd_thumbnail(src_path: Optional[str], cache_path: str) -> Optional[str]:
    """Ensure thumbnail is 1280x720 16:9 HD JPEG under 200KB for Telegram."""
    if not src_path or not os.path.exists(src_path):
        return src_path
    try:
        from PIL import Image
        with Image.open(src_path) as im:
            im = im.convert("RGB")
            if im.size != (1280, 720):
                im_resized = im.resize((1280, 720), Image.Resampling.LANCZOS)
                im_resized.save(cache_path, "JPEG", quality=95, optimize=True)
                return cache_path
            return src_path
    except Exception as e:
        LOGGER.warning(f"Failed to prepare HD thumbnail from {src_path}: {e}")
        return src_path


class StarfallReleasePipeline:
    def __init__(self):
        self.tokens = [t.strip() for t in HELPER_TOKENS.split() if t.strip()]
        self.helper_clients: List[Client] = []
        self.client_names: Dict[str, str] = {}
        self.user_client: Optional[Client] = None
        self._is_initialized = False

    async def initialize(self):
        """Boot helper bots and premium user client concurrently."""
        if self._is_initialized:
            return

        sessions_dir = os.path.join(DATA_DIR, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)

        # 1. Start User Client
        LOGGER.info("🔑 Starting User Client (@Hsin_The_Moon_Fox)...")
        self.user_client = Client(
            "starfall_user_uploader",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            session_string=USER_SESSION_STRING,
            in_memory=True,
            workers=16,
            max_concurrent_transmissions=6,
        )
        await self.user_client.start()
        u_me = await self.user_client.get_me()
        u_uname = f"@{u_me.username}" if u_me.username else "@Hsin_The_Moon_Fox"
        self.client_names[self.user_client.name] = u_uname
        LOGGER.info(f"✅ User Client online: {u_uname} (ID: {u_me.id}, Premium={u_me.is_premium})")

        # 2. Start Helper Bots concurrently
        LOGGER.info(f"🤖 Starting {len(self.tokens)} Helper Bots...")
        async def _start_bot(idx: int, token: str):
            bot = Client(
                f"starfall_helper_{idx}",
                api_id=TELEGRAM_API,
                api_hash=TELEGRAM_HASH,
                bot_token=token,
                workdir=sessions_dir,
                in_memory=False,
                workers=8,
                max_concurrent_transmissions=4,
            )
            try:
                await bot.start()
                me = await bot.get_me()
                b_uname = f"@{me.username}" if me.username else f"bot_{idx}"
                self.client_names[bot.name] = b_uname
                LOGGER.info(f"✅ Helper Bot #{idx} online: {b_uname}")
                return bot
            except Exception as e:
                LOGGER.error(f"❌ Failed to start Helper Bot #{idx}: {e}")
                return None

        bot_tasks = [_start_bot(i, tok) for i, tok in enumerate(self.tokens, 1)]
        results = await asyncio.gather(*bot_tasks)
        self.helper_clients = [b for b in results if b is not None]
        self._is_initialized = True
        LOGGER.info(f"🚀 Release Pipeline Ready with {len(self.helper_clients)} Bots + User Client.")

    async def close(self):
        """Gracefully shut down all Pyrogram clients."""
        for b in self.helper_clients:
            try:
                await b.stop()
            except Exception:
                pass
        if self.user_client:
            try:
                await self.user_client.stop()
            except Exception:
                pass
        self._is_initialized = False

    async def upload_single_to_dump(
        self,
        client: Client,
        filepath: str,
        caption: str,
        thumb_path: Optional[str],
        tag: str,
        dump_results: Dict[str, int],
        exact_duration: Optional[int] = None,
    ):
        """Uploads a single video directly to the dump channel."""
        uname = self.client_names.get(client.name, client.name)
        fname = os.path.basename(filepath)
        fsize_mib = os.path.getsize(filepath) / (1024 * 1024)
        meta = probe_file_meta(filepath)
        final_duration = exact_duration if exact_duration is not None else meta["duration"]

        v_thumb = thumb_path if (thumb_path and os.path.exists(thumb_path)) else None

        LOGGER.info(f"⬆️  [Dump Stage] Uploading {fname} ({fsize_mib:.1f} MiB, dur={final_duration}s) via {uname}...")
        t0 = time.time()
        for attempt in range(1, 5):
            try:
                msg: Message = await client.send_video(
                    chat_id=DUMP_CHANNEL_ID,
                    video=filepath,
                    caption=caption,
                    thumb=v_thumb,
                    video_cover=v_thumb,
                    duration=final_duration,
                    width=meta["width"],
                    height=meta["height"],
                    supports_streaming=True,
                    parse_mode=ParseMode.HTML,
                )
                elapsed = time.time() - t0
                speed_mbps = (fsize_mib * 8) / max(elapsed, 0.01)
                LOGGER.info(
                    f"✅ [Dump Stage] Confirmed {fname} -> Msg #{msg.id} via {uname} "
                    f"in {elapsed:.1f}s ({speed_mbps:.1f} Mbps)"
                )
                dump_results[tag] = msg.id
                return msg.id
            except FloodWait as fw:
                wait_s = int(getattr(fw, "value", 15)) + 2
                LOGGER.warning(f"⚠️ FloodWait {wait_s}s on {uname} (attempt {attempt}). Sleeping...")
                await asyncio.sleep(wait_s)
            except RPCError as rpc:
                LOGGER.error(f"❌ RPC error on {uname} uploading {fname} (attempt {attempt}): {rpc}")
                await asyncio.sleep(3.0 * attempt)
            except Exception as e:
                LOGGER.error(f"❌ Error on {uname} uploading {fname} (attempt {attempt}): {e}")
                await asyncio.sleep(2.0 * attempt)

        raise RuntimeError(f"Failed to upload {fname} to dump channel after 4 attempts.")

    async def execute_solo_leveling_release(self) -> Dict[str, Any]:
        """
        Executes end-to-end benchmark release for Solo Leveling Season 1:
        1. High-speed parallel staging of 2 merged parts + 12 single episodes to Dump Channel.
        2. Strict sequential forwarding with 3.5s delay to Target Channel.
        3. Local NVMe sweep upon confirmation.
        """
        await self.initialize()

        muxed_dir = "/home/administrator/starfall/downloads/muxed_output"
        thumbs_dir = "/home/administrator/starfall/thumbnails"

        # 1. Collect files
        p1_matches = [
            os.path.join(muxed_dir, f)
            for f in os.listdir(muxed_dir)
            if 'Solo Leveling' in f and ('E01-06' in f or 'Part 1' in f) and f.endswith('.mkv')
        ]
        p2_matches = [
            os.path.join(muxed_dir, f)
            for f in os.listdir(muxed_dir)
            if 'Solo Leveling' in f and ('E07-12' in f or 'Part 2' in f) and f.endswith('.mkv')
        ]
        part1_file = p1_matches[0] if p1_matches else ""
        part2_file = p2_matches[0] if p2_matches else ""

        single_ep_files: List[Tuple[int, str]] = []
        for i in range(1, 13):
            matches = [
                os.path.join(muxed_dir, f)
                for f in os.listdir(muxed_dir)
                if 'Solo Leveling' in f and f'S01E{i:02d}' in f and f.endswith('.mkv')
            ]
            if not matches:
                raise FileNotFoundError(f"Missing Solo Leveling episode {i:02d} in {muxed_dir}")
            single_ep_files.append((i, matches[0]))

        if not os.path.exists(part1_file) or not os.path.exists(part2_file):
            raise FileNotFoundError("Merged parts missing in muxed_output directory!")

        raw_landscape = find_anime_landscape_thumbnail("Solo Leveling", 1)
        hd_thumb_path = os.path.join(thumbs_dir, "hd_landscape_Solo_Leveling_S01.jpg")
        master_hd_thumb = prepare_hd_thumbnail(raw_landscape, hd_thumb_path) if raw_landscape else os.path.join(thumbs_dir, "hero_card_Solo_Leveling_S01.jpg")
        poster_thumb = os.path.join(thumbs_dir, "poster_Solo_Leveling_S01.jpg")

        # =========================================================================
        # PHASE A: Parallel Dump Upload
        # =========================================================================
        LOGGER.info("=" * 70)
        LOGGER.info("PHASE A: HIGH-SPEED PARALLEL BOT STAGING -> AUTO RENAME DUMP")
        LOGGER.info("=" * 70)

        dump_results: Dict[str, int] = {}

        # Episode Queue for 5 Helper Bots
        ep_queue: asyncio.Queue = asyncio.Queue()
        for item in single_ep_files:
            ep_queue.put_nowait(item)

        async def _bot_worker(bot_client: Client):
            while not ep_queue.empty():
                try:
                    ep_num, ep_path = ep_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                # Clean filename format
                clean_name = re.sub(r"\s+CR\s+", " ", os.path.basename(ep_path))
                clean_name = re.sub(r"~\s+EMBER\s+~", "~", clean_name)
                clean_name = re.sub(r"\s+", " ", clean_name).strip()

                await self.upload_single_to_dump(
                    client=bot_client,
                    filepath=ep_path,
                    caption=f"<b>{clean_name}</b>",
                    thumb_path=master_hd_thumb,
                    tag=f"ep_{ep_num:02d}",
                    dump_results=dump_results,
                )
                ep_queue.task_done()

        # User Client Worker for Merged Parts (Part 1 and Part 2)
        async def _user_worker():
            for p_tag, p_file in [("part_1", part1_file), ("part_2", part2_file)]:
                await self.upload_single_to_dump(
                    client=self.user_client,
                    filepath=p_file,
                    caption=f"<b>{os.path.basename(p_file)}</b>",
                    thumb_path=master_hd_thumb,
                    tag=p_tag,
                    dump_results=dump_results,
                )

        # Launch 5 Bot Workers + 1 User Client Worker in parallel
        workers = [_user_worker()]
        for bot in self.helper_clients:
            workers.append(_bot_worker(bot))

        await asyncio.gather(*workers)

        LOGGER.info("=" * 70)
        LOGGER.info("✅ ALL 14 ASSETS CONFIRMED IN AUTO RENAME DUMP CHANNEL!")
        for k, v in sorted(dump_results.items()):
            LOGGER.info(f"   - {k}: Dump Msg #{v}")
        LOGGER.info("=" * 70)

        # =========================================================================
        # PHASE B: Sequential Target Publishing
        # =========================================================================
        LOGGER.info("PHASE B: SEQUENTIAL TARGET CHANNEL PUBLISHING (-1004415413346)")
        LOGGER.info("=" * 70)

        target_links: List[Dict[str, Any]] = []

        # 1. Poster Card (Template 11) with synopsis
        poster_caption = (
            "<b>「 Solo Leveling - 2024 」</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "╔════◇═══════════◇════\n"
            "║ Season ➤ 1\n"
            "║ AniList ➤ 8.0 Rating\n"
            "║ Genres ➤ Action, Adventure, Fantasy\n"
            "║ Quality ➤ 1080p 10bit BluRay x265\n"
            "║ Audio ➤ Tamil AAC 2.0 ~ ESub\n"
            "╚════◇═══════════◇════\n"
            " <blockquote expandable>Synopsis :\n   "
            "They say whatever doesn't kill you makes you stronger, but that's not the case for the world's weakest hunter Sung Jinwoo. After being brutally slaughtered by monsters in a high-ranking dungeon, Jinwoo came back with the System, a program only he could see, that's leveling him up in every way. Now, he's inspired to discover the secrets behind his powers and the dungeon that spawned them."
            "</blockquote>\n\n"
            "Dropped By ➤ [@Anime_Starfall🥰]"
        )

        LOGGER.info(f"📸 Step 1/15: Posting Season Poster Card (Template 11)...")
        card_post: Message = await self.user_client.send_photo(
            chat_id=TARGET_CHANNEL_ID,
            photo=hero_card_thumb if os.path.exists(hero_card_thumb) else poster_thumb,
            caption=poster_caption,
            parse_mode=ParseMode.HTML,
        )
        poster_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{card_post.id}"
        target_links.append({
            "step": 1,
            "title": "Poster Card (Template 11)",
            "msg_id": card_post.id,
            "url": poster_link
        })
        LOGGER.info(f"✅ Poster Card published -> Msg #{card_post.id} ({poster_link})")
        LOGGER.info(f"⏳ Waiting {POST_SPACING_SEC}s spacing before next release...")
        await asyncio.sleep(POST_SPACING_SEC)

        # 2. Merged Part 1
        LOGGER.info(f"📦 Step 2/15: Copying Merged Part 1 (E01-06)...")
        p1_msg: Message = await self.user_client.copy_message(
            chat_id=TARGET_CHANNEL_ID,
            from_chat_id=DUMP_CHANNEL_ID,
            message_id=dump_results["part_1"],
        )
        p1_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{p1_msg.id}"
        target_links.append({
            "step": 2,
            "title": "Merged Part 1 (E01-E06)",
            "msg_id": p1_msg.id,
            "url": p1_link
        })
        LOGGER.info(f"✅ Merged Part 1 published -> Msg #{p1_msg.id} ({p1_link})")
        LOGGER.info(f"⏳ Waiting {POST_SPACING_SEC}s spacing before next release...")
        await asyncio.sleep(POST_SPACING_SEC)

        # 3. Merged Part 2
        LOGGER.info(f"📦 Step 3/15: Copying Merged Part 2 (E07-12)...")
        p2_msg: Message = await self.user_client.copy_message(
            chat_id=TARGET_CHANNEL_ID,
            from_chat_id=DUMP_CHANNEL_ID,
            message_id=dump_results["part_2"],
        )
        p2_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{p2_msg.id}"
        target_links.append({
            "step": 3,
            "title": "Merged Part 2 (E07-E12)",
            "msg_id": p2_msg.id,
            "url": p2_link
        })
        LOGGER.info(f"✅ Merged Part 2 published -> Msg #{p2_msg.id} ({p2_link})")
        LOGGER.info(f"⏳ Waiting {POST_SPACING_SEC}s spacing before next release...")
        await asyncio.sleep(POST_SPACING_SEC)

        # 4. Single Episodes 01 through 12
        for ep_num in range(1, 13):
            step_num = 3 + ep_num
            LOGGER.info(f"🎬 Step {step_num}/15: Copying Episode {ep_num:02d}...")
            ep_msg: Message = await self.user_client.copy_message(
                chat_id=TARGET_CHANNEL_ID,
                from_chat_id=DUMP_CHANNEL_ID,
                message_id=dump_results[f"ep_{ep_num:02d}"],
            )
            ep_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{ep_msg.id}"
            target_links.append({
                "step": step_num,
                "title": f"Episode {ep_num:02d}",
                "msg_id": ep_msg.id,
                "url": ep_link
            })
            LOGGER.info(f"✅ Episode {ep_num:02d} published -> Msg #{ep_msg.id} ({ep_link})")
            if ep_num < 12:
                LOGGER.info(f"⏳ Waiting {POST_SPACING_SEC}s spacing before next release...")
                await asyncio.sleep(POST_SPACING_SEC)

        LOGGER.info("=" * 70)
        LOGGER.info("🎉 ALL 15 RELEASES SUCCESSFULLY PUBLISHED TO TARGET CHANNEL!")
        LOGGER.info("=" * 70)

        # =========================================================================
        # PHASE C: Post-Release Local Storage Sweep
        # =========================================================================
        LOGGER.info("PHASE C: POST-RELEASE LOCAL STORAGE SWEEP")
        LOGGER.info("=" * 70)
        total_purged_bytes = 0

        # Purge single episodes
        for ep_num, ep_path in single_ep_files:
            if os.path.exists(ep_path):
                sz = os.path.getsize(ep_path)
                os.remove(ep_path)
                total_purged_bytes += sz
                LOGGER.info(f"🧹 Purged single episode {os.path.basename(ep_path)} ({sz / (1024*1024):.1f} MiB)")

        # Purge merged parts
        for mf in (part1_file, part2_file):
            if os.path.exists(mf):
                sz = os.path.getsize(mf)
                os.remove(mf)
                total_purged_bytes += sz
                LOGGER.info(f"🧹 Purged merged part {os.path.basename(mf)} ({sz / (1024*1024):.1f} MiB)")

        LOGGER.info(f"✨ Sweep complete! Reclaimed {total_purged_bytes / (1024**3):.2f} GB on NVMe storage.")

        return {
            "dump_results": dump_results,
            "target_links": target_links,
            "purged_gb": total_purged_bytes / (1024**3),
        }


if __name__ == "__main__":
    async def main():
        pipeline = StarfallReleasePipeline()
        try:
            res = await pipeline.execute_solo_leveling_release()
            print("\n" + "=" * 50)
            print("RELEASE SUMMARY:")
            for item in res["target_links"]:
                print(f"[{item['step']}/15] {item['title']}: {item['url']}")
            print(f"Total Reclaimed Space: {res['purged_gb']:.2f} GB")
            print("=" * 50)
        finally:
            await pipeline.close()

    asyncio.run(main())
