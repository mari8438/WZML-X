#!/usr/bin/env python3
import asyncio
import logging
import os
import re
import sys
import time
from typing import Dict, Any, List, Tuple

sys.path.insert(0, "/home/administrator/starfall")

from PIL import Image
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

from bot.modules.starfall_pipeline.autonomous_agent import StarfallAutonomousAgent, PHASE1_COMPLETED_CATALOGUE
from bot.modules.starfall_pipeline.config import (
    TELEGRAM_API,
    TELEGRAM_HASH,
    USER_SESSION_STRING,
    HELPER_TOKENS,
    DATA_DIR,
)
from bot.modules.starfall_pipeline.release_pipeline import probe_file_meta, prepare_hd_thumbnail, find_anime_landscape_thumbnail
from bot.modules.starfall_pipeline.merge_planner import format_merged_filename

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
)
LOGGER = logging.getLogger("SoloLevelingRelease")

# Silence noisy Pyrogram loggers
for _log in ("pyrogram", "pyrogram.session", "pyrogram.connection", "pyrogram.dispatcher", "pyrogram.client"):
    logging.getLogger(_log).setLevel(logging.ERROR)

DUMP_CHANNEL_ID = -1003887117402       # Main Dump (-1003887117402)
TARGET_CHANNEL_ID = -1004415413346     # Tamil Muxed Collection (-1004415413346)
TARGET_CLEAN_ID = "4415413346"
DUMP_CLEAN_ID = "3887117402"
POST_SPACING_SEC = 3.5


async def mux_season_1_if_needed():
    """Ensure all 12 episodes of Solo Leveling S01 are muxed with the new autorename & fixed sync."""
    muxed_dir = "/home/administrator/starfall/downloads/muxed_output"
    os.makedirs(muxed_dir, exist_ok=True)
    
    needed = []
    for i in range(1, 13):
        matches = [f for f in os.listdir(muxed_dir) if "Solo Leveling" in f and f"S01E{i:02d}" in f and f.endswith(".mkv")]
        if not matches:
            needed.append(i)

    if not needed:
        LOGGER.info(f"✅ All 12 episodes of Solo Leveling S01 already present in {muxed_dir}")
        return True

    LOGGER.info(f"🚀 Ingesting & Muxing missing Solo Leveling S01 episodes: {needed}")
    agent = StarfallAutonomousAgent()
    sl_def = [x for x in PHASE1_COMPLETED_CATALOGUE if x["title"] == "Solo Leveling" and x["season"] == 1][0]
    
    # Audit Tamil audio
    is_complete, audio_map = agent.audit_audio_bank(sl_def)
    if not is_complete:
        LOGGER.error(f"Audio bank missing episodes for Solo Leveling S01: {len(audio_map)}/12 found")
        return False
        
    candidate = {
        "title": "[EMBER] Solo Leveling (2024-2025) (Season 1 + 2) [BDRip] [1080p Dual Audio HEVC 10 bits DDP] (Ore dake Level Up na Ken) (Batch)",
        "info_hash": "09760b4724856e9d8eba76bfdfd00b079a2b3585",
        "magnet_uri": "magnet:?xt=urn:btih:09760b4724856e9d8eba76bfdfd00b079a2b3585",
        "release_group": "EMBER",
        "codec": "x265",
        "size": "13.2 GiB",
        "seeders": 220,
    }
    
    # Download clean video files for S01
    download_dir, info_hash = await agent.download_batch(sl_def, candidate, needed_eps=set(needed))
    if not download_dir or not os.path.exists(download_dir):
        LOGGER.error("Batch download failed!")
        return False
        
    # Map video files
    found_videos = []
    for root, _, files in os.walk(download_dir):
        for f in sorted(files):
            if f.lower().endswith((".mkv", ".mp4")):
                f_path = os.path.join(root, f)
                if os.path.getsize(f_path) < 10 * 1024 * 1024:
                    continue
                s_num, ep_num = agent.parse_video_season_and_ep(f, parent_path=root, expected_season=1)
                if s_num == 1 and ep_num and ep_num in needed:
                    found_videos.append((ep_num, f_path))
                    
    ep_to_path = {}
    for ep_num, p in found_videos:
        if ep_num not in ep_to_path:
            ep_to_path[ep_num] = p
            
    video_files = sorted(ep_to_path.items(), key=lambda x: x[0])
    LOGGER.info(f"Mapping complete: {len(video_files)} episodes ready to mux.")
    
    # 4 Parallel Workers muxing in RAM tmpfs
    sem = asyncio.Semaphore(4)
    async def mux_worker(ep_num: int, v_path: str):
        async with sem:
            tamil_audio = audio_map.get(ep_num)
            out_filename = agent.mux_engine.format_output_filename(
                anime_name="Solo Leveling",
                season_num=1,
                episode_num=ep_num,
                year=2024,
                resolution="1080p",
                bit="10bit",
                quality="BluRay",
                codec="x265",
                release_group="EMBER",
            )
            out_path = os.path.join(muxed_dir, out_filename)
            LOGGER.info(f"🎬 Muxing Episode {ep_num:02d}/12: {out_filename}")
            sync_res = await agent.sync_engine.analyze_sync(v_path, tamil_audio)
            calc_delay = sync_res.get("calculated_delay_ms", 0)
            ratio = sync_res.get("speed_ratio", 1.0)
            
            cover_photo = "/home/administrator/starfall/data/intro_preroll.jpg"
            ok = await agent.mux_engine.mux_single_episode(
                video_path=v_path,
                tamil_audio_path=tamil_audio,
                output_mkv_path=out_path,
                calculated_delay_ms=calc_delay,
                speed_ratio=ratio,
                poster_path=cover_photo if os.path.exists(cover_photo) else None,
                release_group="EMBER",
            )
            if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 50 * 1024 * 1024:
                LOGGER.info(f"✅ Mux Verified ({os.path.getsize(out_path)/(1024**2):.1f} MB): {out_filename}")
                try:
                    os.remove(v_path)
                    LOGGER.info(f"🗑️ Purged raw video: {os.path.basename(v_path)}")
                except Exception:
                    pass
                return True
            else:
                LOGGER.error(f"❌ Failed muxing episode {ep_num}")
                return False

    await asyncio.gather(*(mux_worker(e, p) for e, p in video_files))
    
    # Cleanup torrent and clean folder
    if info_hash:
        agent.qbt.delete_torrent(info_hash, delete_files=False)
    import shutil
    shutil.rmtree(download_dir, ignore_errors=True)
    return True


async def create_merged_parts_if_needed():
    """Create Part 1 (E01-E06) and Part 2 (E07-E12) with cover art under 3,950 MiB."""
    muxed_dir = "/home/administrator/starfall/downloads/muxed_output"
    cover_photo = "/home/administrator/starfall/data/intro_preroll.jpg"
    
    # Check for existing merged parts
    p1_name = "🄰🅂- [S1E01-06] Solo Leveling Part 1 (2024) 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ EMBER ~ [@Anime_Starfall🥰].mkv"
    p2_name = "🄰🅂- [S1E07-12] Solo Leveling Part 2 (2024) 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ EMBER ~ [@Anime_Starfall🥰].mkv"
    
    p1_path = os.path.join(muxed_dir, p1_name)
    p2_path = os.path.join(muxed_dir, p2_name)
    
    # Single episode paths
    single_ep_paths = []
    for i in range(1, 13):
        m = [os.path.join(muxed_dir, f) for f in os.listdir(muxed_dir) if "Solo Leveling" in f and f"S01E{i:02d}" in f and f.endswith(".mkv")]
        if not m:
            raise FileNotFoundError(f"Missing S01E{i:02d} for merging!")
        single_ep_paths.append(m[0])
        
    async def _merge_partition(out_path: str, eps: List[str]):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 500 * 1024 * 1024:
            LOGGER.info(f"Merged part already exists ({os.path.getsize(out_path)/(1024**2):.1f} MiB): {os.path.basename(out_path)}")
            return
            
        LOGGER.info(f"🔧 Merging {len(eps)} episodes into {os.path.basename(out_path)}...")
        cmd = [
            "mkvmerge",
            "--ui-language", "en_US",
            "-o", out_path,
            "--title", "[@Anime_Starfall🥰]",
            "--append-mode", "file",
            "--generate-chapters", "when-appending",
        ]
        if os.path.exists(cover_photo):
            cmd.extend([
                "--attachment-name", "cover.jpg",
                "--attachment-mime-type", "image/jpeg",
                "--attach-file", cover_photo,
            ])
            
        cmd.append(eps[0])
        for ep in eps[1:]:
            cmd.extend(["+", ep])
            
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode > 1:
            raise RuntimeError(f"mkvmerge error: {stderr.decode(errors='replace')}")
            
        LOGGER.info(f"✅ Merged successfully: {os.path.basename(out_path)} ({os.path.getsize(out_path)/(1024**2):.1f} MiB)")

    await _merge_partition(p1_path, single_ep_paths[0:6])
    await _merge_partition(p2_path, single_ep_paths[6:12])
    return p1_path, p2_path, single_ep_paths


async def main():
    LOGGER.info("======================================================================")
    LOGGER.info("🚀 STARTING SOLO LEVELING S01 ULTRA-FAST RELEASE PIPELINE")
    LOGGER.info(f"   Dump Channel: {DUMP_CHANNEL_ID} (Main Dump)")
    LOGGER.info(f"   Target Channel: {TARGET_CHANNEL_ID} (Tamil Muxed Collection)")
    LOGGER.info("   Bots: 3 Helper Tokens + 1 User String Client")
    LOGGER.info("======================================================================")

    # Step 1: Ensure episodes are muxed
    ok = await mux_season_1_if_needed()
    if not ok:
        LOGGER.error("Cannot proceed without muxed episodes.")
        sys.exit(1)

    # Step 2: Ensure 4GB-aware merged parts exist
    p1_path, p2_path, single_ep_paths = await create_merged_parts_if_needed()

    # Step 3: Prepare HD 16:9 Thumbnail (1280x720, Quality 95, <200KB)
    thumbs_dir = "/home/administrator/starfall/thumbnails"
    raw_thumb = find_anime_landscape_thumbnail("Solo Leveling", 1) or "/home/administrator/starfall/data/thumbnails/Solo_Leveling_S01_landscape.jpg"
    hd_thumb_path = os.path.join(thumbs_dir, "hd_landscape_Solo_Leveling_S01.jpg")
    master_hd_thumb = prepare_hd_thumbnail(raw_thumb, hd_thumb_path)
    LOGGER.info(f"🖼️ Master HD 16:9 Thumbnail ready: {master_hd_thumb} ({os.path.getsize(master_hd_thumb)} bytes)")

    # Step 4: Boot Clients
    sessions_dir = os.path.join(DATA_DIR, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    
    # 1 User Client
    user_client = Client(
        "hsin_moon_fox",
        api_id=TELEGRAM_API,
        api_hash=TELEGRAM_HASH,
        session_string=USER_SESSION_STRING,
        in_memory=True,
        workers=16,
        max_concurrent_transmissions=6,
    )
    await user_client.start()
    u_me = await user_client.get_me()
    LOGGER.info(f"✅ User Client online: @{u_me.username or 'Hsin_The_Moon_Fox'} (ID: {u_me.id})")

    # Exactly 3 Helper Bots as requested
    raw_tokens = [t.strip() for t in HELPER_TOKENS.split() if t.strip()][:3]
    LOGGER.info(f"🤖 Starting exactly {len(raw_tokens)} Helper Bots...")
    helper_bots: List[Client] = []
    for idx, tok in enumerate(raw_tokens, 1):
        bot = Client(
            f"starfall_helper_{idx}",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            bot_token=tok,
            workdir=sessions_dir,
            in_memory=False,
            workers=8,
            max_concurrent_transmissions=4,
        )
        await bot.start()
        b_me = await bot.get_me()
        LOGGER.info(f"✅ Helper Bot #{idx} online: @{b_me.username}")
        helper_bots.append(bot)

    # Step 5: Upload all 14 Assets to Dump Channel (-1003887117402)
    LOGGER.info("=" * 70)
    LOGGER.info(f"PHASE A: HIGH-SPEED PARALLEL UPLOAD -> MAIN DUMP ({DUMP_CHANNEL_ID})")
    LOGGER.info("=" * 70)

    dump_results: Dict[str, int] = {}
    ep_queue: asyncio.Queue = asyncio.Queue()
    for idx, ep_p in enumerate(single_ep_paths, 1):
        ep_queue.put_nowait((idx, ep_p))

    async def upload_video(client: Client, filepath: str, caption: str, tag: str):
        fname = os.path.basename(filepath)
        fsize_mib = os.path.getsize(filepath) / (1024 * 1024)
        meta = probe_file_meta(filepath)
        c_name = getattr(client, "name", "client")
        LOGGER.info(f"⬆️  [Dump] Uploading {fname} ({fsize_mib:.1f} MiB) via {c_name}...")
        t0 = time.time()
        for attempt in range(1, 5):
            try:
                msg = await client.send_video(
                    chat_id=DUMP_CHANNEL_ID,
                    video=filepath,
                    caption=caption,
                    thumb=master_hd_thumb,
                    video_cover=master_hd_thumb,
                    duration=meta["duration"],
                    width=meta["width"],
                    height=meta["height"],
                    supports_streaming=True,
                    parse_mode=ParseMode.HTML,
                )
                elapsed = time.time() - t0
                speed_mbps = (fsize_mib * 8) / max(elapsed, 0.01)
                LOGGER.info(f"✅ [Dump] Confirmed {fname} -> Msg #{msg.id} ({speed_mbps:.1f} Mbps)")
                dump_results[tag] = msg.id
                return msg.id
            except FloodWait as fw:
                wait_s = int(getattr(fw, "value", 15)) + 2
                LOGGER.warning(f"⚠️ FloodWait {wait_s}s on {c_name}. Sleeping...")
                await asyncio.sleep(wait_s)
            except RPCError as rpc:
                LOGGER.error(f"❌ RPC error on {c_name} for {fname}: {rpc}")
                await asyncio.sleep(5)
        raise RuntimeError(f"Failed to upload {filepath} after 4 attempts.")

    # 3 Bots handle 12 Single Episodes
    async def bot_worker(bot_client: Client):
        while not ep_queue.empty():
            try:
                ep_num, ep_path = ep_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            clean_name = os.path.basename(ep_path)
            await upload_video(
                client=bot_client,
                filepath=ep_path,
                caption=f"<b>{clean_name}</b>",
                tag=f"ep_{ep_num:02d}",
            )
            ep_queue.task_done()

    # 1 User Client handles the 2 Merged Parts
    async def user_worker():
        for p_tag, p_file in [("part_1", p1_path), ("part_2", p2_path)]:
            clean_name = os.path.basename(p_file)
            await upload_video(
                client=user_client,
                filepath=p_file,
                caption=f"<b>{clean_name}</b>",
                tag=p_tag,
            )

    workers = [user_worker()]
    for bot in helper_bots:
        workers.append(bot_worker(bot))

    await asyncio.gather(*workers)
    LOGGER.info("=" * 70)
    LOGGER.info(f"✅ ALL 14 ASSETS CONFIRMED IN MAIN DUMP ({DUMP_CHANNEL_ID})!")
    for k, v in sorted(dump_results.items()):
        LOGGER.info(f"   - {k}: https://t.me/c/{DUMP_CLEAN_ID}/{v}")
    LOGGER.info("=" * 70)

    # Step 6: Sequential Target Publishing (-1004415413346)
    LOGGER.info("=" * 70)
    LOGGER.info(f"PHASE B: SEQUENTIAL PUBLISHING -> TARGET CHANNEL ({TARGET_CHANNEL_ID})")
    LOGGER.info("=" * 70)

    target_links: List[Dict[str, Any]] = []

    # 1. Poster Card (Template 11)
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

    hero_card_thumb = os.path.join(thumbs_dir, "hero_card_Solo_Leveling_S01.jpg")
    poster_thumb = os.path.join(thumbs_dir, "poster_Solo_Leveling_S01.jpg")
    card_photo = hero_card_thumb if os.path.exists(hero_card_thumb) else (master_hd_thumb if os.path.exists(master_hd_thumb) else poster_thumb)

    LOGGER.info("📸 Step 1/15: Posting Season Poster Card (Template 11)...")
    card_msg: Message = await user_client.send_photo(
        chat_id=TARGET_CHANNEL_ID,
        photo=card_photo,
        caption=poster_caption,
        parse_mode=ParseMode.HTML,
    )
    p_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{card_msg.id}"
    target_links.append({"step": 1, "title": "Poster Card (Template 11)", "url": p_link})
    LOGGER.info(f"✅ Poster Card published -> Msg #{card_msg.id} ({p_link})")
    LOGGER.info(f"⏳ Waiting {POST_SPACING_SEC}s spacing before next release...")
    await asyncio.sleep(POST_SPACING_SEC)

    # 2. Merged Part 1
    LOGGER.info("📦 Step 2/15: Forwarding Merged Part 1 (E01-06)...")
    p1_m: Message = await user_client.copy_message(
        chat_id=TARGET_CHANNEL_ID,
        from_chat_id=DUMP_CHANNEL_ID,
        message_id=dump_results["part_1"],
    )
    p1_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{p1_m.id}"
    target_links.append({"step": 2, "title": "Merged Part 1 (E01-E06)", "url": p1_link})
    LOGGER.info(f"✅ Merged Part 1 published -> Msg #{p1_m.id} ({p1_link})")
    LOGGER.info(f"⏳ Waiting {POST_SPACING_SEC}s spacing before next release...")
    await asyncio.sleep(POST_SPACING_SEC)

    # 3. Merged Part 2
    LOGGER.info("📦 Step 3/15: Forwarding Merged Part 2 (E07-12)...")
    p2_m: Message = await user_client.copy_message(
        chat_id=TARGET_CHANNEL_ID,
        from_chat_id=DUMP_CHANNEL_ID,
        message_id=dump_results["part_2"],
    )
    p2_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{p2_m.id}"
    target_links.append({"step": 3, "title": "Merged Part 2 (E07-E12)", "url": p2_link})
    LOGGER.info(f"✅ Merged Part 2 published -> Msg #{p2_m.id} ({p2_link})")
    LOGGER.info(f"⏳ Waiting {POST_SPACING_SEC}s spacing before next release...")
    await asyncio.sleep(POST_SPACING_SEC)

    # 4. Single Episodes 01 through 12
    for ep_num in range(1, 13):
        step_idx = 3 + ep_num
        LOGGER.info(f"🎬 Step {step_idx}/15: Forwarding Episode {ep_num:02d}...")
        ep_m: Message = await user_client.copy_message(
            chat_id=TARGET_CHANNEL_ID,
            from_chat_id=DUMP_CHANNEL_ID,
            message_id=dump_results[f"ep_{ep_num:02d}"],
        )
        ep_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{ep_m.id}"
        target_links.append({"step": step_idx, "title": f"Episode {ep_num:02d}", "url": ep_link})
        LOGGER.info(f"✅ Episode {ep_num:02d} published -> Msg #{ep_m.id} ({ep_link})")
        if ep_num < 12:
            LOGGER.info(f"⏳ Waiting {POST_SPACING_SEC}s spacing before next release...")
            await asyncio.sleep(POST_SPACING_SEC)

    LOGGER.info("=" * 70)
    LOGGER.info("🎉 ALL 15 RELEASES SUCCESSFULLY PUBLISHED TO TARGET CHANNEL!")
    LOGGER.info("=" * 70)

    # Step 7: Post-Release Local NVMe Sweep
    LOGGER.info("PHASE C: POST-RELEASE LOCAL STORAGE SWEEP")
    for f_to_del in [p1_path, p2_path] + single_ep_paths:
        try:
            if os.path.exists(f_to_del):
                os.remove(f_to_del)
                LOGGER.info(f"🧹 Purged local release: {os.path.basename(f_to_del)}")
        except Exception as e:
            LOGGER.warning(f"Failed to delete {f_to_del}: {e}")

    # Stop clients
    for b in helper_bots:
        try:
            await b.stop()
        except Exception:
            pass
    try:
        await user_client.stop()
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("RELEASE PUBLISHING COMPLETE!")
    print("=" * 60)
    for item in target_links:
        print(f"[{item['step']}/15] {item['title']}: {item['url']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
