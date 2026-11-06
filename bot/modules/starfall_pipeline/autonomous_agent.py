#!/usr/bin/env python3
"""
Autonomous Starfall Media Engineering Agent
============================================
Executes Phase 1-4 anime sourcing, qBittorrent multi-gigabit downloading,
acoustic synchronization, and MKV multiplexing strictly according to PLAN.md.

Directives Enforced:
1. Completed Anime Only (Zero-Failure Execution):
   - Audits local Tamil Audio Bank (`downloads/vt_audio_bank/8814651149/`).
   - Strictly skips incomplete/missing seasons (e.g. Demon Slayer S01, Dr. Stone S01).
   - Strictly excludes Bleach Classic and Bleach TYBW.
2. Source Ingestion & Multiplexing:
   - Downloads clean 1080p x265 10-bit BluRay batches (Judas/Cleo/EMBER/Breeze) via local qBittorrent.
   - Sets Tamil audio as primary default:
     `--language 0:tam --track-name 0:"Tamil [Stereo]" --default-track-flag 0:yes`
   - Preserves original Japanese audio and all multi-subs intact.
   - Strict `BluRay` naming standard (never `BDRip`).
   - Matches or generates 16:9 widescreen landscape thumbnails.
3. Storage Ceiling & Immediate Cleanup:
   - Enforces hard 80 GB free disk buffer on `/`.
   - Deletes raw source video file immediately upon verified mux completion.
   - Saves muxed output in `downloads/muxed_output/`.
4. 1-Hour Autonomous Self-Healing Watchdog:
   - Runs diagnostics and remediation every 3600 seconds.
"""

import asyncio
import os
import re
import sys
import time
import shutil
import logging
from typing import Dict, Any, List, Optional, Tuple

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        BASE_DIR,
        DATA_DIR,
        DB_PATH,
        RAM_MUX_DIR,
    )
    from bot.modules.starfall_pipeline.qbittorrent_bridge import QBittorrentBridge
    from bot.modules.starfall_pipeline.nyaa_matcher import NyaaMatcher
    from bot.modules.starfall_pipeline.acoustic_sync import AcousticSyncEngine
    from bot.modules.starfall_pipeline.mux_engine import MuxEngine, sanitize_bluray_tag
    from bot.modules.starfall_pipeline.hourly_watchdog import HourlyWatchdog
else:
    from .config import (
        BASE_DIR,
        DATA_DIR,
        DB_PATH,
        RAM_MUX_DIR,
    )
    from .qbittorrent_bridge import QBittorrentBridge
    from .nyaa_matcher import NyaaMatcher
    from .acoustic_sync import AcousticSyncEngine
    from .mux_engine import MuxEngine, sanitize_bluray_tag
    from .hourly_watchdog import HourlyWatchdog

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] - %(message)s",
)
LOGGER = logging.getLogger("StarfallAutonomousAgent")

AUDIO_BANK_DIR = os.path.join(BASE_DIR, "downloads", "vt_audio_bank", "8814651149")
CLEAN_VIDEOS_HOST_DIR = os.path.join(BASE_DIR, "downloads", "clean_videos")
CLEAN_VIDEOS_CONTAINER_DIR = "/usr/src/app/downloads/clean_videos"
MUXED_OUTPUT_DIR = os.path.join(BASE_DIR, "downloads", "muxed_output")
THUMBNAILS_DIR = os.path.join(BASE_DIR, "thumbnails")

# Prioritized Phase 1 Completed Anime Catalogue
PHASE1_COMPLETED_CATALOGUE = [
    {
        "title": "Dan Da Dan",
        "aliases": ["dandadan", "dan da dan"],
        "season": 1,
        "total_eps": 12,
        "year": 2024,
    },
    {
        "title": "Kaiju No. 8",
        "aliases": ["kaiju no.8", "kaiju no 8", "kaiju"],
        "season": 1,
        "total_eps": 12,
        "year": 2024,
    },
    {
        "title": "Solo Leveling",
        "aliases": ["solo leveling"],
        "season": 1,
        "total_eps": 12,
        "year": 2024,
    },
    {
        "title": "Zom 100: Bucket List of the Dead",
        "aliases": ["zom 100", "zom100"],
        "season": 1,
        "total_eps": 12,
        "year": 2023,
    },
    {
        "title": "Mashle: Magic and Muscles",
        "aliases": ["mashle"],
        "season": 1,
        "total_eps": 12,
        "year": 2023,
    },
    {
        "title": "Classroom of the Elite",
        "aliases": ["classroom of the elite"],
        "season": 1,
        "total_eps": 12,
        "year": 2017,
    },
    {
        "title": "The Apothecary Diaries",
        "aliases": ["apothecary diaries", "kusuriya"],
        "season": 1,
        "total_eps": 24,
        "year": 2023,
    },
    {
        "title": "Death Note",
        "aliases": ["death note"],
        "season": 1,
        "total_eps": 37,
        "year": 2006,
    },
    {
        "title": "Frieren: Beyond Journey's End",
        "aliases": ["frieren"],
        "season": 1,
        "total_eps": 28,
        "year": 2023,
    },
    {
        "title": "Blue Box",
        "aliases": ["blue box"],
        "season": 1,
        "total_eps": 25,
        "year": 2024,
    },
    {
        "title": "Dead Mount Death Play",
        "aliases": ["dead mount death play"],
        "season": 1,
        "total_eps": 24,
        "year": 2023,
    },
    {
        "title": "Wistoria: Wand and Sword",
        "aliases": ["wistoria wand and sword", "wistoria"],
        "season": 1,
        "total_eps": 12,
        "year": 2024,
    },
    {
        "title": "SPY x FAMILY",
        "aliases": ["spy x family"],
        "season": 1,
        "total_eps": 25,
        "year": 2022,
    },
    {
        "title": "My Dress-Up Darling",
        "aliases": ["my dress-up darling", "my dress up darling"],
        "season": 1,
        "total_eps": 12,
        "year": 2022,
    },
    {
        "title": "Sword Art Online",
        "aliases": ["sword art online"],
        "season": 1,
        "total_eps": 25,
        "year": 2012,
    },
    {
        "title": "Vinland Saga",
        "aliases": ["vinland saga"],
        "season": 1,
        "total_eps": 24,
        "year": 2019,
    },
]


class StarfallAutonomousAgent:
    def __init__(self):
        self.qbt = QBittorrentBridge()
        self.nyaa = NyaaMatcher()
        self.sync_engine = AcousticSyncEngine()
        self.mux_engine = MuxEngine(workspace_dir=RAM_MUX_DIR)
        self.watchdog = HourlyWatchdog(qbt_bridge=self.qbt)
        self.completed_titles = 0
        self.current_phase = "Phase 1: Modern & Short-Form Releases"
        self._last_watchdog_ts = 0.0

        # Ensure directories
        os.makedirs(CLEAN_VIDEOS_HOST_DIR, exist_ok=True)
        os.makedirs(MUXED_OUTPUT_DIR, exist_ok=True)
        os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    def check_disk_space(self) -> Tuple[bool, float]:
        """Verify >= 80 GB free disk space buffer."""
        usage = shutil.disk_usage("/")
        free_gb = usage.free / (1024 ** 3)
        return free_gb >= 80.0, free_gb

    @staticmethod
    def parse_audio_season_ep(filename: str) -> Tuple[Optional[int], Optional[int]]:
        """Extract season and episode numbers from audio bank filename."""
        m = re.search(r"[Ss]0*(\d{1,2})[._\s-]*[Ee]0*(\d{1,3})", filename)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"\[[Ss]0*(\d{1,2})[Ee]0*(\d{1,3})\]", filename)
        if m:
            return int(m.group(1)), int(m.group(2))
        m_ep = re.search(r"\b(?:EP|Episode)[.\s_-]*0*(\d{1,3})\b", filename, re.IGNORECASE)
        if m_ep:
            return 1, int(m_ep.group(1))
        m_dash = re.search(r"[-_]\s*0*(\d{1,3})(?:\s*\(|\s*\[|\s*_|\s*-|\.|$)", filename)
        if m_dash:
            return 1, int(m_dash.group(1))
        return None, None

    def audit_audio_bank(self, anime_def: Dict[str, Any]) -> Tuple[bool, Dict[int, str]]:
        """
        Verify that 100% of the Tamil audio tracks for this anime season exist in the bank.
        Returns (is_complete, {ep_num: audio_filepath}).
        """
        if not os.path.exists(AUDIO_BANK_DIR):
            return False, {}

        files = []
        for root, _, fs in os.walk(AUDIO_BANK_DIR):
            for f in fs:
                files.append((f, os.path.join(root, f)))

        season = anime_def["season"]
        total_eps = anime_def["total_eps"]
        aliases = anime_def["aliases"]

        matched = {}
        for f, full_path in files:
            f_lower = f.lower()
            if any(alias.lower() in f_lower for alias in aliases):
                s, e = self.parse_audio_season_ep(f)
                if s == season and e and 1 <= e <= total_eps:
                    matched[e] = full_path

        is_complete = len(matched) == total_eps
        return is_complete, matched

    @staticmethod
    def parse_video_season_and_ep(filename: str, parent_path: str = "") -> Tuple[Optional[int], Optional[int]]:
        """Extract both season and episode numbers from video filename and parent path."""
        # 1. Match SxxExx in filename: e.g. S01E05 or S1E5
        m = re.search(r"[Ss]0*(\d{1,2})[._\s-]*[Ee]0*(\d{1,3})", filename)
        if m:
            return int(m.group(1)), int(m.group(2))

        # 2. Check season in parent directory or filename
        full_str = f"{parent_path}/{filename}"
        season = 1
        sm = re.search(r"\b(?:Season|S)[._\s-]*0*(\d{1,2})\b", full_str, re.IGNORECASE)
        if sm:
            season = int(sm.group(1))

        # Match EP xx or Episode xx
        m_ep = re.search(r"\b(?:EP|EPISODE)[.\s_-]*0*(\d{1,3})\b", filename, re.IGNORECASE)
        if m_ep:
            return season, int(m_ep.group(1))

        # Match ' - 01 ' or ' - 01.' or '_01_'
        m_dash = re.search(r"[-_]\s*0*(\d{1,3})(?:\s*\(|\s*\[|\s*_|\s*-|\.|$)", filename)
        if m_dash:
            return season, int(m_dash.group(1))

        # Standalone number surrounded by whitespace or brackets
        m_num = re.search(r"\s+0*(\d{1,3})\s+(?:\[|\()", filename)
        if m_num:
            return season, int(m_num.group(1))

        return season, None

    async def maybe_run_watchdog(self, next_anime: str):
        """Run 1-hour watchdog pulse if 3600 seconds elapsed."""
        now = time.time()
        if now - self._last_watchdog_ts >= 3600:
            LOGGER.info("Running 1-Hour Autonomous Watchdog Pulse...")
            self.watchdog.run_checkup(
                completed_count=self.completed_titles,
                current_phase=self.current_phase,
                next_anime=next_anime,
            )
            self._last_watchdog_ts = now

    async def download_batch(self, anime_def: Dict[str, Any], candidate: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """
        Download clean video batch via local qBittorrent into /usr/src/app/downloads/clean_videos/<slug>.
        Returns (host_save_dir, info_hash).
        """
        slug = re.sub(r"[^\w\s-]", "", anime_def["title"]).strip().replace(" ", "_").lower()
        slug = f"{slug}_s{anime_def['season']:02d}"
        container_save_dir = f"{CLEAN_VIDEOS_CONTAINER_DIR}/{slug}"
        host_save_dir = os.path.join(CLEAN_VIDEOS_HOST_DIR, slug)
        os.makedirs(host_save_dir, exist_ok=True)

        # Check if batch files are already downloaded on disk for THIS season
        existing_vids = set()
        if os.path.exists(host_save_dir):
            for root, _, files in os.walk(host_save_dir):
                for f in files:
                    if f.lower().endswith((".mkv", ".mp4")) and not any(x in f.upper() for x in ("NCOP", "NCED", "MENU", "SP", "PV")):
                        s_num, ep_num = self.parse_video_season_and_ep(f, parent_path=root)
                        if s_num == anime_def["season"] and ep_num and 1 <= ep_num <= anime_def["total_eps"]:
                            existing_vids.add(ep_num)
        if len(existing_vids) >= anime_def["total_eps"]:
            LOGGER.info(f"⚡ Batch files already present on disk ({len(existing_vids)}/{anime_def['total_eps']} eps): {host_save_dir}")
            return host_save_dir, candidate.get("info_hash")

        is_safe, free_gb = self.check_disk_space()
        if not is_safe:
            LOGGER.error(f"❌ Disk space safety tripped! Free: {free_gb:.1f} GB (< 80 GB). Halting ingestion.")
            return None, None

        magnet_or_url = candidate.get("magnet_uri") or candidate.get("torrent_url")
        if not magnet_or_url:
            LOGGER.error(f"No magnet or torrent URL for candidate {candidate.get('title')}")
            return None, None

        LOGGER.info(
            f"📥 Initiating qBittorrent download for '{anime_def['title']}' S{anime_def['season']:02d}\n"
            f"   Release: {candidate.get('title')}\n"
            f"   Size: {candidate.get('size')} | Seeders: {candidate.get('seeders')}\n"
            f"   Save Path: {host_save_dir}"
        )

        info_hash = self.qbt.add_torrent(
            magnet_or_url=magnet_or_url,
            container_save_path=container_save_dir,
            category="starfall",
            paused=False,
        )

        if not info_hash:
            LOGGER.error("Failed to add torrent to qBittorrent")
            return None, None

        LOGGER.info(f"Torrent submitted successfully. Hash: {info_hash}")

        # Poll download progress until 100% complete
        start_time = time.time()
        last_log_time = 0.0
        while True:
            await asyncio.sleep(5)
            await self.maybe_run_watchdog(next_anime=anime_def["title"])

            t_info = self.qbt.get_torrent(info_hash)
            if not t_info:
                LOGGER.warning(f"Could not fetch torrent info for {info_hash}")
                continue

            state = t_info.get("state", "")
            progress = t_info.get("progress", 0.0)
            dlspeed_mb = t_info.get("dlspeed", 0) / (1024 * 1024)
            seeds = t_info.get("num_seeds", 0)

            now = time.time()
            if now - last_log_time >= 30:
                LOGGER.info(
                    f"⏳ DL Status: {progress * 100:.1f}% | State: {state} | "
                    f"Speed: {dlspeed_mb:.2f} MB/s | Seeds: {seeds} | Elapsed: {int(now - start_time)}s"
                )
                last_log_time = now

            # Completed states in qBittorrent
            if progress >= 1.0 or state in ("uploading", "pausedUP", "stalledUP", "forcedUP", "complete"):
                LOGGER.info(f"✅ Batch download complete for '{anime_def['title']}'!")
                break

            # Stalling safeguard
            if (now - start_time > 1800) and (progress == 0.0 or (seeds == 0 and dlspeed_mb == 0)):
                LOGGER.warning(f"Torrent stalled for 30m without progress. Reannouncing...")
                self.qbt.reannounce(info_hash)

        return host_save_dir, info_hash

    async def process_anime(self, anime_def: Dict[str, Any]) -> bool:
        """
        Execute end-to-end processing for a single completed anime title:
        1. Audit Audio Bank for 100% completeness.
        2. Query Nyaa for clean 1080p BluRay batch.
        3. Download via qBittorrent.
        4. Mux episodes losslessly with Tamil default, Japanese audio preserved, multi-subs intact.
        5. Delete raw source video immediately after each mux.
        """
        title = anime_def["title"]
        season = anime_def["season"]
        total_eps = anime_def["total_eps"]
        year = anime_def.get("year", 2024)

        # Step 0: Check if already 100% muxed in MUXED_OUTPUT_DIR
        clean_title_sub = re.sub(r"[^\w]", "", title).lower()
        existing_muxes = []
        if os.path.exists(MUXED_OUTPUT_DIR):
            for f in os.listdir(MUXED_OUTPUT_DIR):
                if f.lower().endswith(".mkv") and f"s{season:02d}" in f.lower():
                    if clean_title_sub in re.sub(r"[^\w]", "", f).lower():
                        existing_muxes.append(f)
        if len(existing_muxes) >= total_eps:
            LOGGER.info(f"🎉 '{title}' S{season:02d} is ALREADY 100% muxed ({len(existing_muxes)}/{total_eps} eps present in muxed_output)! Skipping.")
            self.completed_titles += 1
            return True

        LOGGER.info(f"\n=======================================================\n"
                    f"🚀 STARTING PROCESSING: {title} S{season:02d} ({total_eps} eps)\n"
                    f"=======================================================")

        # Step 1: Audit Audio Bank
        is_complete, audio_map = self.audit_audio_bank(anime_def)
        if not is_complete:
            LOGGER.warning(
                f"⏭️ SKIPPING '{title}' S{season:02d}: Audio Bank incomplete "
                f"({len(audio_map)}/{total_eps} eps found). Directives require 100% completeness."
            )
            return False

        LOGGER.info(f"✅ Audio Bank Audit Passed: 100% complete ({len(audio_map)}/{total_eps} Tamil audio tracks present)")

        # Step 2: Query Nyaa for Multi-Source Batches
        batches = self.nyaa.find_multi_source_batches(title, season_number=season)
        if not batches:
            LOGGER.error(f"No batch torrent candidates found on Nyaa for '{title}' S{season:02d}")
            return False

        # Filter out Bleach or non-matching releases
        valid_candidates = []
        for b in batches:
            if "bleach" in b["title"].lower():
                continue
            valid_candidates.append(b)

        if not valid_candidates:
            LOGGER.error(f"No valid non-excluded batches found for '{title}'")
            return False

        candidate = valid_candidates[0]
        release_group = candidate.get("release_group", "Starfall")
        codec = candidate.get("codec", "x265")

        LOGGER.info(
            f"🎯 Selected Source Batch: [{release_group}] {candidate['title']}\n"
            f"   Seeders: {candidate['seeders']} | Size: {candidate['size']} | Codec: {codec}"
        )

        # Step 3: Download Clean Batch via qBittorrent
        download_dir, info_hash = await self.download_batch(anime_def, candidate)
        if not download_dir or not os.path.exists(download_dir):
            LOGGER.error(f"Download failed or directory missing for '{title}'")
            return False

        # Step 4: Scan Downloaded Files and Map Episodes
        found_videos = []
        for root, _, files in os.walk(download_dir):
            for f in sorted(files):
                if f.lower().endswith((".mkv", ".mp4")):
                    # Skip extras
                    if any(x in f.upper() for x in ("NCOP", "NCED", "MENU", "SP", "PV", "TRAILER", "OST", "SCANS")):
                        continue
                    s_num, ep_num = self.parse_video_season_and_ep(f, parent_path=root)
                    if s_num == season and ep_num and 1 <= ep_num <= total_eps:
                        found_videos.append((ep_num, os.path.join(root, f)))

        # Deduplicate to ensure each episode is mapped once
        ep_to_path = {}
        for ep_num, p in found_videos:
            if ep_num not in ep_to_path:
                ep_to_path[ep_num] = p
        video_files = sorted(ep_to_path.items(), key=lambda x: x[0])
        LOGGER.info(f"Found {len(video_files)} valid Season {season:02d} video episode files in batch directory.")

        if not video_files:
            LOGGER.error(f"No episode files could be parsed from {download_dir}")
            return False

        # Step 5: Mux Episodes Individually & Delete Source
        muxed_count = 0
        for ep_num, video_path in video_files:
            tamil_audio = audio_map.get(ep_num)
            if not tamil_audio or not os.path.exists(tamil_audio):
                LOGGER.error(f"Tamil audio missing for episode {ep_num}: {tamil_audio}")
                continue

            # Check disk buffer before muxing
            is_safe, free_gb = self.check_disk_space()
            if not is_safe:
                LOGGER.error(f"❌ Storage Safety Halt! Free disk space {free_gb:.1f} GB < 80 GB buffer.")
                return False

            # Format standardized filename (strictly BluRay)
            out_filename = self.mux_engine.format_output_filename(
                anime_name=title,
                season_num=season,
                episode_num=ep_num,
                year=year,
                resolution="1080p",
                bit="10bit",
                ott="CR",
                quality="BluRay",
                codec=codec,
                release_group=release_group,
            )
            out_mkv_path = os.path.join(MUXED_OUTPUT_DIR, out_filename)

            LOGGER.info(f"🎬 Muxing Episode {ep_num:02d}/{total_eps:02d}: {out_filename}")

            # Run Acoustic Sync (GCC-PHAT)
            sync_res = await self.sync_engine.analyze_sync(video_path, tamil_audio)
            calculated_delay = sync_res.get("calculated_delay_ms", 0)
            speed_ratio = sync_res.get("speed_ratio", 1.0)

            # Look for cached 16:9 thumbnail
            thumb_path = os.path.join(THUMBNAILS_DIR, f"{slug_hash(title)}_s{season:02d}e{ep_num:02d}.jpg")
            if not os.path.exists(thumb_path):
                # Generate 16:9 thumbnail dynamically at 20% mark
                prop = await self.mux_engine.probe_video_properties_async(video_path)
                await asyncio.to_thread(
                    self.mux_engine.generate_16_9_thumbnail,
                    video_path,
                    thumb_path,
                    prop.get("duration", 1440.0),
                )

            # Lossless MKV Multiplexing
            ok = await self.mux_engine.mux_single_episode(
                video_path=video_path,
                tamil_audio_path=tamil_audio,
                output_mkv_path=out_mkv_path,
                calculated_delay_ms=calculated_delay,
                speed_ratio=speed_ratio,
                release_group=release_group,
            )

            # Step 6: Verify Mux Output & Purge Source Video
            actual_mkv_path = out_mkv_path
            fallback_path = os.path.join(DATA_DIR, "saved_muxes", out_filename)
            if not os.path.exists(actual_mkv_path) and os.path.exists(fallback_path):
                try:
                    shutil.move(fallback_path, actual_mkv_path)
                except Exception:
                    actual_mkv_path = fallback_path

            if ok and os.path.exists(actual_mkv_path) and os.path.getsize(actual_mkv_path) > 50 * 1024 * 1024:
                file_sz_mb = os.path.getsize(actual_mkv_path) / (1024 * 1024)
                LOGGER.info(f"✅ Mux Verified ({file_sz_mb:.1f} MB): {out_filename}")
                
                # Delete source video immediately to free NVMe disk space
                try:
                    os.remove(video_path)
                    LOGGER.info(f"🗑️ Purged source video: {os.path.basename(video_path)}")
                except Exception as e:
                    LOGGER.warning(f"Could not delete source video {video_path}: {e}")

                muxed_count += 1
            else:
                LOGGER.error(f"❌ Mux failed for episode {ep_num}: {out_mkv_path}")

        # Clean up torrent in qBittorrent and delete empty source directory
        if info_hash:
            self.qbt.delete_torrent(info_hash, delete_files=False)
            LOGGER.info(f"Removed completed batch torrent {info_hash} from qBittorrent.")

        try:
            shutil.rmtree(download_dir, ignore_errors=True)
            LOGGER.info(f"Cleaned source directory: {download_dir}")
        except Exception:
            pass

        if muxed_count == len(video_files):
            LOGGER.info(f"🎉 Successfully completed 100% muxing for '{title}' S{season:02d} ({muxed_count} eps)!")
            self.completed_titles += 1
            return True
        else:
            LOGGER.warning(f"Partial muxing for '{title}': {muxed_count}/{len(video_files)} episodes succeeded.")
            return False

    async def run_pipeline(self, max_titles: Optional[int] = None):
        """Execute full autonomous pipeline over all completed Phase 1 titles."""
        LOGGER.info(
            f"🌟 Starting Starfall Autonomous Pipeline Loop\n"
            f"   Total Phase 1 Candidates: {len(PHASE1_COMPLETED_CATALOGUE)}\n"
            f"   NVMe Free Space: {shutil.disk_usage('/').free / (1024**3):.1f} GB\n"
            f"   Storage Safety Ceiling: > 80 GB free buffer enforced."
        )

        # Trigger initial watchdog pulse
        self.watchdog.run_checkup(
            completed_count=self.completed_titles,
            current_phase=self.current_phase,
            next_anime=PHASE1_COMPLETED_CATALOGUE[0]["title"],
        )
        self._last_watchdog_ts = time.time()

        processed = 0
        for anime_def in PHASE1_COMPLETED_CATALOGUE:
            if max_titles and processed >= max_titles:
                LOGGER.info(f"Reached max titles cap ({max_titles}). Halting run.")
                break

            await self.maybe_run_watchdog(next_anime=anime_def["title"])
            success = await self.process_anime(anime_def)
            if success:
                processed += 1


def slug_hash(title: str) -> str:
    """Generate clean slug for thumbnail naming."""
    return re.sub(r"[^\w]", "", title).lower()


if __name__ == "__main__":
    agent = StarfallAutonomousAgent()
    max_count = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(agent.run_pipeline(max_titles=max_count))
