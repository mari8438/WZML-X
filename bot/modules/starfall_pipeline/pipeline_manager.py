#!/usr/bin/env python3
"""
Starfall v1.6 Master Work-Tree Orchestrator (`pipeline_manager.py`)
===================================================================
Target Deadline : 48 Hours for ~3,000 Episodes across 54 Series (>= 62.5 eps/hr)
Hardware Profile: 16 vCPU (Ubuntu 24.04, Kernel 7.0), 32 GB RAM,
                  16 GB tmpfs (`/dev/shm/starfall_mux`), 500 GB Dedicated NVMe,
                  1 Gbps Unmetered Port.

Core Subsystems Implemented in this Module:
1. `AutoRenameResolver`:
   - Resolves `{quality}` (`BluRay` or `WEBRip` — strictly rewrites any `BDRip`, `BD-Rip`,
     `BRRip`, or `BD` to `BluRay`) and `{release_group}` across a 3-tier hierarchy:
     (1) Leaf video filename -> (2) Parent batch directory path -> (3) `.torrent` batch title.
2. `SlidingWindowQuotaGuard` (500 GB NVMe Pre-Fetch & Watermark Controller):
   - Keeps active NVMe utilization between 80% (400 GB Low-Watermark) and 90%
     (450 GB High-Watermark) with a hard 25 GB safety buffer.
   - Supports incremental byte release (`release_episode_bytes`) as individual raw
     episodes are purged post-upload, immediately waking blocked pre-fetchers via
     `asyncio.Condition`.
3. `StarfallPipelineManager` (5-Stage Async Producer-Consumer Work-Tree):
   - Stage 1 (`harvest_q`, 5 workers): Scans `-1004335327733`, filters out merged files
     (`duration > 2400s`), demuxes Tamil AAC (`-c:a copy`) via `/dev/shm`, extracts
     16:9 widescreen logo thumbnails (`msg.video.thumbs[-1]`), and records Season Poster IDs.
   - Stage 2 (`prefetch_q`, 4 concurrent torrent slots): Pre-fetches complete Nyaa BluRay/WebRip
     batches into the 500 GB NVMe buffer whenever usage drops below 450 GB.
   - Stage 3 (`sync_q`, 6 workers): Runs 4-anchor (20%, 40%, 60%, 80%) Mid/Side vocal-stripped
     GCC-PHAT cross-correlation in memory (`pipe:1` float32 8 kHz PCM, zero disk I/O).
   - Stage 4 (`mux_q`, 4 workers in `/dev/shm/starfall_mux`): Losslessly muxes 1080p video +
     synced Tamil AAC + English subtitles + periodic 5-min `[@Anime_Starfall🥰]` promo track,
     followed by `mkvmerge --append-mode file` full-season merge (auto-split > 3,950 MiB).
   - Stage 5 (`upload_q`, 6 clients via `UploadRouter`): Executes strict 3-Step upload
     (Step 1 Poster -> Step 2 Full Season Merge -> Step 3 Sequential Single Episodes)
     with immediate post-upload NVMe & tmpfs purge hooks.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from PIL import Image

try:
    from bot.helper.poster_engine.template_eleven import _template_eleven
except ImportError:
    _template_eleven = None

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        AUDIO_VAULT_DIR,
        DATA_DIR,
        DB_PATH,
        HARVEST_CHANNEL_ID,
        RAM_MUX_DIR,
        TARGET_UPLOAD_CHANNEL_ID,
        TG_MAX_USER_LIMIT_MIB,
        TG_SPLIT_THRESHOLD_MIB,
        USER_SETTINGS,
    )
    from bot.modules.starfall_pipeline.db import (
        get_audio_for_episode,
        get_database_summary,
        get_or_create_season,
        init_db,
        record_episode_job,
        update_season_state,
    )
    from bot.modules.starfall_pipeline.anilist_client import AniListClient
    from bot.modules.starfall_pipeline.nyaa_matcher import NyaaMatcher
    from bot.modules.starfall_pipeline.torrent_downloader import TorrentDownloader, TORRENTS_BASE_DIR
    from bot.modules.starfall_pipeline.acoustic_sync import AcousticSyncEngine
    from bot.modules.starfall_pipeline.mux_engine import MuxEngine
    from bot.modules.starfall_pipeline.upload_router import UploadRouter
    from bot.modules.starfall_pipeline.harvester import harvest_channel
else:
    from .config import (
        AUDIO_VAULT_DIR,
        DATA_DIR,
        DB_PATH,
        HARVEST_CHANNEL_ID,
        RAM_MUX_DIR,
        TARGET_UPLOAD_CHANNEL_ID,
        TG_MAX_USER_LIMIT_MIB,
        TG_SPLIT_THRESHOLD_MIB,
        USER_SETTINGS,
    )
    from .db import (
        get_audio_for_episode,
        get_database_summary,
        get_or_create_season,
        init_db,
        record_episode_job,
        update_season_state,
    )
    from .anilist_client import AniListClient
    from .nyaa_matcher import NyaaMatcher
    from .torrent_downloader import TorrentDownloader, TORRENTS_BASE_DIR
    from .acoustic_sync import AcousticSyncEngine
    from .mux_engine import MuxEngine
    from .upload_router import UploadRouter
    from .harvester import harvest_channel

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] - %(message)s",
)
LOGGER = logging.getLogger("StarfallPipelineManager")

# ============================================================================
# 1. HARDWARE CONCURRENCY & STORAGE WATERMARK CONSTANTS (16 vCPU / 32 GB RAM)
# ============================================================================
STAGE1_HARVEST_WORKERS: int = 5         # Concurrent Telegram audio & 16:9 thumb demuxers
STAGE2_TORRENT_PREFETCH_SLOTS: int = 4  # Simultaneous batch downloads on 1 Gbps port
STAGE3_ACOUSTIC_SYNC_WORKERS: int = 6   # Sub-2s 4-Anchor GCC-PHAT workers (CPU/FFT bound)
STAGE4_RAM_MUX_WORKERS: int = 4         # Concurrent mkvmerge jobs in 16 GB /dev/shm
STAGE5_SEASON_UPLOAD_SLOTS: int = 2     # Overlapping season upload pipelines (6 clients)

RAM_TMPFS_MAX_ACTIVE_EPISODES: int = 4  # Strictly bounds active concurrent mkvmerge tasks in 16 GB tmpfs

DEDICATED_NVME_QUOTA_GB: float = 500.0
HIGH_WATERMARK_GB: float = 450.0        # 90% of 500 GB quota -> pause new pre-fetches
LOW_WATERMARK_GB: float = 400.0         # 80% of 500 GB quota -> resume pre-fetching immediately
HARD_SAFETY_FLOOR_GB: float = 25.0      # Minimum physical free space on root NVMe


# ============================================================================
# 2. AUTO-RENAME RESILIENCE ENGINE (3-Tier Hierarchy + Strict `BluRay` Rule)
# ============================================================================
class AutoRenameResolver:
    """
    Resolves `{quality}`, `{release_group}`, `{codec}`, `{resolution}`, `{bit}`, and `{ott}`
    even when leaf video filenames inside batch torrents are stripped (e.g.
    `A Couple of Cuckoos - S01E01.mkv` inside `[neoHEVC] A Couple of Cuckoos [Season 1] [BD 1080p x265]`).

    Enforces the Starfall v1.6 Naming Rule:
      - NEVER outputs `BDRip`, `BD-Rip`, `BRRip`, or `BD`.
      - Strictly normalizes any Blu-ray indicator to **`BluRay`**, and Web sources to **`WEBRip`**.
    """

    KNOWN_ENCODERS = (
        "YURASUKA", "DB", "animencodes", "Judas", "DKB", "neoHEVC", "sam", "SeaDex",
        "ASW", "EMBER", "Tenrai-Sensei", "Tenrai", "Anime Time", "AnimeTime",
        "Breeze", "LastBreeze", "Ironclad", "Sokudo", "Trix", "NekoTrix",
        "neoDESU", "Lumen", "Anime-AV1", "omnipotent500", "SavI0r-Pro", "SavI0r",
    )

    BLURAY_REGEX = re.compile(
        r"(?:^|[\s\[\(\._\-])(BD(?:Rip)?|BD-Rip|BRRip|Blu-?Ray|BDRemux)(?:$|[\s\]\)\._\-])",
        re.IGNORECASE,
    )
    WEBRIP_REGEX = re.compile(
        r"(?:^|[\s\[\(\._\-])(WEB-?DL|WEBRip|WEB-?Rip|WEB)(?:$|[\s\]\)\._\-])",
        re.IGNORECASE,
    )
    OTT_REGEX = re.compile(
        r"(?:^|[\s\[\(\._\-])(CR|Crunchyroll|NF|Netflix|AMZN|Amazon|DSNP|HIDI|B-Global)(?:$|[\s\]\)\._\-])",
        re.IGNORECASE,
    )
    LeadingBracketGroup = re.compile(r"^\s*\[([A-Za-z0-9_.\-\s]+?)\]")
    TrailingGroup = re.compile(
        r"[-~]\s*([A-Za-z0-9_.\-]+?)(?:\s*(?:\[[^\]]*\]|\([^)]*\)))*\s*(?:\.(?:mkv|mp4|webm))?$",
        re.IGNORECASE,
    )

    @classmethod
    def sanitize_bluray_keyword(cls, text: str) -> str:
        """Strictly replace any variation of BDRip / BD-Rip / BRRip with `BluRay`, and remove the CR word."""
        res = re.sub(r"\b(?:BDRip|BD-Rip|BRRip|Blu-Ray|BDRemux)\b", "BluRay", text, flags=re.IGNORECASE)
        res = re.sub(r"\bCR\b\s*", "", res)
        return re.sub(r"\s+", " ", res).strip()

    @classmethod
    def resolve_metadata(
        cls,
        leaf_video_path: str,
        torrent_batch_title: str = "",
        nyaa_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """
        Inspect (1) Leaf filename -> (2) Parent folder name(s) -> (3) `.torrent` batch title
        -> (4) `nyaa_metadata` dict to guarantee zero `Unknown` or `BDRip` tags.
        """
        nyaa_metadata = nyaa_metadata or {}
        leaf_name = os.path.basename(leaf_video_path)
        parent_dir = os.path.basename(os.path.dirname(leaf_video_path))
        grandparent_dir = os.path.basename(os.path.dirname(os.path.dirname(leaf_video_path)))

        # Ordered hierarchy from most specific to broadest batch context
        context_layers = [
            leaf_name,
            parent_dir,
            grandparent_dir,
            torrent_batch_title,
            str(nyaa_metadata.get("title", "")),
        ]

            # 1. Resolve Quality (`BluRay` vs `WEBRip`)
        quality = ""
        for layer in context_layers:
            if not layer:
                continue
            if cls.WEBRIP_REGEX.search(layer):
                quality = "WEBRip"
                break
            if cls.BLURAY_REGEX.search(layer):
                quality = "BluRay"
                break
        if not quality:
            quality = "BluRay" if nyaa_metadata.get("is_bluray", True) else "WEBRip"

        # 2. Resolve Release Group (`{release_group}`)
        release_group = ""
        # Check known priority encoder groups first across all layers
        for layer in context_layers:
            if not layer:
                continue
            for grp in cls.KNOWN_ENCODERS:
                if re.search(rf"(?:^|[\W_]){re.escape(grp)}(?:$|[\W_])", layer, re.IGNORECASE):
                    release_group = grp
                    break
            if release_group:
                break

        # Fallback to bracketed `[Group]` or trailing `-Group` across layers
        if not release_group:
            for layer in context_layers:
                if not layer or len(layer) == 40:  # Skip 40-char info_hash folder names
                    continue
                m_lead = cls.LeadingBracketGroup.search(layer)
                if m_lead:
                    cand = m_lead.group(1).strip()
                    if not re.match(r"^(?:1080p|720p|BD|BluRay|Season\s*\d+|S\d+|Dual\s*Audio|Batch)$", cand, re.I):
                        release_group = cand
                        break
                m_trail = cls.TrailingGroup.search(layer)
                if m_trail:
                    cand = m_trail.group(1).strip()
                    if len(cand) >= 2 and not cand.isdigit():
                        release_group = cand
                        break

        if not release_group or release_group.lower() == "unknown":
            fallback_grp = nyaa_metadata.get("release_group", "")
            release_group = fallback_grp if (fallback_grp and fallback_grp.lower() != "unknown") else "Starfall"

        # 3. Resolve Video Codec (`x265`, `AV1`, `x264`)
        codec = ""
        for layer in context_layers:
            u = (layer or "").upper()
            if "AV1" in u:
                codec = "AV1"
                break
            if any(k in u for k in ("X265", "HEVC", "H.265", "H265")):
                codec = "x265"
                break
            if any(k in u for k in ("X264", "AVC", "H.264", "H264")):
                codec = "x264"
                break
        if not codec:
            raw_c = str(nyaa_metadata.get("codec", "HEVC")).upper()
            codec = "x265" if raw_c in ("HEVC", "UNKNOWN") else raw_c

        # 4. Resolve Bit Depth, Resolution, OTT
        combined = " ".join(context_layers)
        bit = "8bit" if re.search(r"\b8[-\s]?bit\b", combined, re.I) else "10bit"
        res_m = re.search(r"\b(2160p|1080p|720p|480p)\b", combined, re.I)
        resolution = res_m.group(1).lower() if res_m else "1080p"
        ott_m = cls.OTT_REGEX.search(combined)
        ott_raw = ott_m.group(1).upper() if ott_m else "CR"
        ott = "CR" if ott_raw in ("CR", "CRUNCHYROLL") else ("NF" if ott_raw in ("NF", "NETFLIX") else ott_raw)

        return {
            "quality": "BluRay" if quality.upper().startswith("BD") or quality.upper() == "BLURAY" else "WEBRip",
            "release_group": release_group,
            "codec": codec,
            "bit": bit,
            "resolution": resolution,
            "ott": ott,
        }

    @classmethod
    def format_episode_filename(
        cls,
        anime_name: str,
        season_num: int,
        episode_num: int,
        year: int,
        resolved_meta: Dict[str, str],
    ) -> str:
        """
        Produce canonical Starfall episode filename (as edited by user):
        `🄰🅂- [S01E01] A Couple of Cuckoos (2022) 1080p 10bit BluRay HEVC [Tamil-AAC 2.0] ESub ~ neoHEVC ~ [@Anime_Starfall🥰].mkv`
        """
        clean_name = re.sub(r"[^\w\s-]", "", anime_name).strip()
        quality = cls.sanitize_bluray_keyword(resolved_meta.get("quality", "BluRay"))
        if quality.lower() in ("bdrip", "bd-rip", "brrip", "bluray", "bdremux", "bd"):
            quality = "BluRay"
        codec_raw = resolved_meta.get("codec", "HEVC")
        codec = "HEVC" if codec_raw.lower() in ("x265", "hevc", "h.265", "h265") else codec_raw
        bit = resolved_meta.get("bit", "10bit")
        resolution = resolved_meta.get("resolution", "1080p")
        group = resolved_meta.get("release_group", "Starfall")
        year_str = f"({year})" if year else ""

        raw_fn = (
            f"🄰🅂- [S{season_num:02d}E{episode_num:02d}] {clean_name} {year_str} "
            f"{resolution} {bit} {quality} {codec} [Tam + Jap AAC 2.0] ESub ~ {group} ~ [@Anime_Starfall🥰].mkv"
        )
        raw_fn = re.sub(r"\s+", " ", raw_fn).strip()
        return cls.sanitize_bluray_keyword(raw_fn)

    @classmethod
    def format_season_merge_filename(
        cls,
        anime_name: str,
        season_num: int,
        ep_start: int,
        ep_end: int,
        resolved_meta: Dict[str, str],
        year: Optional[int] = None,
    ) -> str:
        """
        Produce canonical Starfall Full-Season Merge filename:
        `📦 🄰🅂- [S1E01-02] A Couple of Cuckoos (2022) 1080p 10bit BluRay HEVC [Tamil-AAC 2.0] ESub ~ neoHEVC ~ [@Anime_Starfall🥰].mkv`
        """
        clean_name = re.sub(r"[^\w\s-]", "", anime_name).strip()
        quality = cls.sanitize_bluray_keyword(resolved_meta.get("quality", "BluRay"))
        if quality.lower() in ("bdrip", "bd-rip", "brrip", "bluray", "bdremux", "bd"):
            quality = "BluRay"
        codec_raw = resolved_meta.get("codec", "HEVC")
        codec = "HEVC" if codec_raw.lower() in ("x265", "hevc", "h.265", "h265") else codec_raw
        bit = resolved_meta.get("bit", "10bit")
        resolution = resolved_meta.get("resolution", "1080p")
        group = resolved_meta.get("release_group", "Starfall")
        year_str = f"({year})" if year else ""
        s_tag = f"S{season_num}" if season_num < 10 else f"S{season_num:02d}"

        raw_fn = (
            f"📦 🄰🅂- [{s_tag}E{ep_start:02d}-{ep_end:02d}] {clean_name} {year_str} "
            f"{resolution} {bit} {quality} {codec} [Tam + Jap AAC 2.0] ESub ~ {group} ~ [@Anime_Starfall🥰].mkv"
        )
        raw_fn = re.sub(r"\s+", " ", raw_fn).strip()
        return cls.sanitize_bluray_keyword(raw_fn)

    @classmethod
    def plan_merge_chunks(
        cls,
        episodes: List[Tuple[int, str]],
        max_chunk_bytes: int = 3900 * 1024 * 1024,
    ) -> List[Dict[str, Any]]:
        """
        4GB Below Merge Planner:
        Greedily groups discrete, complete episodes into chunks <= max_chunk_bytes (default 3900 MB).
        Never splits across mid-episode bytes!
        """
        valid_episodes = [(ep_n, p) for ep_n, p in episodes if os.path.exists(p)]
        if not valid_episodes:
            return []

        chunks: List[Dict[str, Any]] = []
        cur_eps: List[Tuple[int, str]] = []
        cur_bytes = 0

        for ep_n, ep_p in valid_episodes:
            sz = os.path.getsize(ep_p)
            if cur_eps and (cur_bytes + sz > max_chunk_bytes):
                chunks.append({
                    "ep_start": cur_eps[0][0],
                    "ep_end": cur_eps[-1][0],
                    "episode_paths": [p for _, p in cur_eps],
                    "total_bytes": cur_bytes,
                })
                cur_eps = [(ep_n, ep_p)]
                cur_bytes = sz
            else:
                cur_eps.append((ep_n, ep_p))
                cur_bytes += sz

        if cur_eps:
            chunks.append({
                "ep_start": cur_eps[0][0],
                "ep_end": cur_eps[-1][0],
                "episode_paths": [p for _, p in cur_eps],
                "total_bytes": cur_bytes,
            })

        return chunks


# ============================================================================
# 3. 500 GB NVMe SLIDING-WINDOW QUOTA CONTROLLER (`SlidingWindowQuotaGuard`)
# ============================================================================
class SlidingWindowQuotaGuard:
    """
    Byte-Accurate NVMe Storage Reservation & Pre-Fetch Watermark Controller:
    - Target Buffer Window: 400 GB (80% Low-Watermark) to 450 GB (90% High-Watermark)
      of the 500 GB dedicated NVMe storage.
    - Note: Because episode muxing and full-season merging execute inside `/dev/shm/starfall_mux`
      (16 GB RAM disk) or stream out with immediate per-episode deletion, NVMe overhead per
      torrent batch is `1.05 * torrent_bytes` (raw video files + 5% metadata/index overhead).
    - As each individual episode is muxed and uploaded, `release_episode_bytes(raw_bytes)`
      immediately decrements the reservation and notifies waiting pre-fetch workers via
      `asyncio.Condition`, keeping the 1 Gbps pipe continuously saturated.
    """

    _instance: Optional["SlidingWindowQuotaGuard"] = None

    def __init__(
        self,
        torrents_dir: str = TORRENTS_BASE_DIR,
        audio_vault_dir: str = AUDIO_VAULT_DIR,
        high_watermark_gb: float = HIGH_WATERMARK_GB,
        low_watermark_gb: float = LOW_WATERMARK_GB,
        safety_floor_gb: float = HARD_SAFETY_FLOOR_GB,
    ):
        self.torrents_dir = torrents_dir
        self.audio_vault_dir = audio_vault_dir
        self.high_watermark_bytes = int(high_watermark_gb * (1024 ** 3))
        self.low_watermark_bytes = int(low_watermark_gb * (1024 ** 3))
        self.safety_floor_bytes = int(safety_floor_gb * (1024 ** 3))

        self._cond = asyncio.Condition()
        self._reserved_batches: Dict[str, int] = {}
        self._prefetch_paused: bool = False

        # Separate Semaphore for 16 GB /dev/shm RAM workspace backpressure
        self.ram_episode_sem = asyncio.Semaphore(RAM_TMPFS_MAX_ACTIVE_EPISODES)

    @classmethod
    def get_instance(cls) -> "SlidingWindowQuotaGuard":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _dir_bytes(path: str) -> int:
        if not os.path.exists(path):
            return 0
        total = 0
        for root, _, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                if not os.path.islink(fp):
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        return total

    def get_telemetry(self) -> Dict[str, float]:
        """Return live NVMe and `/dev/shm` utilization metrics in GB."""
        disk_torrents = self._dir_bytes(self.torrents_dir)
        disk_audio = self._dir_bytes(self.audio_vault_dir)
        in_flight_reserved = sum(self._reserved_batches.values())
        # Effective committed footprint is physical audio vault + max(physical torrents, reserved in-flight)
        effective_bytes = disk_audio + max(disk_torrents, in_flight_reserved)
        try:
            root_free = shutil.disk_usage("/").free
        except Exception:
            root_free = 100 * (1024 ** 3)
        try:
            shm_free = shutil.disk_usage(RAM_MUX_DIR).free
        except Exception:
            shm_free = 16 * (1024 ** 3)

        return {
            "effective_committed_gb": round(effective_bytes / (1024 ** 3), 2),
            "physical_torrents_gb": round(disk_torrents / (1024 ** 3), 2),
            "physical_audio_gb": round(disk_audio / (1024 ** 3), 2),
            "reserved_in_flight_gb": round(in_flight_reserved / (1024 ** 3), 2),
            "root_free_gb": round(root_free / (1024 ** 3), 2),
            "shm_free_gb": round(shm_free / (1024 ** 3), 2),
            "utilization_pct_of_500gb": round((effective_bytes / (500.0 * (1024 ** 3))) * 100.0, 1),
        }

    async def acquire_batch_reservation(self, info_hash: str, raw_torrent_bytes: int) -> int:
        """
        Block until reserving `1.05 * raw_torrent_bytes` fits within the 450 GB High-Watermark
        and respects the 25 GB physical root free space floor.
        """
        required_bytes = int(raw_torrent_bytes * 1.05)

        async with self._cond:
            while True:
                disk_audio = self._dir_bytes(self.audio_vault_dir)
                in_flight = sum(self._reserved_batches.values())
                effective_used = disk_audio + in_flight

                try:
                    root_free = shutil.disk_usage("/").free
                except Exception:
                    root_free = 100 * (1024 ** 3)

                # Hysteresis check: if paused at High-Watermark (450 GB), wait until usage drops <= Low-Watermark (400 GB)
                if self._prefetch_paused and effective_used <= self.low_watermark_bytes:
                    self._prefetch_paused = False
                    LOGGER.info(
                        f"🟢 [DiskQuotaGuard] NVMe usage dropped to {effective_used / (1024**3):.1f} GB "
                        f"(<= 400 GB Low-Watermark). Resuming 1 Gbps pre-fetchers!"
                    )

                projected = effective_used + required_bytes
                has_quota = (not self._prefetch_paused) and (projected <= self.high_watermark_bytes)
                has_physical = (root_free - required_bytes) >= self.safety_floor_bytes

                if has_quota and has_physical:
                    self._reserved_batches[info_hash] = required_bytes
                    new_total_gb = (effective_used + required_bytes) / (1024 ** 3)
                    if new_total_gb >= (self.high_watermark_bytes / (1024 ** 3)) * 0.98:
                        self._prefetch_paused = True
                    LOGGER.info(
                        f"💾 [DiskQuotaGuard] Reserved {required_bytes / (1024**3):.2f} GB for batch {info_hash[:10]} | "
                        f"Committed: {new_total_gb:.2f} / 450.0 GB ({(new_total_gb / 5.0):.1f}% of 500 GB quota)"
                    )
                    return required_bytes

                self._prefetch_paused = True
                LOGGER.info(
                    f"⏸️ [DiskQuotaGuard] High-Watermark reached (Committed={effective_used/(1024**3):.1f} GB, "
                    f"Need={required_bytes/(1024**3):.1f} GB). Waiting for episode upload purges..."
                )
                await self._cond.wait()

    async def release_episode_bytes(self, info_hash: str, purged_bytes: int) -> None:
        """Incrementally release NVMe quota as each individual episode's raw video is deleted."""
        async with self._cond:
            if info_hash in self._reserved_batches:
                credited = int(purged_bytes * 1.05)
                self._reserved_batches[info_hash] = max(0, self._reserved_batches[info_hash] - credited)
            self._cond.notify_all()

    async def release_batch(self, info_hash: str) -> None:
        """Release any remaining reservation for `info_hash` and wake pre-fetch workers."""
        async with self._cond:
            self._reserved_batches.pop(info_hash, None)
            self._cond.notify_all()


# ============================================================================
# 4. PIPELINE WORK-TREE DATA STRUCTURES
# ============================================================================
@dataclass
class SeasonWorkItem:
    anime_title: str
    season_number: int = 1
    season_id: int = 0
    romaji_title: str = ""
    english_title: str = ""
    year: int = 2024
    genres_str: str = "Anime"
    rating_str: str = "7.8"
    clean_synopsis: str = ""
    poster_path: Optional[str] = None
    poster_card_path: Optional[str] = None
    source_poster_msg_id: Optional[int] = None
    harvested_thumb_path: Optional[str] = None
    primary_source: Dict[str, Any] = field(default_factory=dict)
    info_hash: str = ""
    available_episodes: List[int] = field(default_factory=list)
    downloaded_videos: Dict[int, str] = field(default_factory=dict)
    muxed_episodes: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    merged_season_parts: List[Dict[str, Any]] = field(default_factory=list)


# ============================================================================
# 5. MASTER ASYNC PRODUCER-CONSUMER PIPELINE ORCHESTRATOR
# ============================================================================
class StarfallPipelineManager:
    """
    Master 48-Hour Work-Tree Orchestrator for the 54-Series (~3,000 Episodes) Catalogue.
    Supports both:
      - `run_catalogue_48h(series_catalogue)`: Full 5-stage sliding-window producer-consumer
        execution across all 54 series with continuous torrent pre-fetching.
      - `process_series_season(...)`: Drop-in compatible single-season execution method.
    """

    def __init__(
        self,
        source_channel_id: int = HARVEST_CHANNEL_ID,
        target_channel_id: int = TARGET_UPLOAD_CHANNEL_ID,
    ):
        self.source_channel_id = source_channel_id
        self.target_channel_id = target_channel_id

        self.quota_guard = SlidingWindowQuotaGuard.get_instance()
        self.anilist = AniListClient()
        self.nyaa = NyaaMatcher(self.anilist)
        self.downloader = TorrentDownloader()
        self.sync_engine = AcousticSyncEngine()
        self.muxer = MuxEngine(RAM_MUX_DIR)
        self.router = UploadRouter(
            target_channel_id=self.target_channel_id,
            source_channel_id=self.source_channel_id,
        )

        # Stage-level concurrency semaphores tuned for 16 vCPUs & 32 GB RAM
        self.harvest_sem = asyncio.Semaphore(STAGE1_HARVEST_WORKERS)
        self.prefetch_sem = asyncio.Semaphore(STAGE2_TORRENT_PREFETCH_SLOTS)
        self.sync_sem = asyncio.Semaphore(STAGE3_ACOUSTIC_SYNC_WORKERS)
        self.mux_sem = asyncio.Semaphore(STAGE4_RAM_MUX_WORKERS)
        self.upload_season_sem = asyncio.Semaphore(STAGE5_SEASON_UPLOAD_SLOTS)

        self.thumbnails_dir = os.path.join(DATA_DIR, "thumbnails")
        os.makedirs(self.thumbnails_dir, exist_ok=True)

    @staticmethod
    def find_audio_in_vault(anime_title: str, season_num: int, ep_num: int) -> Optional[str]:
        """
        Scans both tam_audios and vt_audio_bank (with strict movie exclusion).
        """
        search_dirs = [
            AUDIO_VAULT_DIR,
            os.path.join(os.path.dirname(DATA_DIR), "downloads", "vt_audio_bank"),
            "/home/administrator/starfall/downloads/vt_audio_bank",
        ]
        clean_title = re.sub(r"[^\w\s]", " ", anime_title).lower().strip()
        title_words = set(clean_title.split())
        is_vigilantes = "vigilantes" in title_words
        movie_keywords = ["movie", "two heroes", "heroes rising", "world heroes", "you're next", "youre next"]
        ep_pattern = re.compile(rf"\bS0*{season_num}\s*E0*{ep_num}\b", re.IGNORECASE)

        stopwords = {"a", "an", "the", "in", "on", "of", "to", "for", "and", "no", "na", "ni", "ga", "wa", "season", "part"}
        sig_words = [w for w in title_words if len(w) > 2 and w not in stopwords]

        for base_dir in search_dirs:
            if not os.path.exists(base_dir):
                continue
            for root, _, files in os.walk(base_dir):
                for f in sorted(files):
                    if not f.endswith((".m4a", ".opus", ".aac", ".ac3", ".mp3")):
                        continue
                    f_lower = f.lower()
                    if any(k in f_lower for k in movie_keywords):
                        continue
                    if is_vigilantes and "vigilantes" not in f_lower:
                        continue
                    if not is_vigilantes and "vigilantes" in f_lower:
                        continue
                    if sig_words:
                        matches = [w in f_lower for w in sig_words]
                        if not all(matches) and not (len(matches) > 1 and sum(matches) >= 2):
                            continue
                    if ep_pattern.search(f):
                        return os.path.join(root, f)
        return None

    @staticmethod
    def parse_nyaa_size_bytes(size_str: str) -> int:
        """Convert Nyaa RSS size string (e.g. '14.2 GiB', '850 MiB') to exact bytes."""
        if not size_str:
            return 10 * (1024 ** 3)
        m = re.match(r"^\s*([\d.]+)\s*([KMGT]i?B)\s*$", size_str.strip(), re.I)
        if not m:
            return 10 * (1024 ** 3)
        val = float(m.group(1))
        unit = m.group(2).upper()
        mult = {
            "KIB": 1024, "KB": 1000,
            "MIB": 1024 ** 2, "MB": 1000 ** 2,
            "GIB": 1024 ** 3, "GB": 1000 ** 3,
            "TIB": 1024 ** 4, "TB": 1000 ** 4,
        }.get(unit, 1024 ** 3)
        return int(val * mult)

    async def _resolve_stage1_metadata_and_audio(
        self,
        item: SeasonWorkItem,
        max_episodes: Optional[int] = None,
    ) -> bool:
        """
        Stage 1 Worker:
        - Resolves canonical Romaji & English titles via AniList GraphQL.
        - Queries Nyaa for complete 1080p batch torrents (BluRay > WebRip, ranked HEVC/AV1).
        - Verifies or harvests Tamil AAC audio (`-c:a copy` in `/dev/shm`) and 16:9 landscape
          logo thumbnails (`msg.video.thumbs[-1]`) from `-1004335327733`.
        """
        async with self.harvest_sem:
            meta = await asyncio.to_thread(self.anilist.search_anime, item.anime_title)
            item.romaji_title = meta.get("title", {}).get("romaji", item.anime_title) if meta else item.anime_title
            item.english_title = meta.get("title", {}).get("english") or item.anime_title if meta else item.anime_title
            item.year = meta.get("seasonYear", 2024) if meta else 2024
            genres_list = meta.get("genres", ["Anime"]) if meta else ["Anime"]
            item.genres_str = ", ".join(genres_list)
            rating_score = meta.get("averageScore") if meta else None
            item.rating_str = f"{rating_score / 10:.1f}" if rating_score else "7.8"

            raw_synopsis = (meta.get("description") if meta else None) or "Synopsis not available."
            clean_syn = re.sub(r"<.*?>", "", raw_synopsis).strip()
            item.clean_synopsis = (clean_syn[:547] + "...") if len(clean_syn) > 550 else clean_syn

            item.poster_path = await asyncio.to_thread(
                self.anilist.download_season_poster,
                item.anime_title,
                item.season_number,
                self.thumbnails_dir,
            )

            # Generate Template 11 Hero Card (1280x720 Widescreen) for Post #1
            if item.poster_path and os.path.exists(item.poster_path) and _template_eleven is not None:
                try:
                    clean_slug = re.sub(r"[^\w\s-]", "", item.anime_title).strip().replace(" ", "_")
                    t11_target = os.path.join(self.thumbnails_dir, f"hero_card_{clean_slug}_S{item.season_number:02d}.jpg")
                    card_data = {
                        "title": item.english_title or item.anime_title,
                        "season": item.season_number,
                        "year": item.year,
                        "rating": item.rating_str,
                        "genres": item.genres_str,
                        "plot": item.clean_synopsis,
                        "quality": "1080p BluRay",
                        "audio": "Tamil AAC 2.0",
                        "episodes": (meta.get("episodes") or 12) if meta else 12,
                    }
                    card_img = await asyncio.to_thread(_template_eleven, item.poster_path, item.poster_path, card_data)
                    card_img.convert("RGB").save(t11_target, "JPEG", quality=95)
                    if os.path.exists(t11_target) and os.path.getsize(t11_target) > 5000:
                        item.poster_card_path = t11_target
                        LOGGER.info(f"🎨 Generated Template 11 Hero Card for Post #1: {item.poster_card_path}")
                except Exception as e:
                    LOGGER.warning(f"Could not render Template 11 hero card: {e}")

            item.season_id = await get_or_create_season(
                title_romaji=item.romaji_title,
                season_number=item.season_number,
                total_episodes=(meta.get("episodes") or 12) if meta else 12,
                season_state="HARVESTING",
            )

            # Check harvested 16:9 logo thumbnail from source channel
            clean_slug = re.sub(r"[^\w\s-]", "", item.anime_title).strip().replace(" ", "_")
            candidate_harvest_thumb = os.path.join(
                self.thumbnails_dir, f"{clean_slug}_S{item.season_number:02d}_thumb.jpg"
            )
            if os.path.exists(candidate_harvest_thumb):
                item.harvested_thumb_path = candidate_harvest_thumb

            # Discover Nyaa Batch Torrents
            batches = await asyncio.to_thread(
                self.nyaa.find_multi_source_batches,
                item.anime_title,
                item.season_number,
                4,
                2,
            )
            if not batches:
                LOGGER.error(f"[Stage 1] No Nyaa batch found for {item.anime_title} S{item.season_number:02d}")
                await update_season_state(item.season_id, "FAILED")
                return False

            item.primary_source = batches[0]
            item.info_hash = item.primary_source.get("info_hash") or f"season_{item.season_id}"

            # Check existing Tamil AAC audio files
            clean_eng_slug = re.sub(r"[^\w\s-]", "", item.english_title).strip().replace(" ", "_")
            clean_romaji_slug = re.sub(r"[^\w\s-]", "", item.romaji_title).strip().replace(" ", "_")
            available_eps: List[int] = []
            for ep in range(1, 200):
                rec = await get_audio_for_episode(item.season_id, ep)
                if rec and rec.get("local_m4a_path") and os.path.exists(rec["local_m4a_path"]):
                    available_eps.append(ep)
                else:
                    found_aud = self.find_audio_in_vault(item.english_title or item.anime_title, item.season_number, ep)
                    if found_aud:
                        available_eps.append(ep)

            if not available_eps:
                LOGGER.info(f"[Stage 1] Harvesting Tamil AAC audio for {item.anime_title} from {self.source_channel_id}...")
                await harvest_channel(
                    channel_id=self.source_channel_id,
                    limit=max_episodes or 24,
                    anime_name=item.anime_title,
                    season_number=item.season_number,
                )
                for ep in range(1, 200):
                    rec = await get_audio_for_episode(item.season_id, ep)
                    if rec and rec.get("local_m4a_path") and os.path.exists(rec["local_m4a_path"]):
                        available_eps.append(ep)
                    else:
                        found_aud = self.find_audio_in_vault(item.english_title or item.anime_title, item.season_number, ep)
                        if found_aud:
                            available_eps.append(ep)

            if not available_eps:
                LOGGER.error(f"[Stage 1] No Tamil audio available for {item.anime_title} S{item.season_number:02d}")
                await update_season_state(item.season_id, "FAILED")
                return False

            if max_episodes:
                available_eps = available_eps[:max_episodes]
            item.available_episodes = sorted(available_eps)
            LOGGER.info(
                f"✅ [Stage 1] Ready: {item.english_title} S{item.season_number:02d} "
                f"({len(item.available_episodes)} Tamil episodes matched, Batch=[{item.primary_source.get('release_group')}])"
            )
            return True

    async def _execute_stage2_prefetch_download(self, item: SeasonWorkItem) -> bool:
        """
        Stage 2 Pre-Fetch Worker:
        - Acquires byte reservation in `SlidingWindowQuotaGuard` (400-450 GB window).
        - Runs up to 4 concurrent batch downloads across the 1 Gbps port.
        """
        est_bytes = self.parse_nyaa_size_bytes(item.primary_source.get("size", ""))
        await self.quota_guard.acquire_batch_reservation(item.info_hash, est_bytes)

        async with self.prefetch_sem:
            await update_season_state(item.season_id, "DOWNLOADING")
            LOGGER.info(
                f"⬇️ [Stage 2 Pre-Fetch] Downloading batch for {item.english_title} S{item.season_number:02d} "
                f"({item.primary_source.get('size', 'N/A')})..."
            )
            downloaded = await self.downloader.download_batch_torrent(
                item.primary_source,
                target_episodes=item.available_episodes,
            )
            if not downloaded:
                await self.quota_guard.release_batch(item.info_hash)
                await update_season_state(item.season_id, "FAILED")
                return False

            item.downloaded_videos = downloaded
            await update_season_state(item.season_id, "PROCESSING")
            return True

    async def _process_single_episode_sync_and_mux(
        self,
        item: SeasonWorkItem,
        ep_num: int,
        video_file: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Stages 3 & 4 Worker (Per-Episode Concurrent Pipeline):
        - Acquires `ram_episode_sem` to guarantee `/dev/shm/starfall_mux` stays <= 11.5 GB.
        - Stage 3 (`sync_sem`, 6 workers): Runs 4-anchor Mid/Side GCC-PHAT acoustic sync in RAM.
        - Stage 4 (`mux_sem`, 4 workers): Runs lossless `mkvmerge` in `/dev/shm/starfall_mux`
          with Auto-Rename Resilience (`BluRay` enforcement + batch group inheritance).
        """
        audio_rec = await get_audio_for_episode(item.season_id, ep_num)
        tamil_audio_path = audio_rec["local_m4a_path"] if audio_rec else None
        clean_slug = re.sub(r"[^\w\s-]", "", item.anime_title).strip().replace(" ", "_")
        clean_eng_slug = re.sub(r"[^\w\s-]", "", item.english_title).strip().replace(" ", "_")
        clean_romaji_slug = re.sub(r"[^\w\s-]", "", item.romaji_title).strip().replace(" ", "_")

        if not tamil_audio_path or not os.path.exists(tamil_audio_path):
            tamil_audio_path = self.find_audio_in_vault(
                anime_title=item.english_title or item.anime_title,
                season_num=item.season_number,
                ep_num=ep_num,
            )

        if not tamil_audio_path or not os.path.exists(tamil_audio_path):
            LOGGER.warning(f"Skipping {item.english_title} S{item.season_number:02d}E{ep_num:02d}: missing Tamil audio")
            return None

        # Resolve resilient metadata from leaf file -> batch folder -> torrent title
        resolved_meta = AutoRenameResolver.resolve_metadata(
            leaf_video_path=video_file,
            torrent_batch_title=item.primary_source.get("title", ""),
            nyaa_metadata=item.primary_source,
        )

        # Stage 3: Acoustic Synchronization (6 concurrent workers across 16 vCPUs)
        async with self.sync_sem:
            prop = await asyncio.to_thread(self.muxer.probe_video_properties, video_file)
            dur_sec = prop["duration_sec"] or 1440
            width = prop["width"] or 1920
            height = prop["height"] or 1080

            # Prefer harvested 16:9 logo thumbnail from source channel; otherwise generate 16:9 frame
            if item.harvested_thumb_path and os.path.exists(item.harvested_thumb_path):
                ep_thumb_path = item.harvested_thumb_path
            else:
                ep_thumb_path = os.path.join(
                    self.thumbnails_dir, f"thumb_{clean_slug}_S{item.season_number:02d}E{ep_num:02d}.jpg"
                )
                await asyncio.to_thread(
                    self.muxer.generate_16_9_thumbnail, video_file, ep_thumb_path, prop["duration"]
                )

            sync_result = await self.sync_engine.analyze_sync(video_file, tamil_audio_path)

        calculated_delay = sync_result["calculated_delay_ms"]
        sync_mode = sync_result["sync_mode"]
        psr = sync_result["min_psr_score"]
        speed_ratio = sync_result["speed_ratio"]

        output_filename = AutoRenameResolver.format_episode_filename(
            anime_name=item.english_title,
            season_num=item.season_number,
            episode_num=ep_num,
            year=item.year,
            resolved_meta=resolved_meta,
        )

        # 1. Fast Cache Check: If already muxed and valid in NVMe saved_muxes, reuse directly
        safe_dest_dir = os.path.join(DATA_DIR, "saved_muxes")
        os.makedirs(safe_dest_dir, exist_ok=True)
        safe_saved_path = os.path.join(safe_dest_dir, output_filename)
        if os.path.exists(safe_saved_path) and os.path.getsize(safe_saved_path) > 50 * 1024 * 1024:
            LOGGER.info(f"⚡ Episode already muxed in storage: {output_filename}")
            actual_mkv_path = safe_saved_path
        else:
            # 2. Check available RAM tmpfs space; spillover to NVMe tmp_mux if RAM is tight
            try:
                st = os.statvfs(RAM_MUX_DIR)
                shm_free = st.f_bavail * st.f_frsize
                raw_size = os.path.getsize(video_file) if os.path.exists(video_file) else 0
                needed_bytes = int(raw_size * 1.3) + (2 * 1024 * 1024 * 1024)
                if shm_free < needed_bytes:
                    out_dir = os.path.join(DATA_DIR, "tmp_mux")
                    os.makedirs(out_dir, exist_ok=True)
                else:
                    out_dir = RAM_MUX_DIR
            except Exception:
                out_dir = os.path.join(DATA_DIR, "tmp_mux")
                os.makedirs(out_dir, exist_ok=True)

            output_mkv_path = os.path.join(out_dir, output_filename)

            # Stage 4: Lossless MKV Muxing (bounded by ram_episode_sem)
            await self.quota_guard.ram_episode_sem.acquire()
            try:
                async with self.mux_sem:
                    mux_ok = await self.muxer.mux_single_episode(
                        video_path=video_file,
                        tamil_audio_path=tamil_audio_path,
                        output_mkv_path=output_mkv_path,
                        calculated_delay_ms=calculated_delay,
                        speed_ratio=speed_ratio,
                        poster_path=item.poster_path,
                        release_group=resolved_meta["release_group"],
                        title_tag=output_filename,
                    )
                if not mux_ok:
                    return None
            finally:
                self.quota_guard.ram_episode_sem.release()

            actual_mkv_path = safe_saved_path if os.path.exists(safe_saved_path) else output_mkv_path

        return {
            "episode_num": ep_num,
            "path": actual_mkv_path,
            "thumb_path": ep_thumb_path,
            "duration_sec": dur_sec,
            "width": width,
            "height": height,
            "sync_mode": sync_mode,
            "delay_ms": calculated_delay,
            "psr": psr,
            "speed_ratio": speed_ratio,
            "audio_id": audio_rec["audio_id"] if audio_rec else None,
            "raw_video": video_file,
            "telemetry": sync_result["anchor_telemetry"],
            "resolved_meta": resolved_meta,
        }

    async def _execute_stage3_to_5_season_pipeline(
        self,
        item: SeasonWorkItem,
        skip_upload: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute parallel episode sync/mux across the season, full-season merge,
        and strict 3-Step Telegram upload with immediate post-upload NVMe purge hooks.
        """
        sorted_eps = sorted(item.downloaded_videos.keys())
        tasks = [
            self._process_single_episode_sync_and_mux(item, ep_num, item.downloaded_videos[ep_num])
            for ep_num in sorted_eps
        ]
        ep_results = await asyncio.gather(*tasks)

        for res in ep_results:
            if res:
                item.muxed_episodes[res["episode_num"]] = res

        muxed_paths = [item.muxed_episodes[k]["path"] for k in sorted(item.muxed_episodes.keys())]
        if not muxed_paths:
            await self.downloader.cleanup_batch(item.info_hash)
            await self.quota_guard.release_batch(item.info_hash)
            await update_season_state(item.season_id, "FAILED")
            return {"status": "FAILED", "reason": "All episode muxes failed"}

        first_meta = item.muxed_episodes[sorted(item.muxed_episodes.keys())[0]]["resolved_meta"]
        quality_str = first_meta["quality"]       # Strictly 'BluRay' or 'WEBRip'
        codec_str = first_meta["codec"]
        group_str = first_meta["release_group"]

        # Full-Season Merged Video Generation (Step 2 artifact) - 4GB Below Merge Planner
        await update_season_state(item.season_id, "MERGING")
        if len(muxed_paths) >= 2:
            ep_tuples = [(k, item.muxed_episodes[k]["path"]) for k in sorted(item.muxed_episodes.keys())]
            merge_chunks = AutoRenameResolver.plan_merge_chunks(
                ep_tuples, max_chunk_bytes=TG_MAX_USER_LIMIT_MIB * 1024 * 1024
            )
            chunk_summary = [(c['ep_start'], c['ep_end'], f"{c['total_bytes'] / (1024 * 1024):.1f} MB") for c in merge_chunks]
            LOGGER.info(
                f"📋 4GB Below Merge Planner created {len(merge_chunks)} chunk(s) for "
                f"{item.english_title} S{item.season_number:02d}: {chunk_summary}"
            )

            clean_slug = re.sub(r"[^\w\s-]", "", item.english_title).strip().replace(" ", "_")

            for chunk in merge_chunks:
                chunk_paths = chunk["episode_paths"]
                if len(chunk_paths) < 2:
                    continue

                chunk_fn = AutoRenameResolver.format_season_merge_filename(
                    anime_name=item.english_title,
                    season_num=item.season_number,
                    ep_start=chunk["ep_start"],
                    ep_end=chunk["ep_end"],
                    resolved_meta=first_meta,
                    year=item.year,
                )

                merge_dir = RAM_MUX_DIR
                try:
                    st = os.statvfs(RAM_MUX_DIR)
                    shm_free = st.f_bavail * st.f_frsize
                    needed_merge = sum(os.path.getsize(p) for p in chunk_paths if os.path.exists(p)) * 1.15
                    if shm_free < needed_merge:
                        merge_dir = os.path.join(DATA_DIR, "tmp_mux")
                        os.makedirs(merge_dir, exist_ok=True)
                except Exception as se:
                    merge_dir = os.path.join(DATA_DIR, "tmp_mux")
                    os.makedirs(merge_dir, exist_ok=True)

                chunk_out_path = os.path.join(merge_dir, chunk_fn)
                async with self.mux_sem:
                    created_parts = await self.muxer.merge_season(
                        chunk_paths,
                        chunk_out_path,
                        split_size_mb=None,  # NEVER SPLIT! Discrete chunk <= 3900 MB
                        poster_path=item.poster_path,
                    )

                for m_path in created_parts:
                    m_prop = await asyncio.to_thread(self.muxer.probe_video_properties, m_path)
                    item.merged_season_parts.append({
                        "path": m_path,
                        "filename": chunk_fn,
                        "ep_start": chunk["ep_start"],
                        "ep_end": chunk["ep_end"],
                        "thumb_path": None,
                        "duration_sec": m_prop["duration_sec"],
                        "width": m_prop["width"],
                        "height": m_prop["height"],
                    })

        if skip_upload:
            return {"status": "SUCCESS", "season_id": item.season_id, "episodes_processed": len(muxed_paths)}

        # ====================================================================
        # STAGE 5: STRICT 3-STEP TARGET CHANNEL UPLOAD SEQUENCING
        # ====================================================================
        uploaded_msg_ids: List[int] = []
        async with self.upload_season_sem:
            await self.router.start()

            # SINGLE AUTHORITATIVE 16:9 HD (1280x720) THUMBNAIL FOR ENTIRE SEASON (Merge + All Episodes)
            clean_slug = re.sub(r"[^\w-]", "_", item.english_title)
            season_thumb: Optional[str] = None
            
            # 1. Prefer true HD 1280x720 extracted directly from 1080p source video
            if item.muxed_episodes:
                first_ep_num = min(item.muxed_episodes.keys())
                first_ep_path = item.muxed_episodes[first_ep_num]["path"]
                if first_ep_path and os.path.exists(first_ep_path):
                    hd_thumb_target = os.path.join(self.thumbnails_dir, f"thumb_{clean_slug}_S{item.season_number:02d}_1280x720.jpg")
                    self.muxer.generate_16_9_thumbnail(first_ep_path, hd_thumb_target, 1440.0)
                    if os.path.exists(hd_thumb_target) and os.path.getsize(hd_thumb_target) > 5000:
                        season_thumb = hd_thumb_target

            # 2. Check candidate harvested thumb if verified >= 1280x720
            if not season_thumb and item.harvested_thumb_path and os.path.exists(item.harvested_thumb_path):
                try:
                    with Image.open(item.harvested_thumb_path) as im:
                        w, h = im.size
                        if w >= 1280 and h >= 720:
                            season_thumb = item.harvested_thumb_path
                except Exception:
                    pass

            # 3. Fallback to poster card or any valid cached thumb
            if not season_thumb and item.poster_card_path and os.path.exists(item.poster_card_path):
                season_thumb = item.poster_card_path

            LOGGER.info(f"🖼️ Authoritative HD (1280x720) Season 16:9 Thumbnail: {season_thumb}")

            # STEP 1: Post Season Poster Card (Template 11 Hero Card)
            caption_template = USER_SETTINGS.get("POST_ANIME_CAPTION") or (
                "<b>「 {title} - {year} 」</b>\n━━━━━━━━━━━━━━━━━━\n"
                "╔════◇═══════════◇════\n"
                "║ Season ➤ {season}\n"
                "║ AniList ➤ {rating} Rating\n"
                "║ Genres ➤ {genres}\n"
                "║ Quality ➤ {resolution} {bit} {quality} {codec}\n"
                "║ Audio ➤ Tamil {audio_codec} {audio_channels} ~ {shortsub}\n"
                "╚════◇═══════════◇════\n"
                " <blockquote expandable>Synopsis :\n   {plot}</blockquote>\n\n"
                "Dropped By ➤ [@Anime_Starfall🥰]"
            )
            poster_caption = AutoRenameResolver.sanitize_bluray_keyword(
                caption_template.format(
                    title=item.english_title,
                    year=item.year,
                    season=item.season_number,
                    rating=item.rating_str,
                    genres=item.genres_str,
                    resolution=first_meta["resolution"],
                    bit=first_meta["bit"],
                    quality=first_meta.get("quality", "BluRay"),
                    codec=first_meta.get("codec", "x265"),
                    audio_codec="AAC",
                    audio_channels="2.0",
                    shortsub="ESub",
                    plot=item.clean_synopsis,
                )
            )
            poster_msg_id = await self.router.publish_season_poster(
                source_poster_msg_id=item.source_poster_msg_id,
                photo_path=item.poster_card_path or item.poster_path,
                caption=poster_caption,
            )
            if poster_msg_id:
                uploaded_msg_ids.append(poster_msg_id)

            # STEP 2: Post Full-Season Merged Video File(s) (4GB Below Planner Chunks)
            if item.merged_season_parts:
                for m_part in item.merged_season_parts:
                    part_title = m_part.get("filename") or os.path.basename(m_part["path"])
                    merge_caption = f"<b>{part_title}</b>"
                    m_msg_id = await self.router.upload_file(
                        filepath=m_part["path"],
                        caption=merge_caption,
                        thumb_path=season_thumb,
                        duration_sec=m_part["duration_sec"],
                        width=m_part["width"],
                        height=m_part["height"],
                        cleanup_after_upload=True,
                    )
                    if m_msg_id:
                        uploaded_msg_ids.append(m_msg_id)

            # STEP 3: Post Single Episodes Sequentially
            episodes_payload: List[Dict[str, Any]] = []
            for ep_num in sorted(item.muxed_episodes.keys()):
                ep_data = item.muxed_episodes[ep_num]
                ep_meta = ep_data["resolved_meta"]
                formatted_ep_title = AutoRenameResolver.format_episode_filename(
                    item.english_title, item.season_number, ep_num, item.year, ep_meta
                )
                ep_caption = f"<b>{formatted_ep_title}</b>"
                episodes_payload.append({
                    **ep_data,
                    "thumb_path": season_thumb,
                    "caption": ep_caption,
                    "cleanup_after_upload": True,
                })

            async def _on_episode_purged(reclaimed_bytes: int) -> None:
                await self.quota_guard.release_episode_bytes(item.info_hash, reclaimed_bytes)

            ep_upload_results = await self.router.upload_season_episodes_ordered(
                episodes_payload,
                on_purged_bytes_cb=_on_episode_purged,
            )

            for ep_num in sorted(item.muxed_episodes.keys()):
                ep_data = item.muxed_episodes[ep_num]
                msg_id, bot_tag = ep_upload_results.get(ep_num, (None, "NONE"))
                if msg_id:
                    uploaded_msg_ids.append(msg_id)
                await record_episode_job(
                    season_id=item.season_id,
                    episode_number=ep_num,
                    audio_id=ep_data["audio_id"],
                    bd_relative_path=ep_data["raw_video"],
                    sync_mode=ep_data["sync_mode"],
                    calculated_delay_ms=ep_data["delay_ms"],
                    speed_ratio=ep_data["speed_ratio"],
                    min_psr_score=ep_data["psr"],
                    anchor_telemetry=ep_data["telemetry"],
                    job_state="COMPLETED" if msg_id else "QUARANTINE",
                    tg_single_msg_id=msg_id,
                    uploaded_by_bot=bot_tag,
                )
        # Final Batch Purge & Quota Release
        await self.downloader.cleanup_batch(item.info_hash)
        await self.quota_guard.release_batch(item.info_hash)
        await update_season_state(item.season_id, "COMPLETED")

        LOGGER.info(
            f"🎉 Season Complete: {item.english_title} S{item.season_number:02d} | "
            f"{len(uploaded_msg_ids)} messages posted to {self.target_channel_id} | "
            f"Quota Telemetry: {self.quota_guard.get_telemetry()}"
        )
        return {
            "status": "SUCCESS",
            "season_id": item.season_id,
            "target_channel": self.target_channel_id,
            "episodes_processed": len(muxed_paths),
            "uploaded_msg_ids": uploaded_msg_ids,
        }

    async def process_series_season(
        self,
        anime_title: str,
        season_number: int = 1,
        max_episodes_to_process: Optional[int] = None,
        skip_upload: bool = False,
    ) -> Dict[str, Any]:
        """Drop-in compatible entry point for processing a single anime season."""
        await init_db()
        item = SeasonWorkItem(anime_title=anime_title, season_number=season_number)
        ok_s1 = await self._resolve_stage1_metadata_and_audio(item, max_episodes=max_episodes_to_process)
        if not ok_s1:
            return {"status": "FAILED", "reason": "Stage 1 resolution or audio harvest failed"}

        ok_s2 = await self._execute_stage2_prefetch_download(item)
        if not ok_s2:
            return {"status": "FAILED", "reason": "Stage 2 batch torrent download failed"}

        try:
            return await self._execute_stage3_to_5_season_pipeline(item, skip_upload=skip_upload)
        finally:
            if not skip_upload:
                await self.router.stop()

    async def run_catalogue_48h(
        self,
        catalogue: List[Tuple[str, int]],
        max_episodes_per_season: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Continuous 48-Hour Sliding-Window Producer-Consumer Engine for all 54 Series (~3,000 Episodes).
        - Pre-fetches upcoming series torrents into the 400-450 GB NVMe sliding window while
          downstream seasons are syncing, muxing in `/dev/shm`, and uploading across the 6 clients.
        """
        start_ts = time.time()
        await init_db()
        await self.router.start()

        prefetch_queue: asyncio.Queue[Optional[SeasonWorkItem]] = asyncio.Queue(maxsize=12)
        process_queue: asyncio.Queue[Optional[SeasonWorkItem]] = asyncio.Queue(maxsize=6)
        completed_seasons: List[Dict[str, Any]] = []

        async def _stage1_producer() -> None:
            for title, s_num in catalogue:
                # Skip already completed seasons
                try:
                    async with aiosqlite.connect(self.db_path) as db:
                        async with db.execute(
                            "SELECT season_state FROM anime_seasons WHERE title_romaji = ? AND season_number = ?",
                            (title, s_num),
                        ) as cur:
                            row = await cur.fetchone()
                            if row and row[0] == "COMPLETED":
                                LOGGER.info(f"⏭️ Skipping already completed season: {title} S{s_num:02d}")
                                continue
                except Exception as dbe:
                    LOGGER.debug(f"DB check error: {dbe}")

                item = SeasonWorkItem(anime_title=title, season_number=s_num)
                try:
                    if await self._resolve_stage1_metadata_and_audio(item, max_episodes=max_episodes_per_season):
                        await prefetch_queue.put(item)
                except Exception as e:
                    LOGGER.error(f"[Stage 1 Producer] Error on {title} S{s_num}: {e}")
            for _ in range(STAGE2_TORRENT_PREFETCH_SLOTS):
                await prefetch_queue.put(None)

        async def _stage2_prefetch_worker(worker_id: int) -> None:
            while True:
                item = await prefetch_queue.get()
                if item is None:
                    prefetch_queue.task_done()
                    break
                try:
                    if await self._execute_stage2_prefetch_download(item):
                        await process_queue.put(item)
                except Exception as e:
                    LOGGER.error(f"[Stage 2 Worker #{worker_id}] Error on {item.anime_title}: {e}")
                    await self.quota_guard.release_batch(item.info_hash)
                finally:
                    prefetch_queue.task_done()

        async def _stage3_to_5_consumer(worker_id: int) -> None:
            while True:
                item = await process_queue.get()
                if item is None:
                    process_queue.task_done()
                    break
                try:
                    res = await self._execute_stage3_to_5_season_pipeline(item, skip_upload=False)
                    completed_seasons.append(res)
                except Exception as e:
                    LOGGER.error(f"[Stage 3-5 Consumer #{worker_id}] Error on {item.anime_title}: {e}")
                    await self.quota_guard.release_batch(item.info_hash)
                finally:
                    process_queue.task_done()

        try:
            producer_task = asyncio.create_task(_stage1_producer())
            prefetch_workers = [
                asyncio.create_task(_stage2_prefetch_worker(i))
                for i in range(1, STAGE2_TORRENT_PREFETCH_SLOTS + 1)
            ]
            consumer_workers = [
                asyncio.create_task(_stage3_to_5_consumer(i))
                for i in range(1, STAGE5_SEASON_UPLOAD_SLOTS + 1)
            ]

            await producer_task
            await asyncio.gather(*prefetch_workers)
            for _ in range(STAGE5_SEASON_UPLOAD_SLOTS):
                await process_queue.put(None)
            await asyncio.gather(*consumer_workers)

        finally:
            await self.router.stop()

        elapsed_hr = (time.time() - start_ts) / 3600.0
        total_eps = sum(r.get("episodes_processed", 0) for r in completed_seasons)
        return {
            "seasons_completed": len(completed_seasons),
            "total_episodes_uploaded": total_eps,
            "elapsed_hours": round(elapsed_hr, 2),
            "sustained_eps_per_hour": round(total_eps / max(elapsed_hr, 0.001), 2),
        }


def load_catalogue_from_json_or_db(catalogue_json_path: str) -> List[Tuple[str, int]]:
    """Load the 54-series catalogue from `data/channel_animes.json` if present."""
    if os.path.exists(catalogue_json_path):
        try:
            with open(catalogue_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pairs: List[Tuple[str, int]] = []
            for entry in data:
                title = entry.get("title")
                for s in entry.get("seasons", [{"season": 1}]):
                    pairs.append((title, int(s.get("season", 1))))
            if pairs:
                return pairs
        except Exception as e:
            LOGGER.warning(f"Failed to parse {catalogue_json_path}: {e}")
    return [("A Couple of Cuckoos", 1)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Starfall v1.6 48-Hour Master Pipeline Orchestrator")
    parser.add_argument("--anime", type=str, default="", help="Single anime title to process (omit for full 54-series catalogue)")
    parser.add_argument("--season", type=int, default=1, help="Season number")
    parser.add_argument("--catalogue-json", type=str, default=os.path.join(DATA_DIR, "channel_animes.json"), help="Path to 54-series JSON")
    parser.add_argument("--limit", type=int, default=0, help="Max episodes per season (0 = all)")
    parser.add_argument("--source-channel", type=int, default=HARVEST_CHANNEL_ID)
    parser.add_argument("--target-channel", type=int, default=TARGET_UPLOAD_CHANNEL_ID)
    parser.add_argument("--self-test", action="store_true", help="Run offline unit verification of AutoRenameResolver and QuotaGuard")
    args = parser.parse_args()

    async def _main():
        if args.self_test:
            sample_path = (
                "/home/administrator/starfall/data/torrents/026da52a33effbb458afacbaa95675186860812c/"
                "[neoHEVC] A Couple of Cuckoos [Season 1] [BD 1080p x265 HEVC AAC] [Dual Audio]/"
                "A Couple of Cuckoos - S01E01.mkv"
            )
            meta = AutoRenameResolver.resolve_metadata(
                leaf_video_path=sample_path,
                torrent_batch_title="[neoHEVC] A Couple of Cuckoos [Season 1] [BDRip 1080p x265 HEVC AAC]",
            )
            ep_fn = AutoRenameResolver.format_episode_filename("A Couple of Cuckoos", 1, 1, 2022, meta)
            merge_fn = AutoRenameResolver.format_season_merge_filename("A Couple of Cuckoos", 1, 1, 24, meta, year=2022)
            assert meta["quality"] == "BluRay", f"Expected BluRay, got {meta['quality']}"
            assert meta["release_group"] == "neoHEVC", f"Expected neoHEVC, got {meta['release_group']}"
            assert "BDRip" not in ep_fn and "BluRay" in ep_fn
            print("✅ AutoRenameResolver verified:")
            print("  • Resolved Metadata :", meta)
            print("  • Episode Filename  :", ep_fn)
            print("  • Merge Filename    :", merge_fn)
            assert merge_fn.startswith("📦 🄰🅂- [S1E01-24]")

            # Test 4GB Below Merge Planner simulation
            from unittest.mock import patch
            dummy_eps = [(i, f"/tmp/dummy_ep_{i}.mkv") for i in range(1, 26)]
            with patch("os.path.exists", return_value=True), patch("os.path.getsize", return_value=400 * 1024 * 1024):
                chunks = AutoRenameResolver.plan_merge_chunks(dummy_eps, max_chunk_bytes=3900 * 1024 * 1024)
                print(f"  • Merge Planner 4GB Below Simulation ({len(dummy_eps)} eps x 400MB = 10GB): {len(chunks)} chunk(s)")
                for idx, c in enumerate(chunks, 1):
                    c_fn = AutoRenameResolver.format_season_merge_filename("My Hero Academia", 2, c["ep_start"], c["ep_end"], meta, year=2016)
                    print(f"    Chunk {idx} (E{c['ep_start']:02d}-E{c['ep_end']:02d}, {c['total_bytes']/(1024*1024):.0f} MB): {c_fn}")
                    assert c["total_bytes"] <= 3900 * 1024 * 1024, "Chunk exceeded 3900 MB limit!"
            return

        manager = StarfallPipelineManager(
            source_channel_id=args.source_channel,
            target_channel_id=args.target_channel,
        )
        ep_limit = args.limit if args.limit > 0 else None

        if args.anime:
            res = await manager.process_series_season(
                args.anime,
                season_number=args.season,
                max_episodes_to_process=ep_limit,
            )
            print("Single Season Result:", res)
        else:
            catalogue = load_catalogue_from_json_or_db(args.catalogue_json)
            LOGGER.info(f"Loaded {len(catalogue)} season entries for 48-hour continuous execution.")
            res = await manager.run_catalogue_48h(catalogue, max_episodes_per_season=ep_limit)
            print("48-Hour Catalogue Execution Summary:", res)

    asyncio.run(_main())