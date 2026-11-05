import asyncio
import logging
import os
import re
import sys
import shutil
from typing import Dict, Any, List, Optional

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        HARVEST_CHANNEL_ID,
        TARGET_UPLOAD_CHANNEL_ID,
        AUDIO_VAULT_DIR,
        RAM_MUX_DIR,
        TG_SPLIT_THRESHOLD_MIB,
        TG_MAX_USER_LIMIT_MIB,
        USER_SETTINGS,
    )
    from bot.modules.starfall_pipeline.db import (
        init_db,
        get_or_create_season,
        get_audio_for_episode,
        record_episode_job,
        update_season_state,
        get_database_summary,
    )
    from bot.modules.starfall_pipeline.disk_guard import DiskQuotaGuard
    from bot.modules.starfall_pipeline.anilist_client import AniListClient
    from bot.modules.starfall_pipeline.nyaa_matcher import NyaaMatcher
    from bot.modules.starfall_pipeline.torrent_downloader import TorrentDownloader
    from bot.modules.starfall_pipeline.acoustic_sync import AcousticSyncEngine
    from bot.modules.starfall_pipeline.mux_engine import MuxEngine
    from bot.modules.starfall_pipeline.upload_router import UploadRouter
    from bot.modules.starfall_pipeline.harvester import harvest_channel
else:
    from .config import (
        HARVEST_CHANNEL_ID,
        TARGET_UPLOAD_CHANNEL_ID,
        AUDIO_VAULT_DIR,
        RAM_MUX_DIR,
        TG_SPLIT_THRESHOLD_MIB,
        TG_MAX_USER_LIMIT_MIB,
        USER_SETTINGS,
    )
    from .db import (
        init_db,
        get_or_create_season,
        get_audio_for_episode,
        record_episode_job,
        update_season_state,
        get_database_summary,
    )
    from .disk_guard import DiskQuotaGuard
    from .anilist_client import AniListClient
    from .nyaa_matcher import NyaaMatcher
    from .torrent_downloader import TorrentDownloader
    from .acoustic_sync import AcousticSyncEngine
    from .mux_engine import MuxEngine
    from .upload_router import UploadRouter
    from .harvester import harvest_channel

LOGGER = logging.getLogger("StarfallPipelineManager")

class StarfallPipelineManager:
    """
    Master Automated Pipeline Manager for Starfall Anime Muxing & Upload.
    Orchestrates Harvesting -> Nyaa Discovery -> Torrent Download ->
    4-Anchor GCC-PHAT Acoustic Sync -> Lossless Muxing -> Strict Tiered Upload.
    """

    def __init__(
        self,
        source_channel_id: int = HARVEST_CHANNEL_ID,
        target_channel_id: int = TARGET_UPLOAD_CHANNEL_ID,
    ):
        self.source_channel_id = source_channel_id
        self.target_channel_id = target_channel_id
        self.quota_guard = DiskQuotaGuard.get_instance()
        self.anilist = AniListClient()
        self.nyaa = NyaaMatcher(self.anilist)
        self.downloader = TorrentDownloader()
        self.sync_engine = AcousticSyncEngine()
        self.muxer = MuxEngine(RAM_MUX_DIR)
        self.router = UploadRouter(self.target_channel_id)

    async def process_series_season(
        self,
        anime_title: str,
        season_number: int = 1,
        max_episodes_to_process: Optional[int] = None,
        skip_upload: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute full Starfall Pipeline for an anime season:
        1. [Harvest]: Extract Tamil AAC audios from source channel -1004335327733.
        2. [Source Finder]: Translate anime names to Romaji via AniList & find Nyaa batch torrents.
        3. [Periodic Subtitle & Metadata]: Dynamic recurring promo subtitle & user_settings metadata.
        4. [Acoustic Sync & Mux]: 4-anchor GCC-PHAT delay sync & lossless muxing in RAM disk.
        5. [Duration & Thumbnails]: Probe exact duration & dimensions; generate STRICT 16:9 thumbnails.
        6. [Strict Upload Sequence to target channel]:
           - Step 1: Season Poster with Unicode frame caption & expandable synopsis.
           - Step 2: Full Season Merge video file (via @Hsin_The_Moon_Fox if > 1980 MiB).
           - Step 3: Single episodes in sequential order (S01E01, S01E02...) via 5 Helper Bots.
        7. Immediate local purge to keep storage quota <450 GB.
        """
        LOGGER.info(f"==================================================")
        LOGGER.info(f"▶ Starting Starfall Pipeline for: {anime_title} (Season {season_number})")
        LOGGER.info(f"  Source Channel: {self.source_channel_id}")
        LOGGER.info(f"  Target Channel: {self.target_channel_id}")
        LOGGER.info(f"==================================================")

        await init_db()

        # 1. AniList Metadata & Poster Resolution
        meta = self.anilist.search_anime(anime_title)
        romaji_title = meta.get("title", {}).get("romaji", anime_title) if meta else anime_title
        english_title = meta.get("title", {}).get("english", anime_title) if meta else anime_title
        year = meta.get("seasonYear", 2024) if meta else 2024
        genres_list = meta.get("genres", ["Anime"]) if meta else ["Anime"]
        genres_str = ", ".join(genres_list)
        rating_score = meta.get("averageScore")
        rating_str = f"{rating_score / 10:.1f}" if rating_score else "7.5"
        raw_synopsis = meta.get("description") or "Synopsis not available."
        clean_synopsis = re.sub(r"<.*?>", "", raw_synopsis).strip()
        if len(clean_synopsis) > 550:
            clean_synopsis = clean_synopsis[:547] + "..."

        LOGGER.info(f"AniList Resolved: Romaji='{romaji_title}', English='{english_title}', Year={year}")

        # Download official season poster
        thumbnails_dir = os.path.join(os.path.dirname(AUDIO_VAULT_DIR), "thumbnails")
        poster_path = self.anilist.download_season_poster(anime_title, season_number, output_dir=thumbnails_dir)
        LOGGER.info(f"Season Poster: {poster_path}")

        # Ensure anime season record in SQLite WAL DB
        season_id = await get_or_create_season(
            title_romaji=romaji_title,
            season_number=season_number,
            total_episodes=meta.get("episodes", 12) if meta else 12,
            season_state="PROCESSING",
        )

        # 2. Find Multi-Source Batches on Nyaa
        batches = self.nyaa.find_multi_source_batches(
            title_english=anime_title,
            season_number=season_number,
            max_hevc=4,
            max_av1=2,
        )

        if not batches:
            LOGGER.error(f"No suitable batch torrents found on Nyaa for {anime_title} S{season_number}")
            await update_season_state(season_id, "FAILED")
            return {"status": "FAILED", "reason": "No torrent batches found"}

        primary_source = batches[0]
        release_group = primary_source["release_group"]
        codec = primary_source["codec"]
        quality = "BluRay" if primary_source["is_bluray"] else "WEBRip"
        info_hash = primary_source["info_hash"]

        LOGGER.info(f"Selected Primary Source: [{release_group}] {primary_source['title']} ({quality}, {codec})")

        # 3. Check / Harvest Tamil Audios from Source Channel
        available_ep_nums = []
        for ep in range(1, 100):
            rec = await get_audio_for_episode(season_id, ep)
            if rec and rec.get("local_m4a_path") and os.path.exists(rec["local_m4a_path"]):
                available_ep_nums.append(ep)
            else:
                clean_slug = re.sub(r"[^\w\s-]", "", anime_title).strip().replace(" ", "_")
                candidate_audio = os.path.join(AUDIO_VAULT_DIR, f"{clean_slug}_S{season_number:02d}E{ep:02d}.m4a")
                if os.path.exists(candidate_audio):
                    available_ep_nums.append(ep)

        if not available_ep_nums:
            LOGGER.info(f"No cached Tamil audio found. Harvesting from source channel {self.source_channel_id}...")
            harvested = await harvest_channel(
                channel_id=self.source_channel_id,
                limit=max_episodes_to_process or 5,
                anime_name=anime_title,
            )
            # Recheck
            for ep in range(1, 100):
                rec = await get_audio_for_episode(season_id, ep)
                if rec and rec.get("local_m4a_path") and os.path.exists(rec["local_m4a_path"]):
                    available_ep_nums.append(ep)

        if not available_ep_nums:
            LOGGER.error(f"Failed to find or harvest Tamil audio for {anime_title} S{season_number}")
            await update_season_state(season_id, "FAILED")
            return {"status": "FAILED", "reason": "No Tamil audio available"}

        LOGGER.info(f"Available Tamil Audio Episodes: {available_ep_nums}")
        if max_episodes_to_process:
            available_ep_nums = available_ep_nums[:max_episodes_to_process]

        # 4. Download Torrent Batch for matched episodes
        downloaded_videos = await self.downloader.download_batch_torrent(
            primary_source,
            target_episodes=available_ep_nums
        )
        if not downloaded_videos:
            LOGGER.error(f"Failed to download torrent batch for {info_hash}")
            await update_season_state(season_id, "FAILED")
            return {"status": "FAILED", "reason": "Torrent download failed"}

        LOGGER.info(f"Downloaded {len(downloaded_videos)} episode videos.")

        # 5. Acoustic Sync & Lossless Muxing
        sorted_ep_nums = sorted(downloaded_videos.keys())
        if max_episodes_to_process:
            sorted_ep_nums = sorted_ep_nums[:max_episodes_to_process]

        muxed_episode_metadata: Dict[int, Dict[str, Any]] = {}
        muxed_episode_paths: List[str] = []

        for ep_num in sorted_ep_nums:
            video_file = downloaded_videos[ep_num]
            audio_record = await get_audio_for_episode(season_id, ep_num)

            tamil_audio_path = audio_record["local_m4a_path"] if audio_record else None
            if not tamil_audio_path or not os.path.exists(tamil_audio_path):
                clean_slug = re.sub(r"[^\w\s-]", "", anime_title).strip().replace(" ", "_")
                candidate_audio = os.path.join(AUDIO_VAULT_DIR, f"{clean_slug}_S{season_number:02d}E{ep_num:02d}.m4a")
                if os.path.exists(candidate_audio):
                    tamil_audio_path = candidate_audio

            if not tamil_audio_path or not os.path.exists(tamil_audio_path):
                LOGGER.warning(f"Skipping S{season_number:02d}E{ep_num:02d}: Tamil audio not found.")
                continue

            LOGGER.info(f"\n--- Muxing Episode {ep_num:02d} ---")
            LOGGER.info(f"Video: {os.path.basename(video_file)}")
            LOGGER.info(f"Audio: {os.path.basename(tamil_audio_path)}")

            # Probe video properties
            prop = self.muxer.probe_video_properties(video_file)
            dur_sec = prop["duration_sec"] or 1440
            width = prop["width"]
            height = prop["height"]

            # Generate STRICT 16:9 landscape thumbnail at 20% timestamp
            clean_slug = re.sub(r"[^\w\s-]", "", english_title).strip().replace(" ", "_")
            ep_thumb_path = os.path.join(thumbnails_dir, f"thumb_{clean_slug}_S{season_number:02d}E{ep_num:02d}.jpg")
            self.muxer.generate_16_9_thumbnail(video_file, ep_thumb_path, prop["duration"])

            # 4-Anchor GCC-PHAT Acoustic Delay Analysis
            sync_result = await self.sync_engine.analyze_sync(video_file, tamil_audio_path)
            calculated_delay = sync_result["calculated_delay_ms"]
            sync_mode = sync_result["sync_mode"]
            psr = sync_result["min_psr_score"]
            speed_ratio = sync_result["speed_ratio"]

            output_filename = self.muxer.format_output_filename(
                anime_name=english_title,
                season_num=season_number,
                episode_num=ep_num,
                year=year,
                resolution="1080p",
                bit="10bit",
                ott="CR",
                quality=quality,
                codec="x265" if codec == "HEVC" else codec,
                release_group=release_group,
            )
            output_mkv_path = os.path.join(RAM_MUX_DIR, output_filename)

            mux_ok = await self.muxer.mux_single_episode(
                video_path=video_file,
                tamil_audio_path=tamil_audio_path,
                output_mkv_path=output_mkv_path,
                calculated_delay_ms=calculated_delay,
                speed_ratio=speed_ratio,
                poster_path=poster_path,
                release_group=release_group,
                title_tag=output_filename,
            )

            if not mux_ok:
                LOGGER.error(f"Failed to mux S{season_number:02d}E{ep_num:02d}")
                continue

            muxed_episode_paths.append(output_mkv_path)
            muxed_episode_metadata[ep_num] = {
                "path": output_mkv_path,
                "thumb_path": ep_thumb_path,
                "duration_sec": dur_sec,
                "width": width,
                "height": height,
                "sync_mode": sync_mode,
                "delay_ms": calculated_delay,
                "psr": psr,
                "speed_ratio": speed_ratio,
                "audio_id": audio_record["audio_id"] if audio_record else None,
                "raw_video": video_file,
                "telemetry": sync_result["anchor_telemetry"],
            }

        # 6. Season Merge (if 2 or more episodes muxed)
        merged_files_data = []
        if len(muxed_episode_paths) >= 2:
            clean_slug = re.sub(r"[^\w\s-]", "", english_title).strip().replace(" ", "_")
            merged_output_path = os.path.join(
                RAM_MUX_DIR,
                f"🄰🅂-[Season_{season_number:02d}_Complete] {clean_slug} 1080p 10bit {quality} {codec} [Tamil-AAC 2.0] ~ [@Anime_Starfall🥰].mkv"
            )
            created_merge_parts = await self.muxer.merge_season(
                muxed_episode_paths,
                merged_output_path,
                poster_path=poster_path
            )
            for m_path in created_merge_parts:
                m_prop = self.muxer.probe_video_properties(m_path)
                m_thumb = os.path.join(thumbnails_dir, f"thumb_{clean_slug}_S{season_number:02d}_Merge.jpg")
                self.muxer.generate_16_9_thumbnail(m_path, m_thumb, m_prop["duration"])
                merged_files_data.append({
                    "path": m_path,
                    "thumb_path": m_thumb,
                    "duration_sec": m_prop["duration_sec"],
                    "width": m_prop["width"],
                    "height": m_prop["height"],
                })

        # 7. EXECUTE STRICT UPLOAD SEQUENCE TO TARGET CHANNEL (-1004415413346)
        if skip_upload:
            LOGGER.info("Upload skipped by request.")
            return {"status": "SUCCESS", "season_id": season_id, "muxed_episodes": len(muxed_episode_paths)}

        await self.router.start()
        uploaded_msg_ids: List[int] = []

        try:
            # ========================================================
            # STEP 1: Send Season Poster with user_settings.json
            #         Unicode frame caption & expandable synopsis
            # ========================================================
            LOGGER.info(f"--- STEP 1: Sending Season Poster to {self.target_channel_id} ---")
            caption_template = USER_SETTINGS.get("POST_ANIME_CAPTION", "")
            if not caption_template:
                caption_template = (
                    "<b>「 {title} - {year} 」</b>\n━━━━━━━━━━━━━━━━━━\n"
                    "╔════◇═══════════◇════\n"
                    "║ Season ➤ {season}\n"
                    "║ AniList ➤ {rating} Rating\n"
                    "║ Genres ➤ {genres}\n"
                    "║ Quality ➤ {resolution} {bit} {codec}\n"
                    "║ Audio ➤ Tamil {audio_codec} {audio_channels} ~ {shortsub}\n"
                    "╚════◇═══════════◇════\n"
                    " <blockquote expandable>Synopsis :\n   {plot}</blockquote>\n\n"
                    "Dropped By ➤ [@Anime_Starfall🥰]"
                )

            poster_caption = caption_template.format(
                title=english_title,
                year=year,
                season=season_number,
                rating=rating_str,
                genres=genres_str,
                resolution="1080p",
                bit="10bit",
                codec="x265" if codec == "HEVC" else codec,
                audio_codec="AAC",
                audio_channels="2.0",
                shortsub="ESub",
                plot=clean_synopsis,
            )

            poster_msg_id = await self.router.upload_photo(
                photo_path=poster_path,
                caption=poster_caption,
            )
            if poster_msg_id:
                uploaded_msg_ids.append(poster_msg_id)
                LOGGER.info(f"✅ STEP 1 COMPLETE: Season Poster posted (Msg #{poster_msg_id})")

            # ========================================================
            # STEP 2: Send Full Season Merge video file with landscape
            #         thumbnail & probed duration (via User if > 1980 MiB)
            # ========================================================
            if merged_files_data:
                LOGGER.info(f"--- STEP 2: Sending Full Season Merge to {self.target_channel_id} ---")
                ep_range_str = f"E01 - E{max(sorted_ep_nums):02d}"
                merge_caption = (
                    f"📦 <b>「 {english_title} - Season {season_number:02d} Complete Season Merge 」</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"╔════◇═══════════◇════\n"
                    f"║ Episodes ➤ {ep_range_str} (Chapters Included)\n"
                    f"║ Quality ➤ 1080p 10bit {quality} {codec} ~ [{release_group}]\n"
                    f"║ Audio ➤ Tamil AAC 2.0 [Lossless Muxed]\n"
                    f"║ Subtitles ➤ English + [@Anime_Starfall🥰] Promo\n"
                    f"╚════◇═══════════◇════\n\n"
                    f"🌟 <b>Channel:</b> [@Anime_Starfall🥰]"
                )

                for m_part in merged_files_data:
                    m_msg_id = await self.router.upload_file(
                        filepath=m_part["path"],
                        caption=merge_caption,
                        thumb_path=m_part["thumb_path"],
                        duration_sec=m_part["duration_sec"],
                        width=m_part["width"],
                        height=m_part["height"],
                        cleanup_after_upload=True,  # Purge immediately to maintain quota
                    )
                    if m_msg_id:
                        uploaded_msg_ids.append(m_msg_id)
                        LOGGER.info(f"✅ STEP 2 COMPLETE: Season Merge uploaded (Msg #{m_msg_id})")

            # ========================================================
            # STEP 3: Send single episodes in sequential order
            #         (S01E01, S01E02...) via the 5 Helper Bots
            # ========================================================
            LOGGER.info(f"--- STEP 3: Sending Single Episodes in Sequential Order to {self.target_channel_id} ---")
            for ep_num in sorted_ep_nums:
                ep_data = muxed_episode_metadata.get(ep_num)
                if not ep_data:
                    continue

                ep_caption = (
                    f"🎬 <b>{english_title} - S{season_number:02d}E{ep_num:02d}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Quality:</b> 1080p 10bit {quality} {codec} ~ [{release_group}]\n"
                    f"<b>Audio:</b> Tamil AAC 2.0 (Lossless Synced)\n"
                    f"<b>Subtitles:</b> English + Periodic Channel Promo\n"
                    f"<b>Sync Confidence:</b> PSR {ep_data['psr']:.1f} ({ep_data['sync_mode']})\n\n"
                    f"🌟 <b>Channel:</b> [@Anime_Starfall🥰]"
                )

                ep_msg_id = await self.router.upload_file(
                    filepath=ep_data["path"],
                    caption=ep_caption,
                    thumb_path=ep_data["thumb_path"],
                    duration_sec=ep_data["duration_sec"],
                    width=ep_data["width"],
                    height=ep_data["height"],
                    cleanup_after_upload=True,  # Purge immediately to maintain quota
                )

                if ep_msg_id:
                    uploaded_msg_ids.append(ep_msg_id)
                    LOGGER.info(f"✅ Uploaded S{season_number:02d}E{ep_num:02d} -> Msg #{ep_msg_id}")

                # Record in SQLite DB
                job_id = await record_episode_job(
                    season_id=season_id,
                    episode_number=ep_num,
                    audio_id=ep_data["audio_id"],
                    bd_relative_path=ep_data["raw_video"],
                    sync_mode=ep_data["sync_mode"],
                    calculated_delay_ms=ep_data["delay_ms"],
                    speed_ratio=ep_data["speed_ratio"],
                    min_psr_score=ep_data["psr"],
                    anchor_telemetry=ep_data["telemetry"],
                    job_state="COMPLETED",
                    tg_single_msg_id=ep_msg_id,
                    uploaded_by_bot=f"HelperBot (Msg #{ep_msg_id})" if ep_msg_id else None,
                )
                LOGGER.info(f"Recorded Episode Job #{job_id} in SQLite DB.")

            # 8. Quota Governance: Cleanup downloaded batch and RAM scratch
            await self.downloader.cleanup_batch(info_hash)
            DiskQuotaGuard.cleanup_ram_scratch(older_than_sec=0)
            await update_season_state(season_id, "COMPLETED")

            LOGGER.info(
                f"🎉 SUCCESS! Completed Starfall Pipeline for {anime_title} Season {season_number:02d}! "
                f"Total uploads to {self.target_channel_id}: {len(uploaded_msg_ids)} messages."
            )

            return {
                "status": "SUCCESS",
                "season_id": season_id,
                "target_channel": self.target_channel_id,
                "episodes_processed": len(muxed_episode_paths),
                "uploaded_msg_ids": uploaded_msg_ids,
            }

        finally:
            await self.router.stop()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Starfall Pipeline Manager")
    parser.add_argument("--anime", type=str, default="A Couple of Cuckoos", help="Anime title")
    parser.add_argument("--season", type=int, default=1, help="Season number")
    parser.add_argument("--limit", type=int, default=0, help="Episode limit")
    parser.add_argument("--source-channel", type=int, default=HARVEST_CHANNEL_ID, help="Source Channel ID")
    parser.add_argument("--target-channel", type=int, default=TARGET_UPLOAD_CHANNEL_ID, help="Target Channel ID")
    args = parser.parse_args()

    async def _main():
        manager = StarfallPipelineManager(
            source_channel_id=args.source_channel,
            target_channel_id=args.target_channel,
        )
        res = await manager.process_series_season(
            args.anime,
            season_number=args.season,
            max_episodes_to_process=(args.limit if args.limit > 0 else None)
        )
        print("Pipeline Execution Result:", res)

    asyncio.run(_main())
