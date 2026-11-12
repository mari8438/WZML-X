#!/usr/bin/env python3
"""
Starfall Master Automated Channel Dispatcher
============================================
Autonomous release and publishing pipeline for Starfall anime releases.
- Scans /home/administrator/starfall/downloads/muxed_output/
- Groups episodes by (title, season, year)
- Partitions & merges whole episodes into balanced <= 3,950 MiB parts
- Attaches container cover art (/home/administrator/starfall/data/intro_preroll.jpg)
- Generates official 1280x720 HD 16:9 thumbnail (<200KB)
- Generates Template 11 Poster Card via AniList metadata
- High-speed parallel staging to Main Dump (-1003887117402) via 3 Helper Bots + User Client
- Sequential publishing to Target Channel (-1004415413346) with 3.5s spacing
- Verified immediate post-release NVMe sweep to reclaim disk space
"""

import argparse
import asyncio
import concurrent.futures
import html
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, "/home/administrator/starfall")

import numpy as np
import scipy.signal
from PIL import Image
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
    HELPER_TOKENS,
    DATA_DIR,
)
from bot.modules.starfall_pipeline.merge_planner import TG_MAX_CHUNK_MIB
from bot.modules.starfall_pipeline.release_pipeline import (
    probe_file_meta,
    prepare_hd_thumbnail,
    find_anime_landscape_thumbnail,
)

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s",
)
LOGGER = logging.getLogger("MasterDispatcher")

for _log in ("pyrogram", "pyrogram.session", "pyrogram.connection", "pyrogram.dispatcher", "pyrogram.client"):
    logging.getLogger(_log).setLevel(logging.ERROR)

# Operational Constants
DUMP_CHANNEL_ID = -1003887117402       # Main Dump (-1003887117402)
TARGET_CHANNEL_ID = -1004415413346     # Tamil Muxed Collection (-1004415413346)
TARGET_CLEAN_ID = "4415413346"
DUMP_CLEAN_ID = "3887117402"
POST_SPACING_SEC = 1.5
COVER_PHOTO_PATH = "/home/administrator/starfall/data/intro_preroll.jpg"
MUXED_DIR = "/home/administrator/starfall/downloads/muxed_output"
THUMBS_DIR = "/home/administrator/starfall/thumbnails"

FILENAME_REGEX = re.compile(
    r"^🄰🅂-\s*\[S(\d+)E(\d+)\]\s*(.+?)\s*\((\d{4})\)\s*"
    r"(\w+)\s+(\w+)\s+(\w+)\s+(\w+)\s*"
    r"\[(.*?)[-\s]+([A-Za-z0-9]+)\s+([0-9.]+)\]\s*(\w+)\s*~\s*(.*?)\s*~\s*\[@Anime_Starfall🥰\]\.mkv"
)


def format_merged_filename(
    title: str,
    season: int,
    start_ep: int,
    end_ep: int,
    part_num: int,
    total_parts: int,
    meta_info: Dict[str, str],
) -> str:
    """Format standard merged filename without CR word."""
    part_suffix = f" Part {part_num}" if total_parts > 1 else ""
    year = meta_info.get("year", "2024")
    resolution = meta_info.get("resolution", "1080p")
    bit = meta_info.get("bit", "10bit")
    quality = meta_info.get("quality", "BluRay")
    codec = meta_info.get("codec", "x265")
    languages = "Tam + Jap"
    audio_codec = meta_info.get("audio_codec", "AAC")
    audio_channels = meta_info.get("audio_channels", "2.0")
    shortsub = meta_info.get("shortsub", "ESub")
    release_group = meta_info.get("release_group", "Starfall")

    raw_fn = (
        f"🄰🅂- [S{season}E{start_ep:02d}-{end_ep:02d}] {title}{part_suffix} ({year}) "
        f"{resolution} {bit} {quality} {codec} [{languages} {audio_codec} {audio_channels}] {shortsub} ~ "
        f"{release_group} ~ [@Anime_Starfall🥰].mkv"
    )
    raw_fn = re.sub(r"\bCR\b\s*", "", raw_fn)
    raw_fn = re.sub(r"\s+", " ", raw_fn).strip()
    return raw_fn


def probe_season_audio_indices(filepath: str) -> Tuple[int, int]:
    """Identify Tamil (Track 1) and Reference (Track 2/3) audio stream indices."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type:stream_tags=language", "-of", "json", filepath]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
        a_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if len(a_streams) < 2:
            return 1, 2
        tam_idx = a_streams[0]["index"]
        ref_idx = a_streams[1]["index"]
        for s in a_streams[1:]:
            if (s.get("tags", {}).get("language") or "").lower() in ("jpn", "jap"):
                ref_idx = s["index"]
                break
        return tam_idx, ref_idx
    except Exception:
        return 1, 2


def measure_acoustic_lag(fpath: str, tam_idx: int, ref_idx: int, ss: int = 10, dur: int = 25) -> Tuple[float, float]:
    """Measure acoustic envelope lag (GCC-PHAT) between Tamil and Reference tracks."""
    uid = uuid.uuid4().hex[:8]
    ram_dir = "/dev/shm" if os.path.exists("/dev/shm") else "/tmp"
    t_raw = f"{ram_dir}/disp_tam_{uid}.raw"
    r_raw = f"{ram_dir}/disp_ref_{uid}.raw"
    try:
        cmd1 = ["ffmpeg", "-y", "-ss", str(ss), "-i", fpath, "-t", str(dur), "-map", f"0:{tam_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", t_raw]
        cmd2 = ["ffmpeg", "-y", "-ss", str(ss), "-i", fpath, "-t", str(dur), "-map", f"0:{ref_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", r_raw]
        subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        t1 = np.fromfile(t_raw, dtype=np.float32)
        t2 = np.fromfile(r_raw, dtype=np.float32)
        min_l = min(len(t1), len(t2))
        if min_l < 16000 * 3:
            return 0.0, 0.0
        e1 = np.abs(scipy.signal.hilbert(t1[:min_l]))
        e2 = np.abs(scipy.signal.hilbert(t2[:min_l]))
        corr = scipy.signal.correlate(e1, e2, mode="full")
        lags = scipy.signal.correlation_lags(len(e1), len(e2), mode="full")
        best = lags[np.argmax(corr)]
        delay_ms = float((best / 16000.0) * 1000.0)
        conf = float(corr[np.argmax(corr)] / (np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-9))
        return delay_ms, conf
    except Exception:
        return 0.0, 0.0
    finally:
        if os.path.exists(t_raw):
            try: os.remove(t_raw)
            except Exception: pass
        if os.path.exists(r_raw):
            try: os.remove(r_raw)
            except Exception: pass


def strip_unwanted_audio_tracks(fpath: str) -> bool:
    """Ensure file contains ONLY Tamil and Japanese audio tracks, dropping English audio."""
    cmd_info = ["mkvmerge", "-J", fpath]
    res = subprocess.run(cmd_info, capture_output=True, text=True)
    if res.returncode != 0:
        return False
    try:
        meta = json.loads(res.stdout)
    except Exception:
        return False

    keep_audio_ids = []
    has_english = False
    for tr in meta.get("tracks", []):
        if tr.get("type") == "audio":
            props = tr.get("properties", {})
            lang = (props.get("language") or "").lower()
            name = (props.get("track_name") or "").lower()
            if lang in ["eng", "en"] or "eng" in name:
                has_english = True
            else:
                keep_audio_ids.append(tr.get("id"))

    if not has_english:
        return False

    tmp_out = fpath + ".strip.mkv"
    cmd_strip = ["mkvmerge", "--ui-language", "en_US", "-o", tmp_out]
    if keep_audio_ids:
        cmd_strip.extend(["-a", ",".join(str(i) for i in keep_audio_ids)])
    else:
        cmd_strip.append("-A")
    cmd_strip.append(fpath)
    proc = subprocess.run(cmd_strip, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode <= 1 and os.path.exists(tmp_out):
        shutil.move(tmp_out, fpath)
        LOGGER.info(f"🚫 [Audio Filter] Removed English audio from {os.path.basename(fpath)} (kept tracks {keep_audio_ids})")
        return True
    else:
        if os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass
        return False


def ensure_audio_sync_for_file(fpath: str) -> bool:
    """Audit and losslessly lock audio sync if intro-card offset is detected."""
    # First: Strip English audio if present, keeping ONLY Tamil and Japanese audio
    strip_unwanted_audio_tracks(fpath)

    fname = os.path.basename(fpath)
    tam_idx, ref_idx = probe_season_audio_indices(fpath)
    lag_ms, conf = measure_acoustic_lag(fpath, tam_idx, ref_idx, ss=10, dur=25)
    if conf < 0.25:
        lag_ms2, conf2 = measure_acoustic_lag(fpath, tam_idx, ref_idx, ss=25, dur=25)
        if conf2 > conf:
            lag_ms, conf = lag_ms2, conf2

    if abs(lag_ms) <= 35.0:
        return True

    if lag_ms < -100.0 and conf >= 0.25:
        needed_delay_ms = int(round(-lag_ms))
        LOGGER.info(f"🔧 [AutoSync] {fname}: Lag={lag_ms:+.1f}ms (conf: {conf:.2f}) -> Applying --sync 1:{needed_delay_ms}ms...")
        tmp_out = fpath + ".sync.mkv"
        cmd = ["mkvmerge", "--ui-language", "en_US", "-o", tmp_out, "--sync", f"1:{needed_delay_ms}", fpath]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if proc.returncode > 1:
            if os.path.exists(tmp_out):
                try: os.remove(tmp_out)
                except Exception: pass
            LOGGER.error(f"❌ AutoSync mkvmerge failed for {fname}: {proc.stderr}")
            return False

        res_ms, res_conf = measure_acoustic_lag(tmp_out, tam_idx, ref_idx, ss=10, dur=25)
        if abs(res_ms) <= 35.0:
            shutil.move(tmp_out, fpath)
            LOGGER.info(f"🎯 [AutoSync Locked] {fname}: Residual Lag={res_ms:+.2f}ms (was {lag_ms:+.1f}ms)")
            return True
        else:
            if os.path.exists(tmp_out):
                try: os.remove(tmp_out)
                except Exception: pass
            LOGGER.warning(f"⚠️ [AutoSync Divergence] {fname}: Residual {res_ms:+.1f}ms did not drop <= 35ms. Kept original.")
            return False
    return True


class MasterReleaseDispatcher:
    def __init__(self):
        self.anilist = AniListClient()
        self.user_client: Optional[Client] = None
        self.helper_bots: List[Client] = []
        self._is_initialized = False

    async def initialize(self):
        """Start User Client and 3 Helper Bots."""
        if self._is_initialized:
            return

        sessions_dir = os.path.join(DATA_DIR, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)

        LOGGER.info("🔑 Starting User Client (@Hsin_The_Moon_Fox)...")
        self.user_client = Client(
            "hsin_moon_fox",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            session_string=USER_SESSION_STRING,
            in_memory=True,
            workers=16,
            max_concurrent_transmissions=10,
        )
        await self.user_client.start()
        u_me = await self.user_client.get_me()
        u_name = f"@{u_me.username}" if u_me.username else f"User_{u_me.id}"
        LOGGER.info(f"✅ User Client online: {u_name} (ID: {u_me.id}, Premium={u_me.is_premium})")

        raw_tokens = [t.strip() for t in HELPER_TOKENS.split() if t.strip()]
        LOGGER.info(f"🤖 Starting all {len(raw_tokens)} Helper Bots in pool...")
        self.helper_bots = []
        for idx, tok in enumerate(raw_tokens, 1):
            bot = Client(
                f"starfall_helper_{idx}",
                api_id=TELEGRAM_API,
                api_hash=TELEGRAM_HASH,
                bot_token=tok,
                workdir=sessions_dir,
                in_memory=False,
                workers=8,
                max_concurrent_transmissions=8,
            )
            await bot.start()
            b_me = await bot.get_me()
            LOGGER.info(f"✅ Helper Bot #{idx} online: @{b_me.username}")
            self.helper_bots.append(bot)

        self._is_initialized = True
        LOGGER.info("🚀 Master Release Dispatcher initialized successfully.")

    async def close(self):
        """Gracefully shut down all clients."""
        for b in self.helper_bots:
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
        LOGGER.info("🛑 Master Release Dispatcher clients stopped.")

    def scan_seasons(self) -> List[Dict[str, Any]]:
        """
        Scan muxed_output directory, parse files with regex,
        and group into completed seasons with full metadata.
        """
        if not os.path.exists(MUXED_DIR):
            return []

        files = [f for f in os.listdir(MUXED_DIR) if f.endswith(".mkv")]
        from collections import defaultdict
        seasons_map = defaultdict(list)

        for f in files:
            m = FILENAME_REGEX.match(f)
            if m:
                s_num = int(m.group(1))
                ep_num = int(m.group(2))
                title = m.group(3).strip()
                year = int(m.group(4))
                filepath = os.path.join(MUXED_DIR, f)
                file_size = os.path.getsize(filepath)

                tech_meta = {
                    "year": str(year),
                    "resolution": m.group(5),
                    "bit": m.group(6),
                    "quality": m.group(7),
                    "codec": m.group(8),
                    "languages": m.group(9),
                    "audio_codec": m.group(10),
                    "audio_channels": m.group(11),
                    "shortsub": m.group(12),
                    "release_group": m.group(13).strip(),
                }
                seasons_map[(title, s_num, year)].append((ep_num, filepath, file_size, tech_meta))

        discovered = []
        for (title, s_num, year), ep_list in seasons_map.items():
            ep_list.sort(key=lambda x: x[0])
            total_size_bytes = sum(x[2] for x in ep_list)
            first_meta = ep_list[0][3]

            discovered.append({
                "title": title,
                "season": s_num,
                "year": year,
                "total_eps": len(ep_list),
                "episodes": ep_list,
                "total_size_bytes": total_size_bytes,
                "total_size_mib": total_size_bytes / (1024 * 1024),
                "total_size_gib": total_size_bytes / (1024 ** 3),
                "meta": first_meta,
            })

        return discovered

    def plan_season_merge(self, season_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Partition episodes into balanced <= 3,950 MiB whole-episode chunks."""
        ep_list = season_info["episodes"]
        episodes = []
        for ep_num, path, size_bytes, _ in ep_list:
            episodes.append({
                "ep_num": ep_num,
                "path": path,
                "size_mib": size_bytes / (1024 * 1024),
            })

        total_size = sum(e["size_mib"] for e in episodes)
        if total_size <= TG_MAX_CHUNK_MIB:
            return [{
                "part_num": 1,
                "total_parts": 1,
                "episodes": [e["path"] for e in episodes],
                "start_ep": episodes[0]["ep_num"],
                "end_ep": episodes[-1]["ep_num"],
                "est_size_mib": total_size,
            }]

        min_parts = math.ceil(total_size / TG_MAX_CHUNK_MIB)
        target_size = total_size / min_parts

        chunks = []
        curr_chunk = []
        curr_size = 0.0

        for ep in episodes:
            rem_eps = len(episodes) - (sum(len(c) for c in chunks) + len(curr_chunk))
            rem_parts = min_parts - len(chunks)

            should_split = False
            if curr_chunk:
                if (curr_size + ep["size_mib"]) > TG_MAX_CHUNK_MIB:
                    should_split = True
                elif rem_parts > 1 and curr_size >= target_size * 0.95 and rem_eps >= (rem_parts - 1):
                    should_split = True

            if should_split:
                chunks.append(curr_chunk)
                curr_chunk = [ep]
                curr_size = ep["size_mib"]
            else:
                curr_chunk.append(ep)
                curr_size += ep["size_mib"]

        if curr_chunk:
            chunks.append(curr_chunk)

        total_parts = len(chunks)
        plan = []
        for idx, chunk in enumerate(chunks, 1):
            chunk_eps = [e["path"] for e in chunk]
            chunk_duration = sum(probe_file_meta(ep_p)["duration"] for ep_p in chunk_eps)
            plan.append({
                "part_num": idx,
                "total_parts": total_parts,
                "episodes": chunk_eps,
                "start_ep": chunk[0]["ep_num"],
                "end_ep": chunk[-1]["ep_num"],
                "est_size_mib": sum(e["size_mib"] for e in chunk),
                "exact_duration": chunk_duration,
            })
        return plan

    async def execute_merge(
        self,
        season_info: Dict[str, Any],
        plan: List[Dict[str, Any]],
    ) -> List[Tuple[str, str, int, int]]:
        """
        Merge each chunk using mkvmerge with passive cover art.
        Returns list of (part_tag, output_filepath, part_num, exact_duration).
        """
        merged_files = []
        for chunk in plan:
            part_num = chunk["part_num"]
            total_parts = chunk["total_parts"]
            start_ep = chunk["start_ep"]
            end_ep = chunk["end_ep"]
            episodes = chunk["episodes"]
            exact_duration = chunk.get("exact_duration") or sum(probe_file_meta(ep)["duration"] for ep in episodes)

            out_filename = format_merged_filename(
                title=season_info["title"],
                season=season_info["season"],
                start_ep=start_ep,
                end_ep=end_ep,
                part_num=part_num,
                total_parts=total_parts,
                meta_info=season_info["meta"],
            )
            out_path = os.path.join(MUXED_DIR, out_filename)

            if os.path.exists(out_path) and os.path.getsize(out_path) > 100 * 1024 * 1024:
                LOGGER.info(
                    f"📦 Merged part already exists: {out_filename} "
                    f"({os.path.getsize(out_path) / (1024*1024):.1f} MiB)"
                )
            else:
                LOGGER.info(
                    f"🔧 Merging Part {part_num}/{total_parts} (E{start_ep:02d}-E{end_ep:02d}) -> {out_filename}..."
                )
                cmd = [
                    "mkvmerge",
                    "--ui-language", "en_US",
                    "-o", out_path,
                    "--title", "[@Anime_Starfall🥰]",
                    "--append-mode", "file",
                    "--generate-chapters", "when-appending",
                ]

                if os.path.exists(COVER_PHOTO_PATH):
                    cmd.extend([
                        "--attachment-name", "cover.jpg",
                        "--attachment-mime-type", "image/jpeg",
                        "--attachment-description", "Starfall Official Release Cover",
                        "--attach-file", COVER_PHOTO_PATH,
                    ])

                cmd.append(episodes[0])
                for ep in episodes[1:]:
                    cmd.extend(["+", "--no-attachments", ep])

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode > 1:
                    stdout_str = stdout.decode(errors="replace")
                    LOGGER.warning(f"Standard merge failed ({proc.returncode}): {stdout_str[:250]}... Retrying with matched tracks (Video+Audio+MainSubs)...")

                    # Fallback 1: Retrying with Video + Audio (no subtitle mismatch)
                    cmd_fallback = [
                        "mkvmerge",
                        "--ui-language", "en_US",
                        "-o", out_path,
                        "--title", "[@Anime_Starfall🥰]",
                        "--append-mode", "file",
                        "--generate-chapters", "when-appending",
                    ]
                    if os.path.exists(COVER_PHOTO_PATH):
                        cmd_fallback.extend([
                            "--attachment-name", "cover.jpg",
                            "--attachment-mime-type", "image/jpeg",
                            "--attachment-description", "Starfall Official Release Cover",
                            "--attach-file", COVER_PHOTO_PATH,
                        ])

                    cmd_fallback.extend(["--no-subtitles", episodes[0]])
                    for ep in episodes[1:]:
                        cmd_fallback.extend(["+", "--no-attachments", "--no-subtitles", ep])

                    proc2 = await asyncio.create_subprocess_exec(
                        *cmd_fallback,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout2, stderr2 = await proc2.communicate()
                    if proc2.returncode > 1:
                        # Fallback 2: Match video + Tamil audio only
                        LOGGER.warning("Secondary merge failed. Retrying with Video + Primary Audio track...")
                        cmd_fallback2 = [
                            "mkvmerge",
                            "--ui-language", "en_US",
                            "-o", out_path,
                            "--title", "[@Anime_Starfall🥰]",
                            "--append-mode", "file",
                            "--generate-chapters", "when-appending",
                        ]
                        if os.path.exists(COVER_PHOTO_PATH):
                            cmd_fallback2.extend([
                                "--attachment-name", "cover.jpg",
                                "--attachment-mime-type", "image/jpeg",
                                "--attachment-description", "Starfall Official Release Cover",
                                "--attach-file", COVER_PHOTO_PATH,
                            ])
                        cmd_fallback2.extend(["-d", "0", "-a", "1", episodes[0]])
                        for ep in episodes[1:]:
                            cmd_fallback2.extend(["+", "--no-attachments", "-d", "0", "-a", "1", ep])
                        proc3 = await asyncio.create_subprocess_exec(
                            *cmd_fallback2,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout3, stderr3 = await proc3.communicate()
                        if proc3.returncode > 1:
                            raise RuntimeError(f"mkvmerge failed ({proc3.returncode}): {stdout3.decode(errors='replace')}")

                LOGGER.info(
                    f"✅ Merged successfully: {out_filename} "
                    f"({os.path.getsize(out_path) / (1024*1024):.1f} MiB, dur={exact_duration}s)"
                )

            tag = f"part_{part_num}"
            merged_files.append((tag, out_path, part_num, exact_duration))

        return merged_files

    def prepare_poster_and_caption(
        self,
        season_info: Dict[str, Any],
        hd_thumb: str,
    ) -> Tuple[str, str]:
        """Fetch AniList synopsis & rating, generate Template 11 Hero Card, return (poster_path, formatted_caption)."""
        title = season_info["title"]
        season = season_info["season"]
        year = season_info["year"]
        meta = season_info["meta"]

        # AniList query
        meta_ani = self.anilist.search_anime(title)
        if not meta_ani:
            # Fallback search with first 2 words
            words = title.split()[:2]
            meta_ani = self.anilist.search_anime(" ".join(words))

        rating = "8.0"
        genres = "Action, Animation, Drama"
        synopsis = (
            f"Official Starfall release for {title} Season {season}. "
            "High quality BluRay encode with crystal-clear Tamil audio and complete subtitles."
        )

        source_poster = hd_thumb
        if meta_ani:
            score = meta_ani.get("averageScore")
            if score:
                rating = f"{score / 10.0:.1f}"
            g_list = meta_ani.get("genres")
            if g_list:
                genres = ", ".join(g_list[:4])
            raw_syn = meta_ani.get("description")
            if raw_syn:
                clean_syn = re.sub(r"<[^>]+>", " ", raw_syn)
                clean_syn = re.sub(r"\s+", " ", clean_syn).strip()
                if len(clean_syn) > 550:
                    clean_syn = clean_syn[:547].rsplit(" ", 1)[0] + "..."
                synopsis = clean_syn

            # Download official AniList poster if possible
            dl_poster = self.anilist.download_season_poster(title, season, output_dir=THUMBS_DIR)
            if dl_poster and os.path.exists(dl_poster):
                source_poster = dl_poster

        # Generate Template 11 Flagship Hero Card
        slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
        hero_card_path = os.path.join(THUMBS_DIR, f"hero_card_{slug}_S{season:02d}.jpg")
        poster_path = source_poster
        try:
            t11_data = {
                "title": title,
                "year": year,
                "season": season,
                "episodes": season_info["total_eps"],
                "rating": rating,
                "genres": genres,
                "plot": synopsis,
                "resolution": meta.get("resolution", "1080P").upper(),
                "codec": meta.get("codec", "HEVC x265").upper(),
                "bit": meta.get("bit", "10bit"),
                "quality": meta.get("quality", "BluRay"),
                "audio": f"Tam + Jap {meta.get('audio_codec', 'AAC')} {meta.get('audio_channels', '2.0')}",
            }
            img = _template_eleven(bg=source_poster, side=source_poster, data=t11_data)
            img.save(hero_card_path, "JPEG", quality=95)
            poster_path = hero_card_path
            LOGGER.info(f"🎨 Generated Template 11 Hero Card: {hero_card_path} ({os.path.getsize(hero_card_path)} bytes)")
        except Exception as e:
            LOGGER.error(f"Failed to generate Template 11 Hero Card: {e}")

        quality_str = f"{meta['resolution']} {meta['bit']} {meta['quality']} {meta['codec']}"
        audio_str = f"Tam + Jap {meta['audio_codec']} {meta['audio_channels']} ~ {meta['shortsub']}"

        caption = (
            f"<b>「 {html.escape(title)} - {year} 」</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "╔════◇═══════════◇════\n"
            f"║ Season ➤ {season}\n"
            f"║ AniList ➤ {rating} Rating\n"
            f"║ Genres ➤ {html.escape(genres)}\n"
            f"║ Quality ➤ {html.escape(quality_str)}\n"
            f"║ Audio ➤ {html.escape(audio_str)}\n"
            "╚════◇═══════════◇════\n"
            " <blockquote expandable>Synopsis :\n   "
            f"{html.escape(synopsis)}"
            "</blockquote>\n\n"
            "Dropped By ➤ [@Anime_Starfall🥰]"
        )

        return poster_path, caption

    async def upload_file_to_dump(
        self,
        client: Client,
        filepath: str,
        caption: str,
        thumb_path: str,
        tag: str,
        dump_results: Dict[str, int],
        exact_duration: Optional[int] = None,
    ):
        """Upload a single file into the dump channel with retries and FloodWait protection."""
        fname = os.path.basename(filepath)
        # Ensure Japanese audio track header is tagged in container
        try:
            subprocess.run(
                ["mkvpropedit", filepath, "--edit", "track:a2", "--set", "name=Japanese [Original]", "--set", "language=jpn"],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

        fsize_mib = os.path.getsize(filepath) / (1024 * 1024)
        meta = probe_file_meta(filepath)
        final_duration = exact_duration if exact_duration is not None else meta["duration"]
        c_name = getattr(client, "name", "client")

        LOGGER.info(f"⬆️  [Dump] Uploading {fname} ({fsize_mib:.1f} MiB, dur={final_duration}s) via {c_name}...")
        t0 = time.time()

        for attempt in range(1, 6):
            try:
                msg: Message = await client.send_video(
                    chat_id=DUMP_CHANNEL_ID,
                    video=filepath,
                    caption=caption,
                    thumb=thumb_path,
                    video_cover=thumb_path,
                    duration=final_duration,
                    width=meta["width"],
                    height=meta["height"],
                    supports_streaming=True,
                    parse_mode=ParseMode.HTML,
                )
                elapsed = time.time() - t0
                speed_mbps = (fsize_mib * 8) / max(elapsed, 0.01)
                LOGGER.info(
                    f"✅ [Dump] Confirmed {fname} -> Msg #{msg.id} via {c_name} "
                    f"in {elapsed:.1f}s ({speed_mbps:.1f} Mbps, dur={final_duration}s)"
                )
                dump_results[tag] = msg.id
                return msg.id
            except FloodWait as fw:
                wait_s = int(getattr(fw, "value", 15)) + 2
                LOGGER.warning(f"⚠️ FloodWait {wait_s}s on {c_name} for {fname}. Sleeping...")
                await asyncio.sleep(wait_s)
            except RPCError as rpc:
                LOGGER.error(f"❌ RPC error on {c_name} for {fname} (attempt {attempt}): {rpc}")
                await asyncio.sleep(3.0 * attempt)
            except Exception as e:
                LOGGER.error(f"❌ Error on {c_name} for {fname} (attempt {attempt}): {e}")
                await asyncio.sleep(2.0 * attempt)

        raise RuntimeError(f"Failed to upload {filepath} after 5 attempts.")

    async def release_season(self, season_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute full end-to-end release for a single season:
        1. HD 16:9 Thumbnail preparation
        2. Merging partition plan & mkvmerge
        3. Parallel bot upload to Main Dump
        4. Sequential Target Channel publishing (Poster -> Merged Parts -> Singles)
        5. NVMe sweep of completed local files
        """
        title = season_info["title"]
        season = season_info["season"]
        total_eps = season_info["total_eps"]
        LOGGER.info("=" * 70)
        LOGGER.info(f"🚀 COMMENCING CHANNEL RELEASE: {title} S{season:02d} ({total_eps} episodes)")
        LOGGER.info(f"   Size: {season_info['total_size_gib']:.2f} GB")
        LOGGER.info("=" * 70)

        # 1. HD Landscape Thumbnail
        os.makedirs(THUMBS_DIR, exist_ok=True)
        raw_landscape = find_anime_landscape_thumbnail(title, season)
        if not raw_landscape:
            raise FileNotFoundError(f"Missing landscape thumbnail in data/thumbnails/ for {title} S{season:02d}")

        slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
        hd_thumb_path = os.path.join(THUMBS_DIR, f"hd_landscape_{slug}_S{season:02d}.jpg")
        master_hd_thumb = prepare_hd_thumbnail(raw_landscape, hd_thumb_path)
        LOGGER.info(f"🖼️ Master HD 16:9 Thumbnail: {master_hd_thumb} ({os.path.getsize(master_hd_thumb)} bytes)")

        # 1.5. Autonomous Audio Lip-Sync Pre-Release Audit (Multi-Core Parallel)
        workers_count = min(total_eps, 8)
        LOGGER.info(f"🔊 Running high-speed parallel acoustic audit ({workers_count} workers) on {total_eps} episodes of {title} S{season:02d}...")
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers_count) as executor:
            tasks = [
                loop.run_in_executor(executor, ensure_audio_sync_for_file, ep_path)
                for _, ep_path, _, _ in season_info["episodes"]
            ]
            await asyncio.gather(*tasks)

        # 2. Plan and Execute Chunks Merge
        merge_plan = self.plan_season_merge(season_info)
        LOGGER.info(f"📋 Merge plan for {title} S{season:02d}: {len(merge_plan)} part(s) <= 3,950 MiB")
        for p in merge_plan:
            LOGGER.info(f"   - Part {p['part_num']}: E{p['start_ep']:02d}-E{p['end_ep']:02d} (~{p['est_size_mib']:.1f} MiB)")

        merged_parts = await self.execute_merge(season_info, merge_plan)

        # 3. Stage Singles & Merged Parts to Dump Channel
        LOGGER.info("-" * 70)
        LOGGER.info(f"PHASE A: HIGH-SPEED PARALLEL UPLOAD -> MAIN DUMP ({DUMP_CHANNEL_ID})")
        LOGGER.info("-" * 70)

        dump_results: Dict[str, int] = {}
        single_episodes = season_info["episodes"]

        ep_queue: asyncio.Queue = asyncio.Queue()
        for ep_num, ep_path, _, _ in single_episodes:
            ep_queue.put_nowait((ep_num, ep_path))

        async def _bot_worker(bot_client: Client):
            while not ep_queue.empty():
                try:
                    ep_num, ep_path = ep_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                clean_name = os.path.basename(ep_path)
                await self.upload_file_to_dump(
                    client=bot_client,
                    filepath=ep_path,
                    caption=f"<b>{clean_name}</b>",
                    thumb_path=master_hd_thumb,
                    tag=f"ep_{ep_num:02d}",
                    dump_results=dump_results,
                )
                ep_queue.task_done()

        async def _user_worker():
            for p_tag, p_path, p_num, p_dur in merged_parts:
                clean_name = os.path.basename(p_path)
                await self.upload_file_to_dump(
                    client=self.user_client,
                    filepath=p_path,
                    caption=f"<b>{clean_name}</b>",
                    thumb_path=master_hd_thumb,
                    tag=p_tag,
                    dump_results=dump_results,
                    exact_duration=p_dur,
                )
            # Turbo acceleration: User Client joins ep_queue to upload singles at ~450 Mbps
            while not ep_queue.empty():
                try:
                    ep_num, ep_path = ep_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                clean_name = os.path.basename(ep_path)
                await self.upload_file_to_dump(
                    client=self.user_client,
                    filepath=ep_path,
                    caption=f"<b>{clean_name}</b>",
                    thumb_path=master_hd_thumb,
                    tag=f"ep_{ep_num:02d}",
                    dump_results=dump_results,
                )
                ep_queue.task_done()

        workers = [_user_worker()]
        for bot in self.helper_bots:
            workers.append(_bot_worker(bot))

        await asyncio.gather(*workers)

        LOGGER.info("=" * 70)
        LOGGER.info(f"✅ ALL ASSETS STAGED TO MAIN DUMP ({len(dump_results)} files confirmed)!")
        LOGGER.info("=" * 70)

        # 4. Sequential Forwarding to Target Channel
        LOGGER.info("-" * 70)
        LOGGER.info(f"PHASE B: SEQUENTIAL PUBLISHING -> TARGET CHANNEL ({TARGET_CHANNEL_ID})")
        LOGGER.info("-" * 70)

        target_links: List[Dict[str, Any]] = []
        step_counter = 1
        total_steps = 1 + len(merged_parts) + len(single_episodes)

        # 4.1. Poster Card (Template 11)
        poster_img, poster_caption = self.prepare_poster_and_caption(season_info, master_hd_thumb)
        LOGGER.info(f"📸 Step {step_counter}/{total_steps}: Posting Poster Card (Template 11)...")
        card_msg: Message = await self.user_client.send_photo(
            chat_id=TARGET_CHANNEL_ID,
            photo=poster_img,
            caption=poster_caption,
            parse_mode=ParseMode.HTML,
        )
        p_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{card_msg.id}"
        target_links.append({"step": step_counter, "title": "Poster Card (Template 11)", "url": p_link, "id": card_msg.id})
        LOGGER.info(f"✅ Poster Card published -> Msg #{card_msg.id} ({p_link})")
        LOGGER.info(f"⏳ Spacing {POST_SPACING_SEC}s...")
        await asyncio.sleep(POST_SPACING_SEC)
        step_counter += 1

        # 4.2. Merged Parts in order
        for p_tag, p_path, p_num, _ in sorted(merged_parts, key=lambda x: x[2]):
            LOGGER.info(f"📦 Step {step_counter}/{total_steps}: Forwarding Merged Part {p_num}...")
            p_msg: Message = await self.user_client.copy_message(
                chat_id=TARGET_CHANNEL_ID,
                from_chat_id=DUMP_CHANNEL_ID,
                message_id=dump_results[p_tag],
            )
            p_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{p_msg.id}"
            target_links.append({"step": step_counter, "title": f"Merged Part {p_num}", "url": p_link, "id": p_msg.id})
            LOGGER.info(f"✅ Merged Part {p_num} published -> Msg #{p_msg.id} ({p_link})")
            LOGGER.info(f"⏳ Spacing {POST_SPACING_SEC}s...")
            await asyncio.sleep(POST_SPACING_SEC)
            step_counter += 1

        # 4.3. Single Episodes in strict order
        for ep_idx, (ep_num, _, _, _) in enumerate(single_episodes, 1):
            LOGGER.info(f"🎬 Step {step_counter}/{total_steps}: Forwarding Episode {ep_num:02d}...")
            ep_msg: Message = await self.user_client.copy_message(
                chat_id=TARGET_CHANNEL_ID,
                from_chat_id=DUMP_CHANNEL_ID,
                message_id=dump_results[f"ep_{ep_num:02d}"],
            )
            ep_link = f"https://t.me/c/{TARGET_CLEAN_ID}/{ep_msg.id}"
            target_links.append({"step": step_counter, "title": f"Episode {ep_num:02d}", "url": ep_link, "id": ep_msg.id})
            LOGGER.info(f"✅ Episode {ep_num:02d} published -> Msg #{ep_msg.id} ({ep_link})")
            if ep_idx < len(single_episodes):
                LOGGER.info(f"⏳ Spacing {POST_SPACING_SEC}s...")
                await asyncio.sleep(POST_SPACING_SEC)
            step_counter += 1

        LOGGER.info("=" * 70)
        LOGGER.info(f"🎉 {title} S{season:02d} SUCCESSFULLY PUBLISHED TO TARGET CHANNEL!")
        LOGGER.info("=" * 70)

        # 5. Immediate Post-Release NVMe Storage Sweep
        LOGGER.info("-" * 70)
        LOGGER.info("PHASE C: POST-RELEASE LOCAL NVME STORAGE SWEEP")
        LOGGER.info("-" * 70)

        freed_bytes = 0
        purge_targets = [p[1] for p in merged_parts] + [e[1] for e in single_episodes]
        for f_to_del in purge_targets:
            try:
                if os.path.exists(f_to_del):
                    sz = os.path.getsize(f_to_del)
                    os.remove(f_to_del)
                    freed_bytes += sz
                    LOGGER.info(f"🧹 Purged local file: {os.path.basename(f_to_del)}")
            except Exception as e:
                LOGGER.warning(f"Failed to purge {f_to_del}: {e}")

        freed_gb = freed_bytes / (1024 ** 3)
        LOGGER.info(f"💾 Post-Release NVMe Sweep: Reclaimed {freed_gb:.2f} GB disk space!")

        return {
            "title": title,
            "season": season,
            "year": season_info["year"],
            "total_eps": total_eps,
            "merged_parts": len(merged_parts),
            "target_links": target_links,
            "freed_gb": freed_gb,
        }


async def main():
    parser = argparse.ArgumentParser(description="Starfall Master Automated Channel Dispatcher")
    parser.add_argument("--title", type=str, default=None, help="Filter specific anime title")
    parser.add_argument("--season", type=int, default=None, help="Filter specific season number")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of seasons to release")
    parser.add_argument("--order", type=str, choices=["size_asc", "size_desc", "title"], default="size_asc", help="Ordering of seasons")
    parser.add_argument("--dry-run", action="store_true", help="Print release plan without executing")
    args = parser.parse_args()

    dispatcher = MasterReleaseDispatcher()
    seasons = dispatcher.scan_seasons()

    if not seasons:
        LOGGER.warning(f"No completed seasons found in {MUXED_DIR}.")
        return

    # Filter if specified
    if args.title:
        clean_q = args.title.lower()
        seasons = [s for s in seasons if clean_q in s["title"].lower()]
    if args.season is not None:
        seasons = [s for s in seasons if s["season"] == args.season]

    # Ordering
    if args.order == "size_asc":
        seasons.sort(key=lambda s: s["total_size_bytes"])
    elif args.order == "size_desc":
        seasons.sort(key=lambda s: s["total_size_bytes"], reverse=True)
    elif args.order == "title":
        seasons.sort(key=lambda s: (s["title"].lower(), s["season"]))

    if args.limit:
        seasons = seasons[:args.limit]

    LOGGER.info("=" * 70)
    LOGGER.info(f"STARFALL MASTER DISPATCHER: {len(seasons)} SEASON(S) QUEUED FOR RELEASE")
    for idx, s in enumerate(seasons, 1):
        LOGGER.info(
            f"  {idx:02d}. {s['title']} S{s['season']:02d} ({s['year']}) - "
            f"{s['total_eps']} eps, {s['total_size_gib']:.2f} GB"
        )
    LOGGER.info("=" * 70)

    if args.dry_run:
        LOGGER.info("🔍 DRY-RUN MODE: Displaying partition plan only.")
        for s in seasons:
            plan = dispatcher.plan_season_merge(s)
            LOGGER.info(f"Season: {s['title']} S{s['season']:02d} ({s['total_eps']} eps, {s['total_size_gib']:.2f} GB)")
            for p in plan:
                fn = format_merged_filename(
                    title=s["title"],
                    season=s["season"],
                    start_ep=p["start_ep"],
                    end_ep=p["end_ep"],
                    part_num=p["part_num"],
                    total_parts=p["total_parts"],
                    meta_info=s["meta"],
                )
                LOGGER.info(f"  • Part {p['part_num']}/{p['total_parts']} (E{p['start_ep']:02d}-{p['end_ep']:02d}, ~{p['est_size_mib']:.1f} MiB): {fn}")
        return

    # Initialize Pyrogram Clients
    await dispatcher.initialize()

    published_seasons = []
    try:
        for idx, s in enumerate(seasons, 1):
            LOGGER.info(f"\n>>> PROCESSING QUEUE ITEM {idx}/{len(seasons)}: {s['title']} S{s['season']:02d} <<<")
            try:
                res = await dispatcher.release_season(s)
                published_seasons.append(res)
            except Exception as e:
                LOGGER.error(f"❌ Error releasing {s['title']} S{s['season']:02d}: {e}", exc_info=True)
            # Short breather between seasons
            await asyncio.sleep(5.0)
    finally:
        await dispatcher.close()

    print("\n" + "=" * 70)
    print("MASTER DISPATCHER COMPLETED RELEASES SUMMARY:")
    print("=" * 70)
    for res in published_seasons:
        print(f"\n🌟 {res['title']} S{res['season']:02d} ({res['total_eps']} eps) - Reclaimed {res['freed_gb']:.2f} GB:")
        for link in res["target_links"]:
            print(f"   [{link['step']:02d}] {link['title']}: {link['url']}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
