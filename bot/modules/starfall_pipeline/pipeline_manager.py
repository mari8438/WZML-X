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
        DEFAULT_TAMIL_CHANNEL_ID,
        AUDIO_VAULT_DIR,
        RAM_MUX_DIR,
        TG_SPLIT_THRESHOLD_MIB,
        TG_MAX_USER_LIMIT_MIB,
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
        DEFAULT_TAMIL_CHANNEL_ID,
        AUDIO_VAULT_DIR,
        RAM_MUX_DIR,
        TG_SPLIT_THRESHOLD_MIB,
        TG_MAX_USER_LIMIT_MIB,
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
    4-Anchor GCC-PHAT Acoustic Sync -> Lossless Muxing -> Tiered Upload.
    """

    def __init__(self, channel_id: int = DEFAULT_TAMIL_CHANNEL_ID):
        self.channel_id = channel_id
        self.quota_guard = DiskQuotaGuard.get_instance()
        self.anilist = AniListClient()
        self.nyaa = NyaaMatcher(self.anilist)
        self.downloader = TorrentDownloader()
        self.sync_engine = AcousticSyncEngine()
        self.muxer = MuxEngine(RAM_MUX_DIR)
        self.router = UploadRouter(self.channel_id)

    async def process_series_season(
        self,
        anime_title: str,
        season_number: int = 1,
        max_episodes_to_process: Optional[int] = None,
        skip_upload: bool = False,
    ) -> Dict[str, Any]:
        """
        Process a complete anime season through the automated pipeline:
        1. Query AniList for metadata & download high-res official season poster.
        2. Find multi-source batch torrents on Nyaa (BD > WebRip, HEVC/AV1 ranking).
        3. Download video batch with aria2c (skipping extras/NCOP/NCED).
        4. Match with Tamil AAC audio from library.
        5. Run 4-anchor GCC-PHAT acoustic sync.
        6. Losslessly mux with mkvmerge (drop Japanese audio, keep English subs, inject [@Anime_Starfall🥰] intro).
        7. Tiered upload: single episodes via 5 Helper Bots, season merge via Premium User.
        8. Immediate quota cleanup.
        """
        LOGGER.info(f"==================================================")
        LOGGER.info(f"▶ Starting Starfall Pipeline for: {anime_title} (Season {season_number})")
        LOGGER.info(f"==================================================")

        # 1. AniList Metadata & Poster Resolution
        meta = self.anilist.search_anime(anime_title)
        romaji_title = meta.get("title", {}).get("romaji", anime_title) if meta else anime_title
        english_title = meta.get("title", {}).get("english", anime_title) if meta else anime_title
        year = meta.get("seasonYear", 2024) if meta else 2024

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

        # Select primary video source
        primary_source = batches[0]
        release_group = primary_source["release_group"]
        codec = primary_source["codec"]
        quality = "BDRip" if primary_source["is_bluray"] else "WEBRip"
        info_hash = primary_source["info_hash"]

        LOGGER.info(f"Selected Primary Source: [{release_group}] {primary_source['title']} ({quality}, {codec})")

        # 3. Determine which episodes have Tamil audio in library
        # Query all available audio episodes for this season from DB or vault
        available_ep_nums = []
        for ep in range(1, 25):
            rec = await get_audio_for_episode(season_id, ep)
            if rec and rec.get("local_m4a_path") and os.path.exists(rec["local_m4a_path"]):
                available_ep_nums.append(ep)
            else:
                clean_slug = re.sub(r"[^\w\s-]", "", anime_title).strip().replace(" ", "_")
                candidate_audio = os.path.join(AUDIO_VAULT_DIR, f"{clean_slug}_S{season_number:02d}E{ep:02d}.m4a")
                if os.path.exists(candidate_audio):
                    available_ep_nums.append(ep)

        if not available_ep_nums:
            LOGGER.error(f"No harvested Tamil audio files found for {anime_title} Season {season_number}")
            await update_season_state(season_id, "FAILED")
            return {"status": "FAILED", "reason": "No harvested Tamil audio"}

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

        # 4. Synchronize, Mux, and Upload Episodes
        sorted_ep_nums = sorted(downloaded_videos.keys())
        if max_episodes_to_process:
            sorted_ep_nums = sorted_ep_nums[:max_episodes_to_process]

        muxed_episode_paths: List[str] = []
        uploaded_msg_ids: List[int] = []

        if not skip_upload:
            await self.router.start()

        try:
            for ep_num in sorted_ep_nums:
                video_file = downloaded_videos[ep_num]
                audio_record = await get_audio_for_episode(season_id, ep_num)

                # Check if audio exists in library
                tamil_audio_path = audio_record["local_m4a_path"] if audio_record else None
                if not tamil_audio_path or not os.path.exists(tamil_audio_path):
                    # Check fallback name in AUDIO_VAULT_DIR
                    clean_slug = re.sub(r"[^\w\s-]", "", anime_title).strip().replace(" ", "_")
                    candidate_audio = os.path.join(AUDIO_VAULT_DIR, f"{clean_slug}_S{season_number:02d}E{ep_num:02d}.m4a")
                    if os.path.exists(candidate_audio):
                        tamil_audio_path = candidate_audio

                if not tamil_audio_path or not os.path.exists(tamil_audio_path):
                    LOGGER.warning(f"Skipping S{season_number:02d}E{ep_num:02d}: Tamil audio not found in vault.")
                    continue

                LOGGER.info(f"\n--- Processing Episode {ep_num:02d} ---")
                LOGGER.info(f"Video: {os.path.basename(video_file)}")
                LOGGER.info(f"Audio: {os.path.basename(tamil_audio_path)}")

                # A. 4-Anchor GCC-PHAT Acoustic Delay Analysis
                sync_result = await self.sync_engine.analyze_sync(video_file, tamil_audio_path)
                calculated_delay = sync_result["calculated_delay_ms"]
                sync_mode = sync_result["sync_mode"]
                psr = sync_result["min_psr_score"]
                speed_ratio = sync_result["speed_ratio"]

                LOGGER.info(
                    f"Acoustic Sync Result: mode={sync_mode}, delay={calculated_delay} ms, "
                    f"PSR={psr:.1f}, speed={speed_ratio}"
                )

                # B. Muxing in RAM disk (/dev/shm)
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

                # C. Upload via 5 Helper Bots
                tg_msg_id = None
                if not skip_upload:
                    caption = (
                        f"🎬 **{english_title} - S{season_number:02d}E{ep_num:02d}**\n"
                        f"🔊 **Audio:** Tamil AAC 2.0 (Lossless Muxed)\n"
                        f"🎞 **Quality:** 1080p 10bit {quality} {codec} ~ [{release_group}]\n"
                        f"✨ **Sync Confidence:** PSR {psr:.1f} ({sync_mode})\n\n"
                        f"🌟 **Channel:** @Anime_Starfall 🥰"
                    )
                    tg_msg_id = await self.router.upload_file(
                        filepath=output_mkv_path,
                        caption=caption,
                        thumb_path=poster_path,
                        cleanup_after_upload=False,  # Keep briefly until season merge or batch cleanup
                    )
                    if tg_msg_id:
                        uploaded_msg_ids.append(tg_msg_id)

                # D. Log job record to SQLite
                audio_id = audio_record["audio_id"] if audio_record else None
                job_id = await record_episode_job(
                    season_id=season_id,
                    episode_number=ep_num,
                    audio_id=audio_id,
                    bd_relative_path=video_file,
                    sync_mode=sync_mode,
                    calculated_delay_ms=calculated_delay,
                    speed_ratio=speed_ratio,
                    min_psr_score=psr,
                    anchor_telemetry=sync_result["anchor_telemetry"],
                    job_state="COMPLETED",
                    tg_single_msg_id=tg_msg_id,
                    uploaded_by_bot=f"HelperBot (Msg #{tg_msg_id})" if tg_msg_id else None,
                )
                LOGGER.info(f"Recorded Episode Job #{job_id} in SQLite DB.")

            # 5. Full Season Merge (if multiple episodes available)
            if len(muxed_episode_paths) >= 2 and not skip_upload:
                clean_slug = re.sub(r"[^\w\s-]", "", english_title).strip().replace(" ", "_")
                merged_output_path = os.path.join(
                    RAM_MUX_DIR,
                    f"🄰🅂-[Season_{season_number:02d}_Complete] {clean_slug} 1080p 10bit {quality} {codec} [Tamil-AAC 2.0] ~ [@Anime_Starfall🥰].mkv"
                )
                merged_parts = await self.muxer.merge_season(muxed_episode_paths, merged_output_path)
                for part in merged_parts:
                    merge_caption = (
                        f"📦 **{english_title} Season {season_number:02d} Complete Season Merge**\n"
                        f"🔊 **Audio:** Tamil AAC 2.0\n"
                        f"🎞 **Quality:** 1080p 10bit {quality} {codec}\n\n"
                        f"🌟 **Channel:** @Anime_Starfall 🥰"
                    )
                    merge_msg_id = await self.router.upload_file(
                        filepath=part,
                        caption=merge_caption,
                        thumb_path=poster_path,
                        cleanup_after_upload=True,
                    )
                    LOGGER.info(f"Uploaded Season Merge Part: Msg #{merge_msg_id}")

            # 6. Cleanup muxed files and downloaded raw batch to preserve quota
            for p in muxed_episode_paths:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

            await self.downloader.cleanup_batch(info_hash)
            await update_season_state(season_id, "COMPLETED")
            LOGGER.info(f"🎉 Completed processing for {anime_title} Season {season_number:02d}!")

            return {
                "status": "SUCCESS",
                "season_id": season_id,
                "episodes_processed": len(muxed_episode_paths),
                "uploads": uploaded_msg_ids,
            }

        finally:
            if not skip_upload:
                await self.router.stop()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Starfall Pipeline Manager")
    parser.add_argument("--anime", type=str, default="A Couple of Cuckoos", help="Anime title")
    parser.add_argument("--season", type=int, default=1, help="Season number")
    parser.add_argument("--limit", type=int, default=5, help="Episode limit")
    args = parser.parse_args()

    async def _main():
        manager = StarfallPipelineManager()
        res = await manager.process_series_season(args.anime, season_number=args.season, max_episodes_to_process=args.limit)
        print("Pipeline Execution Result:", res)

    asyncio.run(_main())
