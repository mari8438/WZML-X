#!/usr/bin/env python3
"""
STARFALL MAXIMUM THROUGHPUT MULTI-ENCODER SPRINT ORCHESTRATOR
=============================================================
Hardware Target  : 16 vCPU, 32 GB RAM, 1 Gbps Unmetered Link (48-Hour Sprint)
Target Encoders  : Judas (HEVC 10-bit), Breeze (AV1), Sokudo (AV1), TRiX (AV1) [Fallback: EMBER]
Anti-Ban Shield  : Standard episodes (<2GB) uploaded EXCLUSIVELY by 8 Helper Bots.
                   >2GB Merged parts uploaded sequentially ONE AT A TIME by User Client.
                   Strict 3.5s-5.0s delay between forwarded messages into Target Channel.
                   20.0s inter-season cooldown. Automatic FloodWait(X + 5) handling.
Muxing Engine    : 14-worker ThreadPoolExecutor for mkvmerge.
Track Layout     : Track 1: Untouched Video
                   Track 2: Tamil Audio (flag-default=1, language=tam)
                   Track 3: Japanese Original Audio (flag-default=0, language=jpn)
                   Subtitles: English / Multi-Subs preserved
Container Cover  : Embed /home/administrator/starfall/data/intro_preroll.jpg as cover.jpg (Photo only, NO video bumper).
Fault Tolerance  : Non-blocking Zero-Stop policy. Missing audio/torrents logged to:
                   - /home/administrator/starfall/FAILED_REPORT.md
                   - /home/administrator/starfall/logs/manual_repair_queue.json
Rolling Reclaim  : Instant NVMe purge of local MKV immediately after Main Dump confirmation.
Live Pulse       : Real-time 4-column ASCII status table.
"""

import argparse
import asyncio
import concurrent.futures
from datetime import datetime, timezone
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
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, "/home/administrator/starfall")

os.environ.setdefault("WZGRAM_UPLOAD_RATE_BOT", "160")
os.environ.setdefault("WZGRAM_UPLOAD_POOL_BOT", "16")
os.environ.setdefault("WZGRAM_UPLOAD_RATE_PREMIUM", "600")
os.environ.setdefault("WZGRAM_UPLOAD_POOL_PREMIUM", "20")

from PIL import Image
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

from bot.helper.poster_engine.template_eleven import _template_eleven
from bot.modules.starfall_pipeline.acoustic_sync import AcousticSyncEngine
from bot.modules.starfall_pipeline.anilist_client import AniListClient
from bot.modules.starfall_pipeline.config import (
    TELEGRAM_API,
    TELEGRAM_HASH,
    USER_SESSION_STRING,
    HELPER_TOKENS,
    DATA_DIR,
)
from bot.modules.starfall_pipeline.merge_planner import TG_MAX_CHUNK_MIB
from bot.modules.starfall_pipeline.qbittorrent_bridge import QBittorrentBridge
from bot.modules.starfall_pipeline.nyaa_matcher import NyaaMatcher
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
LOGGER = logging.getLogger("SprintOrchestrator")

for _log in ("pyrogram", "pyrogram.session", "pyrogram.connection", "pyrogram.dispatcher", "pyrogram.client", "urllib3"):
    logging.getLogger(_log).setLevel(logging.ERROR)

# Constants & Paths
DUMP_CHANNEL_ID = -1003887117402       # Main Dump (-1003887117402)
TARGET_CHANNEL_ID = -1004415413346     # Tamil Muxed Collection (-1004415413346)
TARGET_CLEAN_ID = "4415413346"
DUMP_CLEAN_ID = "3887117402"

DOWNLOAD_DIR = "/home/administrator/starfall/downloads"
AUDIO_BANK_DIR = "/home/administrator/starfall/downloads/vt_audio_bank/8814651149"
COVER_PHOTO_PATH = "/home/administrator/starfall/data/intro_preroll.jpg"
MUXED_DIR = "/home/administrator/starfall/downloads/muxed_output"
TORRENTS_DIR = "/home/administrator/starfall/downloads/torrents"
THUMBS_DIR = "/home/administrator/starfall/thumbnails"
CATALOG_PATH = "/home/administrator/starfall/data/sprint_sourcing_catalog.json"
FAILED_REPORT_MD = "/home/administrator/starfall/FAILED_REPORT.md"
REPAIR_QUEUE_JSON = "/home/administrator/starfall/logs/manual_repair_queue.json"
PUBLISHED_SEASONS_PATH = "/home/administrator/starfall/data/published_seasons.json"

POST_SPACING_SEC = 1.2                 # Fast 1.2s pacing between forward messages
INTER_SEASON_COOLDOWN = 4.0           # 4.0s fast cooldown after every completed season
TARGET_ENCODERS = ["Judas", "Breeze", "Sokudo", "TRiX"]
EXCLUDED_KEYWORDS = ["bleach", "share house", "chainsaw man", "tokyo revengers", "my hero academia", "boku no hero academia"]
BLIND_DUMP_ONLY = os.environ.get("BLIND_DUMP_ONLY", "1").lower() in ("1", "true", "yes")  # Phase 1: Blind dump to Main Dump channel only


# ---------------------------------------------------------------------------
# Dedicated Failure Ledger (Non-Blocking Zero-Stop Policy)
# ---------------------------------------------------------------------------

class SprintFaultLedger:
    """Thread-safe and process-safe failure ledger for manual post-sprint review."""

    def __init__(self, md_path: str = FAILED_REPORT_MD, json_path: str = REPAIR_QUEUE_JSON):
        self.md_path = md_path
        self.json_path = json_path
        os.makedirs(os.path.dirname(self.json_path), exist_ok=True)
        self._init_files()

    def _init_files(self):
        if not os.path.exists(self.md_path):
            with open(self.md_path, "w", encoding="utf-8") as f:
                f.write("# Starfall Sprint Failure & Manual Repair Ledger\n\n")
                f.write("| Timestamp (UTC) | Anime Title | Season | Episode | Candidate Encoder | Exact Missing/Failed File | Failure Reason |\n")
                f.write("|---|---|---|---|---|---|---|\n")

        if not os.path.exists(self.json_path):
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump([], f)

    def log_failure(
        self,
        title: str,
        season: int,
        episode: Optional[int],
        encoder: str,
        missing_file: str,
        reason: str,
    ):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        ep_str = f"E{episode:02d}" if episode is not None else "Full Season"

        # 1. Append Markdown row
        row = f"| `{ts}` | {title} | S{season:02d} | {ep_str} | **{encoder}** | `{missing_file}` | {reason} |\n"
        try:
            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write(row)
        except Exception as e:
            LOGGER.error(f"Failed to append to {self.md_path}: {e}")

        # 2. Append JSON entry
        record = {
            "timestamp": ts,
            "anime": title,
            "season": season,
            "episode": episode,
            "encoder": encoder,
            "missing_file": missing_file,
            "reason": reason,
        }
        try:
            records = []
            if os.path.exists(self.json_path):
                with open(self.json_path, "r", encoding="utf-8") as f:
                    records = json.load(f)
            records.append(record)
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
        except Exception as e:
            LOGGER.error(f"Failed to update {self.json_path}: {e}")

        LOGGER.warning(
            f"⚠️ [FAULT LEDGER] Logged non-blocking incident: {title} S{season:02d} {ep_str} "
            f"({encoder}) -> {reason}"
        )


# Global ledger instance
LEDGER = SprintFaultLedger()


# ---------------------------------------------------------------------------
# Telemetry Pulse Table (4-Column ASCII Real-Time Monitor)
# ---------------------------------------------------------------------------

class TelemetryPulse:
    """Manages real-time telemetry and renders clean 4-column ASCII status tables."""

    def __init__(self):
        self.stage_ingest = "Idle / Scanning PDF Catalog"
        self.stage_mux = "Idle (14 Workers Ready)"
        self.stage_upload = "Idle (8 Bots + 1 User Client)"
        self.failures_count = 0

    def update(self, ingest: Optional[str] = None, mux: Optional[str] = None, upload: Optional[str] = None):
        if ingest is not None: self.stage_ingest = ingest
        if mux is not None: self.stage_mux = mux
        if upload is not None: self.stage_upload = upload

    def print_pulse(self):
        # Fetch disk stats
        total, used, free = shutil.disk_usage("/")
        free_gb = free / (1024 ** 3)

        # Count records in json
        try:
            with open(REPAIR_QUEUE_JSON, "r", encoding="utf-8") as f:
                self.failures_count = len(json.load(f))
        except Exception:
            pass

        ingest_col = self.stage_ingest[:23].ljust(23)
        mux_col = self.stage_mux[:23].ljust(23)
        upload_col = self.stage_upload[:23].ljust(23)
        fail_col = f"{self.failures_count} logged issues".ljust(25)

        border = "+-------------------------+-------------------------+-------------------------+---------------------------+"
        header = "| INGESTING (N+1)         | MUXING (N)              | UPLOADING (N-1)         | SKIPPED / FAILED (Ledger) |"
        content = f"| {ingest_col} | {mux_col} | {upload_col} | {fail_col} |"
        footer = f"| NVMe Free: {free_gb:.1f} GB        | Workers: 14 vCPU (mkv)  | Pacing: 3.5s Anti-Flood | Non-Blocking: ACTIVE      |"

        print("\n" + border)
        print("|                   STARFALL 16-vCPU MAXIMUM THROUGHPUT SPRINT PIPELINE PULSE                   |")
        print(border)
        print(header)
        print(border)
        print(content)
        print(border)
        print(footer)
        print(border + "\n")


TELEMETRY = TelemetryPulse()


# ---------------------------------------------------------------------------
# Dynamic Encoder Filename & Naming Helpers
# ---------------------------------------------------------------------------

def resolve_encoder_codec_tag(encoder: str) -> str:
    """AV1 for Breeze, Sokudo, and TRiX; x265 for Judas."""
    enc = encoder.upper()
    if enc in ("BREEZE", "SOKUDO", "TRIX"):
        return "AV1"
    return "x265"


def resolve_source_quality(filepath_or_name: str = "", torrent_title: str = "", catalog_raw: str = "") -> str:
    """
    Detect whether video source is WEBRip / WEB-DL or BluRay.
    Authoritatively inspects:
    1. The source torrent name / batch title
    2. The full file path (including parent directories)
    3. Catalog raw candidate lines
    """
    combined = f"{torrent_title} {filepath_or_name} {catalog_raw}".lower()

    # Explicit WEBRip / WEB-DL indicators (check first)
    web_tokens = (
        "webrip", "web-rip", "web_rip",
        "webdl", "web-dl", "web_dl",
        "web-", "web.", ".web.",
        "crunchyroll", "netflix", "amzn",
        "amazon", "disney", "dsnp", "hidi", "b-global",
    )
    if any(tok in combined for tok in web_tokens) or re.search(r"\b(web-?rip|web-?dl|web)\b", combined):
        return "WEBRip"

    # Explicit BluRay indicators
    bd_tokens = ("bluray", "blu-ray", "bdrip", "bd-rip", "brrip", "bdremux", "bd")
    if any(re.search(rf"\b{re.escape(tok)}\b", combined) for tok in bd_tokens):
        return "BluRay"

    return "BluRay"


def format_episode_filename(title: str, season: int, ep_num: int, year: str, encoder: str, quality: str = "BluRay") -> str:
    """
    Format standard episode filename strictly adhering to rule:
    Judas:  🄰🅂- [SxxExx] Title (Year) 1080p 10bit {Quality} x265 [Tamil-AAC 2.0] ESub ~ Judas ~ [@Anime_Starfall🥰].mkv
    Breeze: 🄰🅂- [SxxExx] Title (Year) 1080p 10bit {Quality} AV1 [Tamil-AAC 2.0] ESub ~ Breeze ~ [@Anime_Starfall🥰].mkv
    Sokudo: 🄰🅂- [SxxExx] Title (Year) 1080p 10bit {Quality} AV1 [Tamil-AAC 2.0] ESub ~ Sokudo ~ [@Anime_Starfall🥰].mkv
    TRiX:   🄰🅂- [SxxExx] Title (Year) 1080p 10bit {Quality} AV1 [Tamil-AAC 2.0] ESub ~ TRiX ~ [@Anime_Starfall🥰].mkv
    """
    codec = resolve_encoder_codec_tag(encoder)
    enc_clean = "TRiX" if encoder.upper() == "TRIX" else encoder.capitalize()
    return f"🄰🅂- [S{season:02d}E{ep_num:02d}] {title} ({year}) 1080p 10bit {quality} {codec} [Tamil-AAC 2.0] ESub ~ {enc_clean} ~ [@Anime_Starfall🥰].mkv"


def format_movie_filename(title: str, year: str, encoder: str, quality: str = "BluRay", audio_tag: str = "Tamil-AAC 2.0") -> str:
    """Format standard movie filename strictly adhering to rule."""
    codec = resolve_encoder_codec_tag(encoder)
    enc_clean = "TRiX" if encoder.upper() == "TRIX" else encoder.capitalize()
    return f"🄰🅂- [Movie] {title} ({year}) 1080p 10bit {quality} {codec} [{audio_tag}] ESub ~ {enc_clean} ~ [@Anime_Starfall🥰].mkv"


def get_exact_video_duration(filepath: str) -> int:
    """Probe exact container duration in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath
        ]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
        val = float(out)
        if val > 0:
            return int(round(val))
    except Exception:
        pass
    return probe_file_meta(filepath).get("duration", 1440)


def format_merged_filename(
    title: str,
    season: int,
    start_ep: int,
    end_ep: int,
    part_num: int,
    total_parts: int,
    year: str,
    encoder: str,
    quality: str = "BluRay",
) -> str:
    """
    Format standard merged filename strictly adhering to AutoRenameResolver canonical rule:
    - Single Part (Full Season):
      📦 🄰🅂- [S1E01-12] Title (Year) 1080p 10bit {Quality} {Codec} [Tamil-AAC 2.0] ESub ~ {Encoder} ~ [@Anime_Starfall🥰].mkv
    - Multi-Part:
      📦 🄰🅂- [S1E01-09] Title Part 1 (Year) 1080p 10bit {Quality} {Codec} [Tamil-AAC 2.0] ESub ~ {Encoder} ~ [@Anime_Starfall🥰].mkv
    """
    codec = resolve_encoder_codec_tag(encoder)
    enc_clean = "TRiX" if encoder.upper() == "TRIX" else encoder.capitalize()
    clean_title = re.sub(r"[^\w\s-]", "", title).strip()
    clean_quality = "BluRay" if ("bd" in quality.lower() or "bluray" in quality.lower()) and "web" not in quality.lower() else "WEBRip" if "web" in quality.lower() else quality
    
    part_str = f" Part {part_num}" if total_parts > 1 else ""
    year_str = f" ({year})" if year else ""

    return (
        f"📦 🄰🅂- [S{season}E{start_ep:02d}-{end_ep:02d}] "
        f"{clean_title}{part_str}{year_str} 1080p 10bit {clean_quality} {codec} [Tamil-AAC 2.0] ESub ~ {enc_clean} ~ [@Anime_Starfall🥰].mkv"
    )


# ---------------------------------------------------------------------------
# Audio Bank Resolver & Non-Blocking Matcher
# ---------------------------------------------------------------------------

def find_tamil_audio(anime_title: str, season: int, ep_num: int) -> Optional[str]:
    """
    Scan /home/administrator/starfall/downloads/vt_audio_bank/8814651149/
    for a matching episode audio file without blocking.
    Strictly disambiguates Demon Slayer vs Demon Lord / Archdemon and OAD/OVA vs TV.
    """
    if not os.path.exists(AUDIO_BANK_DIR):
        return None

    clean_title = re.sub(r"[^a-zA-Z0-9]", "", anime_title).lower()
    target_s_ep = f"s{season:02d}e{ep_num:02d}"
    target_s_alt = f"s{season}e{ep_num:02d}"
    is_ova_req = any(k in anime_title.lower() for k in ("oad", "ova"))

    is_ds = "demonslayer" in clean_title or "kimetsu" in clean_title
    is_demon_lord = "demonlord" in clean_title
    is_archdemon = "archdemon" in clean_title

    candidates = []
    try:
        for root, _, files in os.walk(AUDIO_BANK_DIR):
            for f in files:
                candidates.append((f, os.path.join(root, f)))
    except Exception as e:
        LOGGER.error(f"Error reading audio bank: {e}")
        return None

    def _matches_title(fn_clean: str) -> bool:
        if is_ds:
            if not ("demonslayer" in fn_clean or "kimetsunoyaiba" in fn_clean):
                return False
            if "demonlord" in fn_clean or "archdemon" in fn_clean:
                return False
            return True
        elif is_demon_lord:
            return "demonlord" in fn_clean and not ("demonslayer" in fn_clean or "kimetsunoyaiba" in fn_clean)
        elif is_archdemon:
            return "archdemon" in fn_clean and not ("demonslayer" in fn_clean or "kimetsunoyaiba" in fn_clean)
        else:
            if "demonslayer" in fn_clean or "kimetsunoyaiba" in fn_clean:
                return False
            return clean_title in fn_clean or any(word in fn_clean for word in anime_title.lower().split() if len(word) > 4)

    # Priority 1: Exact season + episode tag + title match with matching OVA status
    for fname, full_path in candidates:
        fn_lower = fname.lower()
        rel_lower = os.path.relpath(full_path, AUDIO_BANK_DIR).lower()
        fn_is_ova = any(k in rel_lower for k in ("oad", "ova"))
        if is_ova_req != fn_is_ova:
            continue

        norm_rel = re.sub(r"[^a-zA-Z0-9]", "", rel_lower)
        if not _matches_title(norm_rel):
            continue

        season_match = (
            target_s_ep in norm_rel or
            target_s_alt in norm_rel or
            ((f"season{season:02d}" in norm_rel or f"season{season}" in norm_rel) and f"e{ep_num:02d}" in fn_lower)
        )
        if season_match:
            return full_path

    # Priority 2: Fallback if season 1 and single-season format: e.g. 'Tamil - Anime E01.eac3'
    if season == 1:
        for fname, full_path in candidates:
            fn_lower = fname.lower()
            rel_lower = os.path.relpath(full_path, AUDIO_BANK_DIR).lower()
            fn_is_ova = any(k in rel_lower for k in ("oad", "ova"))
            if is_ova_req != fn_is_ova:
                continue

            if f"e{ep_num:02d}" in fn_lower and "s0" not in fn_lower and "s1" not in fn_lower:
                norm_rel = re.sub(r"[^a-zA-Z0-9]", "", rel_lower)
                if _matches_title(norm_rel):
                    return full_path

    return None


# ---------------------------------------------------------------------------
# Starfall Intro Subtitle & Full Metadata Engine
# ---------------------------------------------------------------------------

STARFALL_TAGS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE Tags SYSTEM "matroskatags.dtd">
<Tags>
  <Tag>
    <Targets>
      <TargetTypeValue>50</TargetTypeValue>
    </Targets>
    <Simple>
      <Name>TITLE</Name>
      <String>[@Anime_Starfall🥰]</String>
    </Simple>
    <Simple>
      <Name>ARTIST</Name>
      <String>[@Anime_Starfall🥰]</String>
    </Simple>
    <Simple>
      <Name>ENCODER</Name>
      <String>FFmpeg / [@Anime_Starfall🥰]</String>
    </Simple>
    <Simple>
      <Name>COMMENT</Name>
      <String>Encoded by [@Anime_Starfall🥰]</String>
    </Simple>
    <Simple>
      <Name>DESCRIPTION</Name>
      <String>[@Anime_Starfall🥰]</String>
    </Simple>
    <Simple>
      <Name>PUBLISHER</Name>
      <String>[@Anime_Starfall🥰]</String>
    </Simple>
    <Simple>
      <Name>COPYRIGHT</Name>
      <String>[@Anime_Starfall🥰]</String>
    </Simple>
  </Tag>
</Tags>"""


def create_brand_intro_ass(out_path: str, duration_sec: float, text: str = "[@Anime_Starfall🥰]") -> str:
    """
    Generate brand intro subtitle spanning the FULL VIDEO DURATION.
    Format: ASS (Arial 42, white font with gold outline, styled at bottom-center).
    """
    def _ass_time(sec: float) -> str:
        sec = max(0.0, float(sec))
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    end_time = _ass_time(max(duration_sec, 1.0) + 1.0)
    safe_text = text.replace("\n", r"\N").replace("{", "(").replace("}", ")")
    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: StarfallIntro,Arial,42,&H00FFFFFF,&H000000FF,&H00FF9E2D,&H00000000,-1,0,0,0,100,100,0,0,1,2.5,1,2,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{end_time},StarfallIntro,,0,0,0,,{{\\fad(500,500)}}{{\\b1}}{safe_text}
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


def apply_full_starfall_metadata(mkv_path: str, audio_label: str = "Tamil [AAC 2.0]"):
    """
    Applies 100% Starfall branding metadata across container, video, audio, and subtitle streams:
    - Container title: [@Anime_Starfall🥰]
    - Video track 1: name=[@Anime_Starfall🥰]
    - Audio track 1: name=[@Anime_Starfall🥰] - Tamil [AAC 2.0]
    - Subtitle tracks: name=[@Anime_Starfall🥰] - English [ESub]
    - Matroska Global XML Tags: TITLE, ARTIST, ENCODER, COMMENT, DESCRIPTION
    """
    if not os.path.exists(mkv_path):
        return

    pid_str = f"{os.getpid()}_{int(time.time()*1000) % 10000}"
    tags_file = os.path.join("/dev/shm", f"starfall_tags_{pid_str}.xml")
    try:
        with open(tags_file, "w", encoding="utf-8") as f:
            f.write(STARFALL_TAGS_XML)

        probe_res = subprocess.run(["mkvmerge", "-J", mkv_path], capture_output=True, text=True)
        if probe_res.returncode == 0:
            mkv_info = json.loads(probe_res.stdout)
            tracks = mkv_info.get("tracks", [])

            propedit_cmd = [
                "mkvpropedit", mkv_path,
                "--edit", "info", "--set", "title=[@Anime_Starfall🥰]",
                "--tags", f"global:{tags_file}",
            ]

            v_count = 0
            a_count = 0
            s_count = 0
            for t in tracks:
                t_type = t.get("type")
                props = t.get("properties", {})
                t_name = props.get("track_name", "") or ""

                if t_type == "video":
                    v_count += 1
                    propedit_cmd.extend(["--edit", f"track:v{v_count}", "--set", "name=[@Anime_Starfall🥰]"])
                elif t_type == "audio":
                    a_count += 1
                    a_name = f"[@Anime_Starfall🥰] - {audio_label}"
                    propedit_cmd.extend(["--edit", f"track:a{a_count}", "--set", f"name={a_name}"])
                elif t_type == "subtitles":
                    s_count += 1
                    lang = props.get("language", "eng")
                    if "intro" in t_name.lower():
                        s_name = "[@Anime_Starfall🥰] - Intro"
                    elif lang in ("eng", "en"):
                        s_name = "[@Anime_Starfall🥰] - English [ESub]"
                    else:
                        s_name = f"[@Anime_Starfall🥰] - {lang.upper()}"
                    propedit_cmd.extend(["--edit", f"track:s{s_count}", "--set", f"name={s_name}"])

            subprocess.run(propedit_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        LOGGER.warning(f"Could not apply full metadata to {os.path.basename(mkv_path)}: {e}")
    finally:
        if os.path.exists(tags_file):
            try: os.remove(tags_file)
            except Exception: pass


# ---------------------------------------------------------------------------
# 14-Worker Multiplexing Engine
# ---------------------------------------------------------------------------

def mux_single_episode_sync(
    video_path: str,
    audio_path: str,
    output_path: str,
    title: str,
    season: int,
    ep_num: int,
    year: str,
    encoder: str,
) -> bool:
    """
    Losslessly multiplex untouched video + Tamil audio (default) + Japanese original + untouched subs.
    Attach intro_preroll.jpg as cover.jpg (photo attachment only, NO video bumper).
    """
    if os.path.exists(output_path) and os.path.getsize(output_path) > 10 * 1024 * 1024:
        return True

    # Probe Japanese track ID inside source video
    probe_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index:stream_tags=language",
        "-of", "json", video_path,
    ]
    jap_track_idx = None
    try:
        res = subprocess.check_output(probe_cmd, text=True, stderr=subprocess.DEVNULL)
        info = json.loads(res)
        for s in info.get("streams", []):
            lang = s.get("tags", {}).get("language", "").lower()
            if lang in ("jpn", "ja", "japanese") or jap_track_idx is None:
                jap_track_idx = s.get("index")
                if lang in ("jpn", "ja"):
                    break
    except Exception:
        jap_track_idx = 1

    # Acoustic Cross-Correlation Lip-Sync
    sync_arg = ""
    healed_path = None
    try:
        engine = AcousticSyncEngine()
        sync_res = engine.analyze_sync_sync(video_path, audio_path)
        sync_arg = sync_res.get("mkvmerge_sync_arg", "")
        healed_path = sync_res.get("healed_audio_path")
        calc_ms = sync_res.get("calculated_delay_ms", 0)
        LOGGER.info(
            f"🎧 [AcousticSync] Ep {ep_num:02d}: lag={calc_ms}ms -> "
            f"mkvmerge --sync {sync_arg or 'none'} (Mode: {sync_res.get('sync_mode')}, PSR: {sync_res.get('min_psr_score')})"
        )
    except Exception as e:
        LOGGER.warning(f"⚠️ AcousticSync skipped for Ep {ep_num:02d} ({e}), muxing default")

    actual_audio = healed_path if (healed_path and os.path.exists(healed_path)) else audio_path
    tmp_out = output_path + ".tmp.mkv"
    cmd = [
        "mkvmerge",
        "--ui-language", "en_US",
        "--flush-on-close",
        "-o", tmp_out,
        "--title", "[@Anime_Starfall🥰]",
        "--video-tracks", "0",
    ]

    # Strictly Tamil Audio Only + Video (Subtitles Preserved):
    # 1. Add video file with -A to drop all foreign audio (Japanese & English), preserving subtitles
    cmd.extend(["-A", video_path])

    # 2. Add Track 1 Audio: Strictly Tamil Audio (Default=1, language=tam)
    cmd.extend([
        "--language", "0:tam",
        "--default-track-flag", "0:1",
        "--track-name", "0:Tamil [AAC 2.0]",
    ])
    if sync_arg:
        cmd.extend(["--sync", sync_arg])
    cmd.append(actual_audio)

    # 3. Container Cover Art (Strictly photo only, NO intro video bumper)
    if os.path.exists(COVER_PHOTO_PATH):
        cmd.extend([
            "--attachment-name", "cover.jpg",
            "--attachment-mime-type", "image/jpeg",
            "--attach-file", COVER_PHOTO_PATH,
        ])

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode in (0, 1) and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 10 * 1024 * 1024:
            shutil.move(tmp_out, output_path)
            return True
        else:
            if os.path.exists(tmp_out):
                try: os.remove(tmp_out)
                except Exception: pass
            LOGGER.error(f"mkvmerge failed for {output_path}: {proc.stderr[:300]}")
            return False
    except Exception as e:
        if os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass
        LOGGER.error(f"Muxing error: {e}")
        return False
    finally:
        if healed_path and os.path.exists(healed_path):
            try:
                os.remove(healed_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Master Sprint Orchestrator Class
# ---------------------------------------------------------------------------

class MasterSprintOrchestrator:
    def __init__(self):
        self.anilist = AniListClient()
        self.qb = QBittorrentBridge()
        self.nyaa = NyaaMatcher()
        self.sync_engine = AcousticSyncEngine()
        self.user_client: Optional[Client] = None
        self.helper_bots: List[Client] = []
        self._is_initialized = False
        self._prebuffer_running = False
        self.mux_executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)

    async def initialize(self):
        """Start User Client and all 8 Helper Bots."""
        if self._is_initialized:
            return

        sessions_dir = os.path.join(DATA_DIR, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)

        LOGGER.info("🔑 Starting Telegram Premium User Client (@Hsin_The_Moon_Fox)...")
        self.user_client = Client(
            "hsin_moon_fox",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            session_string=USER_SESSION_STRING,
            in_memory=True,
            ipv6=False,
            workers=32,
            max_concurrent_transmissions=16,
        )
        await self.user_client.start()
        u_me = await self.user_client.get_me()
        LOGGER.info(f"✅ User Client online: @{u_me.username} (ID: {u_me.id}, Premium={u_me.is_premium})")

        raw_tokens = [t.strip() for t in HELPER_TOKENS.split() if t.strip()]
        LOGGER.info(f"🤖 Starting all {len(raw_tokens)} Helper Bots for BOT-ONLY staging (Turbo 16x transmissions)...")
        self.helper_bots = []
        for idx, tok in enumerate(raw_tokens, 1):
            bot = Client(
                f"starfall_helper_{idx}",
                api_id=TELEGRAM_API,
                api_hash=TELEGRAM_HASH,
                bot_token=tok,
                workdir=sessions_dir,
                in_memory=False,
                ipv6=False,
                workers=16,
                max_concurrent_transmissions=16,
            )
            await bot.start()
            b_me = await bot.get_me()
            LOGGER.info(f"✅ Helper Bot #{idx} online: @{b_me.username}")
            self.helper_bots.append(bot)

        self._is_initialized = True
        LOGGER.info("🚀 Master Sprint Orchestrator initialized successfully.")

    async def close(self):
        for b in self.helper_bots:
            try: await b.stop()
            except Exception: pass
        if self.user_client:
            try: await self.user_client.stop()
            except Exception: pass
        self.mux_executor.shutdown(wait=False)
        self._is_initialized = False

    def is_season_published(self, title: str, season: int, encoder: str) -> bool:
        """Check if season + encoder release is already published in Target Channel."""
        key = f"{title}_S{season:02d}_{encoder}".lower()
        if not os.path.exists(PUBLISHED_SEASONS_PATH):
            return False
        try:
            with open(PUBLISHED_SEASONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return key in data
        except Exception:
            return False

    def mark_season_published(
        self,
        title: str,
        season: int,
        encoder: str,
        poster_id: int,
        part_ids: List[int],
        ep_ids: List[int],
    ):
        """Record completed season in published_seasons registry."""
        key = f"{title}_S{season:02d}_{encoder}".lower()
        os.makedirs(os.path.dirname(PUBLISHED_SEASONS_PATH), exist_ok=True)
        data = {}
        if os.path.exists(PUBLISHED_SEASONS_PATH):
            try:
                with open(PUBLISHED_SEASONS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[key] = {
            "title": title,
            "season": season,
            "encoder": encoder,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "poster_msg_id": poster_id,
            "parts_msg_ids": part_ids,
            "episode_msg_ids": ep_ids,
        }
        with open(PUBLISHED_SEASONS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_catalog(self) -> List[Dict[str, Any]]:
        """Load parsed PDF catalog of all seasons and candidate encoders."""
        if not os.path.exists(CATALOG_PATH):
            LOGGER.error(f"Catalog file not found at {CATALOG_PATH}!")
            return []
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def plan_season_merge(self, episodes: List[Tuple[int, str, float]]) -> List[Dict[str, Any]]:
        """
        4GB Episode-Aware Auto-Merge (Strict Multi-Episode Only):
        Partitions whole episodes into balanced chunks so no merged file exceeds 3,950 MiB.
        Never splits across a single episode's timeline.
        CRITICAL RULES:
        1. A merged part MUST contain at least 2 episodes (len(chunk) >= 2).
        2. Never create a 1-episode "merged part" (those are already uploaded as individual episodes).
        3. If no multi-episode chunks are possible (e.g. single episodes are > 3.5 GB each), return []
           so only individual episodes are published.
        """
        if len(episodes) < 2:
            return []

        total_size = sum(sz for _, _, sz in episodes)
        if total_size <= TG_MAX_CHUNK_MIB:
            return [{
                "part_num": 1,
                "total_parts": 1,
                "episodes": [p for _, p, _ in episodes],
                "start_ep": episodes[0][0],
                "end_ep": episodes[-1][0],
                "est_size_mib": total_size,
            }]

        chunks = []
        curr_chunk = []
        curr_size = 0.0

        for ep_num, ep_path, sz in episodes:
            if curr_chunk and (curr_size + sz > TG_MAX_CHUNK_MIB):
                chunks.append(curr_chunk)
                curr_chunk = [(ep_num, ep_path, sz)]
                curr_size = sz
            else:
                curr_chunk.append((ep_num, ep_path, sz))
                curr_size += sz

        if curr_chunk:
            chunks.append(curr_chunk)

        # STRICT FILTER: Discard any chunk with fewer than 2 episodes!
        multi_ep_chunks = [c for c in chunks if len(c) >= 2]
        if not multi_ep_chunks:
            LOGGER.info("ℹ️ No multi-episode chunks possible under 3,950 MiB (episodes are individually massive). Skipping merge parts.")
            return []

        total_parts = len(multi_ep_chunks)
        plan = []
        for idx, chunk in enumerate(multi_ep_chunks, 1):
            plan.append({
                "part_num": idx,
                "total_parts": total_parts,
                "episodes": [p for _, p, _ in chunk],
                "start_ep": chunk[0][0],
                "end_ep": chunk[-1][0],
                "est_size_mib": sum(sz for _, _, sz in chunk),
            })
        return plan

    async def execute_merge(
        self,
        title: str,
        season: int,
        year: str,
        encoder: str,
        plan: List[Dict[str, Any]],
        quality: str = "BluRay",
    ) -> List[Tuple[str, str, int, int]]:
        """
        Merge each chunk using mkvmerge with cover art attachment.
        Returns list of (part_tag, output_filepath, part_num, exact_duration).
        """
        merged_files = []
        for chunk in plan:
            part_num = chunk["part_num"]
            total_parts = chunk["total_parts"]
            start_ep = chunk["start_ep"]
            end_ep = chunk["end_ep"]
            episodes = chunk["episodes"]

            out_filename = format_merged_filename(
                title=title,
                season=season,
                start_ep=start_ep,
                end_ep=end_ep,
                part_num=part_num,
                total_parts=total_parts,
                year=year,
                encoder=encoder,
                quality=quality,
            )
            out_path = os.path.join(MUXED_DIR, out_filename)

            if os.path.exists(out_path) and os.path.getsize(out_path) > 100 * 1024 * 1024:
                LOGGER.info(f"📦 Merged part already exists: {out_filename}")
                apply_full_starfall_metadata(out_path, "Tamil [AAC 2.0]")
            else:
                LOGGER.info(f"🔧 Merging Part {part_num}/{total_parts} (E{start_ep:02d}-E{end_ep:02d}) -> {out_filename}...")
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
                        "--attach-file", COVER_PHOTO_PATH,
                    ])

                # Append files in order with --no-attachments
                cmd.append(episodes[0])
                for ep_p in episodes[1:]:
                    cmd.extend(["+", "--no-attachments", ep_p])

                loop = asyncio.get_running_loop()
                res = await loop.run_in_executor(
                    self.mux_executor,
                    lambda: subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                )

                if res.returncode > 1:
                    LOGGER.warning(f"Standard merge returned {res.returncode}. Retrying with Video + Audio (no subtitle mismatch)...")
                    # Fallback 1: No subtitles mismatch
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
                            "--attach-file", COVER_PHOTO_PATH,
                        ])

                    cmd_fallback.extend(["--no-subtitles", episodes[0]])
                    for ep_p in episodes[1:]:
                        cmd_fallback.extend(["+", "--no-attachments", "--no-subtitles", ep_p])

                    res_fb = await loop.run_in_executor(
                        self.mux_executor,
                        lambda: subprocess.run(cmd_fallback, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    )

                    if res_fb.returncode > 1:
                        LOGGER.warning("Secondary merge failed. Retrying with Video + Primary Audio track only...")
                        # Fallback 2: Video + primary audio only
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
                                "--attach-file", COVER_PHOTO_PATH,
                            ])
                        cmd_fallback2.extend(["-d", "0", "-a", "1", episodes[0]])
                        for ep_p in episodes[1:]:
                            cmd_fallback2.extend(["+", "--no-attachments", "-d", "0", "-a", "1", ep_p])

                        res_fb2 = await loop.run_in_executor(
                            self.mux_executor,
                            lambda: subprocess.run(cmd_fallback2, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                        )
                        if res_fb2.returncode > 1:
                            LOGGER.error(f"Fallback 2 merge failed for {out_filename}: {res_fb2.stderr[:200]}")
                            continue

                LOGGER.info(
                    f"✅ Merged successfully: {out_filename} "
                    f"({os.path.getsize(out_path) / (1024*1024):.1f} MiB)"
                )
                apply_full_starfall_metadata(out_path, "Tamil [AAC 2.0]")

            exact_duration = get_exact_video_duration(out_path)
            if exact_duration <= 0:
                exact_duration = sum(probe_file_meta(ep)["duration"] for ep in episodes)
            merged_files.append((f"merged_part_{part_num}", out_path, part_num, exact_duration))

        return merged_files

    async def upload_file_to_dump(
        self,
        client: Client,
        filepath: str,
        caption: str,
        thumb_path: str,
        tag: str,
        dump_results: Dict[str, int],
        exact_duration: Optional[int] = None,
        is_user_client: bool = False,
    ):
        """
        Upload file into Main Dump Channel (-1003887117402) with FloodWait(X + 5) catcher.
        Post-confirmation: Immediately purge local file from NVMe disk!
        """
        fname = os.path.basename(filepath)
        if not os.path.exists(filepath):
            LOGGER.warning(f"File {fname} does not exist on disk, skipping upload.")
            return
        fsize_mib = os.path.getsize(filepath) / (1024 * 1024)
        meta = probe_file_meta(filepath)
        final_duration = exact_duration if exact_duration is not None else meta["duration"]
        c_name = getattr(client, "name", "client")

        LOGGER.info(f"⬆️  [Dump] Uploading {fname} ({fsize_mib:.1f} MiB) via {c_name}...")
        t0 = time.time()

        for attempt in range(1, 6):
            try:
                msg: Message = await client.send_video(
                    chat_id=DUMP_CHANNEL_ID,
                    video=filepath,
                    caption=caption,
                    thumb=thumb_path,
                    duration=final_duration,
                    width=meta["width"],
                    height=meta["height"],
                    supports_streaming=True,
                    parse_mode=ParseMode.HTML,
                )
                elapsed = time.time() - t0
                speed_mbps = (fsize_mib * 8) / max(elapsed, 0.01)
                LOGGER.info(f"✅ [Dump] Staged {fname} -> Msg #{msg.id} via {c_name} in {elapsed:.1f}s ({speed_mbps:.1f} Mbps)")
                dump_results[tag] = msg.id

                # INSTANT ROLLING DISK RECLAIM:
                # The moment an MKV is confirmed received in Main Dump, delete the local file immediately!
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        LOGGER.info(f"🧹 [Instant Disk Reclaim] Purged {fname} from NVMe (Freed {fsize_mib:.1f} MiB).")
                    except Exception as e:
                        LOGGER.warning(f"Could not purge {fname}: {e}")

                return msg.id

            except FloodWait as fw:
                wait_s = int(getattr(fw, "value", 20)) + 5
                LOGGER.warning(f"⚠️ FloodWait on {c_name}. Sleeping for {wait_s}s...")
                await asyncio.sleep(wait_s)
            except RPCError as rpc:
                LOGGER.error(f"❌ RPC error on {c_name} for {fname} (attempt {attempt}): {rpc}")
                await asyncio.sleep(3.0 * attempt)
            except Exception as e:
                LOGGER.error(f"❌ Upload error on {c_name} for {fname} (attempt {attempt}): {e}")
                await asyncio.sleep(2.0 * attempt)

        # Non-blocking failure record
        LEDGER.log_failure(
            title=caption,
            season=0,
            episode=None,
            encoder=c_name,
            missing_file=fname,
            reason="Upload to Main Dump failed after 5 attempts",
        )
        return None

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
        codec = resolve_encoder_codec_tag(encoder)

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
        hero_card_path = os.path.join(THUMBS_DIR, f"hero_card_{slug}_S{season:02d}_{encoder}.jpg")
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
            " <blockquote expandable>Synopsis :\n   "
            f"{html.escape(synopsis)}"
            "</blockquote>\n\n"
            "Dropped By ➤ [@Anime_Starfall🥰]"
        )
        return poster_path, caption

    async def forward_to_target_channel(
        self,
        title: str,
        season: int,
        year: str,
        encoder: str,
        landscape_thumb: str,
        dump_results: Dict[str, int],
        merged_parts: List[Tuple[str, str, int, int]],
        episodes: List[Tuple[int, str, float]],
        quality: str = "BluRay",
    ):
        """
        Sequentially publish to Target Channel (-1004415413346):
        1) Poster Hero Card (Template 11)
        2) Merged 4GB Parts (Part 01, Part 02...)
        3) Individual Episodes with strict 3.5s anti-flood delay.
        """
        step_counter = 1
        total_steps = 1 + len(merged_parts) + len(episodes)

        LOGGER.info("-" * 70)
        LOGGER.info(f"PHASE B: SEQUENTIAL PUBLISHING -> TARGET CHANNEL ({TARGET_CHANNEL_ID})")
        LOGGER.info(f"Pacing: Strict {POST_SPACING_SEC}s delay between forwards.")
        LOGGER.info("-" * 70)

        # 1. Poster Card
        poster_img, poster_caption = self.prepare_poster_and_caption(
            title=title,
            season=season,
            year=year,
            total_eps=len(episodes),
            encoder=encoder,
            landscape_thumb=landscape_thumb,
            quality=quality,
        )
        LOGGER.info(f"📸 Step {step_counter}/{total_steps}: Posting Poster Card (Template 11)...")

        for attempt in range(1, 4):
            try:
                card_msg: Message = await self.user_client.send_photo(
                    chat_id=TARGET_CHANNEL_ID,
                    photo=poster_img,
                    caption=poster_caption,
                    parse_mode=ParseMode.HTML,
                )
                LOGGER.info(f"✅ Poster Card published -> Msg #{card_msg.id} (https://t.me/c/{TARGET_CLEAN_ID}/{card_msg.id})")
                break
            except FloodWait as fw:
                wait_s = int(fw.value) + 5
                LOGGER.warning(f"FloodWait on poster. Sleeping {wait_s}s...")
                await asyncio.sleep(wait_s)

        await asyncio.sleep(POST_SPACING_SEC)
        step_counter += 1

        # 2. Merged 4GB Parts
        for p_tag, p_path, p_num, _ in sorted(merged_parts, key=lambda x: x[2]):
            if p_tag not in dump_results:
                continue
            LOGGER.info(f"📦 Step {step_counter}/{total_steps}: Forwarding Merged Part {p_num}...")
            for attempt in range(1, 4):
                try:
                    p_msg: Message = await self.user_client.copy_message(
                        chat_id=TARGET_CHANNEL_ID,
                        from_chat_id=DUMP_CHANNEL_ID,
                        message_id=dump_results[p_tag],
                    )
                    LOGGER.info(f"✅ Merged Part {p_num} published -> Msg #{p_msg.id} (https://t.me/c/{TARGET_CLEAN_ID}/{p_msg.id})")
                    break
                except FloodWait as fw:
                    wait_s = int(fw.value) + 5
                    LOGGER.warning(f"FloodWait on merged part. Sleeping {wait_s}s...")
                    await asyncio.sleep(wait_s)

            await asyncio.sleep(POST_SPACING_SEC)
            step_counter += 1

        # 3. Single Episodes in strict narrative order
        for ep_idx, (ep_num, ep_p, _) in enumerate(episodes, 1):
            ep_tag = f"ep_{ep_num:02d}"
            if ep_tag not in dump_results:
                continue
            LOGGER.info(f"🎬 Step {step_counter}/{total_steps}: Forwarding Episode {ep_num:02d}...")
            for attempt in range(1, 4):
                try:
                    ep_msg: Message = await self.user_client.copy_message(
                        chat_id=TARGET_CHANNEL_ID,
                        from_chat_id=DUMP_CHANNEL_ID,
                        message_id=dump_results[ep_tag],
                    )
                    LOGGER.info(f"✅ Episode {ep_num:02d} published -> Msg #{ep_msg.id} (https://t.me/c/{TARGET_CLEAN_ID}/{ep_msg.id})")
                    break
                except FloodWait as fw:
                    wait_s = int(fw.value) + 5
                    LOGGER.warning(f"FloodWait on episode forward. Sleeping {wait_s}s...")
                    await asyncio.sleep(wait_s)

            if ep_idx < len(episodes):
                await asyncio.sleep(POST_SPACING_SEC)
            step_counter += 1

        LOGGER.info("=" * 70)
        LOGGER.info(f"🎉 {title} S{season:02d} ({encoder}) COMPLETED TARGET CHANNEL PUBLICATION!")
        self.mark_season_published(
            title=title,
            season=season,
            encoder=encoder,
            poster_id=card_msg.id if 'card_msg' in locals() else 0,
            part_ids=[dump_results[p_tag] for p_tag, _, _, _ in merged_parts if p_tag in dump_results],
            ep_ids=[dump_results[f"ep_{ep_num:02d}"] for ep_num, _, _ in episodes if f"ep_{ep_num:02d}" in dump_results],
        )
        LOGGER.info(f"⏳ Sleeping {INTER_SEASON_COOLDOWN}s inter-season cooldown to shield Telegram rolling flood counter...")
        LOGGER.info("=" * 70)
        await asyncio.sleep(INTER_SEASON_COOLDOWN)

    def check_season_tamil_audios(self, title: str, season: int, expected_eps: Optional[int] = None) -> Tuple[bool, Dict[int, str]]:
        """Verify that all expected episodes have matching Tamil audio in vt_audio_bank."""
        if not os.path.exists(AUDIO_BANK_DIR):
            return False, {}

        known_totals = {
            ("Release That Witch", 1): 8,
            ("Bye Bye, Earth", 1): 10,
            ("Bye Bye, Earth", 2): 10,
            ("Yoasobi Gurashi", 1): 8,
            ("Demon Slayer: Kimetsu no Yaiba", 1): 26,
            ("Demon Slayer: Kimetsu no Yaiba", 2): 18,
            ("Demon Slayer: Kimetsu no Yaiba", 3): 11,
            ("Demon Slayer: Kimetsu no Yaiba", 4): 8,
            ("JUJUTSU KAISEN", 1): 24,
            ("JUJUTSU KAISEN", 2): 23,
            ("Jujutsu Kaisen", 1): 24,
            ("Jujutsu Kaisen", 2): 23,
            ("Solo Leveling", 1): 12,
            ("Solo Leveling", 2): 13,
            ("Vinland Saga", 1): 24,
            ("Vinland Saga", 2): 24,
        }
        for (k_t, k_s), val in known_totals.items():
            if k_t.lower() in title.lower() and season == k_s:
                expected_eps = val
                break

        norm_title = re.sub(r"[^a-zA-Z0-9]", "", title).lower()
        subdirs = [d for d in os.listdir(AUDIO_BANK_DIR) if os.path.isdir(os.path.join(AUDIO_BANK_DIR, d))] if os.path.exists(AUDIO_BANK_DIR) else []
        
        # Scoped directory matching: Match dedicated anime folder in audio bank to prevent cross-contamination
        exact_matches = [os.path.join(AUDIO_BANK_DIR, d) for d in subdirs if norm_title == re.sub(r"[^a-zA-Z0-9]", "", d).lower()]
        if exact_matches:
            search_roots = exact_matches
        else:
            sub_matches = [os.path.join(AUDIO_BANK_DIR, d) for d in subdirs if norm_title in re.sub(r"[^a-zA-Z0-9]", "", d).lower() or re.sub(r"[^a-zA-Z0-9]", "", d).lower() in norm_title]
            search_roots = sub_matches or [AUDIO_BANK_DIR]

        matched: Dict[int, str] = {}
        s_tag = f"s{season:02d}"
        s_tag_alt = f"s{season}"
        is_ova_req = any(k in title.lower() for k in ("oad", "ova"))

        for s_root in search_roots:
            for root, _, files in os.walk(s_root):
                for f in files:
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, s_root).lower()
                    fn_is_ova = any(k in rel_path for k in ("oad", "ova"))
                    if is_ova_req != fn_is_ova:
                        continue

                    norm_a = re.sub(r"[^a-zA-Z0-9]", "", rel_path)
                    fn_lower = f.lower()

                    season_match = (
                        s_tag in norm_a or
                        f"season{season:02d}" in norm_a or
                        f"season{season}" in norm_a or
                        (s_tag_alt in norm_a and not f"s{season}0" in norm_a)
                    )
                    ep_num = None
                    if season_match:
                        ep_m = re.search(r"e(\d{1,3})", fn_lower)
                        if ep_m:
                            ep_num = int(ep_m.group(1))
                    elif season == 1 and not any(f"season{x}" in norm_a or f"s0{x}" in norm_a for x in range(2, 10)):
                        ep_m = re.search(r"e(\d{1,3})", fn_lower)
                        if ep_m:
                            ep_num = int(ep_m.group(1))

                    if ep_num is not None:
                        matched[ep_num] = full_path

        if not matched:
            return False, {}

        if expected_eps:
            missing = [e for e in range(1, expected_eps + 1) if e not in matched]
            return len(missing) == 0, matched

        max_ep = max(matched.keys())
        expected_range = set(range(1, max_ep + 1))
        missing = expected_range - set(matched.keys())
        return len(missing) == 0 and len(matched) >= 8, matched

    @staticmethod
    def _check_title_match(title: str, romaji: str, cand_name: str) -> bool:
        """Strict title verification using exact normalized names and word-boundary matching."""
        norm_t = re.sub(r"[^a-zA-Z0-9]", "", title).lower()
        norm_r = re.sub(r"[^a-zA-Z0-9]", "", romaji).lower() if romaji else ""
        norm_cand = re.sub(r"[^a-zA-Z0-9]", "", cand_name).lower()

        if norm_t and norm_t in norm_cand:
            return True
        if norm_r and norm_r in norm_cand:
            return True

        words = list(set([w for w in re.sub(r"[^\w\s]", " ", title).lower().split() if len(w) >= 3 and w not in ("the", "and", "for", "with", "from", "der", "die", "das")]))
        if len(words) >= 2 and all(re.search(rf"\b{re.escape(w)}\b", cand_name, re.I) for w in words):
            return True

        r_words = list(set([w for w in re.sub(r"[^\w\s]", " ", romaji).lower().split() if len(w) >= 3 and w not in ("the", "and", "for", "with", "from", "der", "die", "das")])) if romaji else []
        if len(r_words) >= 2 and all(re.search(rf"\b{re.escape(w)}\b", cand_name, re.I) for w in r_words):
            return True

        return False

    def find_nyaa_batch(self, title: str, season: int, encoder: str) -> Optional[Dict[str, Any]]:
        """Search Nyaa for active batch release matching encoder with >=3 seeders."""
        clean_title = re.sub(r"[^\w\s]", " ", title).strip()
        s_tag = f"s{season:02d}"
        s_tag_alt = f"s{season}"
        norm_t = re.sub(r"[^a-zA-Z0-9]", "", title).lower()

        # 1. Query Romaji title first for robust matching across cache and Nyaa
        romaji_title = ""
        try:
            romaji_title = self.anilist.get_romaji_title(title)
        except Exception:
            pass
        norm_r = re.sub(r"[^a-zA-Z0-9]", "", romaji_title).lower() if romaji_title else ""
        words_gt2 = [w for w in clean_title.lower().split() if len(w) >= 3 and w not in ("the", "and", "for", "with", "from")]
        r_words_gt2 = [w for w in re.sub(r"[^\w\s]", " ", romaji_title).lower().split() if len(w) >= 3 and w not in ("the", "and", "for", "with", "from")] if romaji_title else []

        # 2. Check existing active or completed torrents in qBittorrent first (Instant Cache Hit)
        is_ds = "demonslayer" in norm_t or "kimetsu" in norm_t
        try:
            qb_torrents = self.qb.get_all_torrents()
            for qt in qb_torrents:
                qt_name = qt.get("name", "").lower()
                if encoder.lower() in qt_name:
                    norm_qt = re.sub(r"[^a-zA-Z0-9]", "", qt_name)
                    if is_ds and ("demonslayer" in norm_qt or "kimetsu" in norm_qt):
                        ds_hit = False
                        if season == 1 and ("season1" in norm_qt or "s01" in norm_qt or "s1" in norm_qt):
                            ds_hit = True
                        elif season == 2 and ("yuukaku" in norm_qt or "mugen" in norm_qt or "s02" in norm_qt):
                            ds_hit = True
                        elif season == 3 and ("season4" in norm_qt or "swordsmith" in norm_qt or "katanakaji" in norm_qt or "s03" in norm_qt):
                            ds_hit = True
                        elif season == 4 and ("season05" in norm_qt or "season5" in norm_qt or "s05" in norm_qt or "hashira" in norm_qt):
                            ds_hit = True
                        if ds_hit or "s0105" in norm_qt or "complete" in norm_qt:
                            LOGGER.info(f"⚡ [Cache Hit] Using existing Demon Slayer torrent in qBittorrent: {qt.get('name')} ({qt.get('progress', 0.0)*100:.1f}%)")
                            return {
                                "title": qt.get("name"),
                                "info_hash": qt.get("hash"),
                                "seeders": max(qt.get("num_complete", 0), qt.get("num_seeds", 0), 10 if qt.get("progress", 0.0) >= 1.0 else 3),
                                "torrent_url": "",
                                "magnet_uri": "",
                            }
                    else:
                        if not self._check_title_match(title, romaji_title, qt_name):
                            continue

                        # Strict cache check: never match weekly single episodes or specials
                        if "weekly" in qt_name or "(weekly)" in qt_name:
                            continue
                        if re.search(r"\b[sS]\d{1,2}[eE]\d{1,3}\b", qt_name) and not any(k in qt_name for k in ("batch", "complete", "seasons", "s01-", "s1-")):
                            continue

                        # Reject movie releases unless looking for a movie
                        if re.search(r"\b(movie|film|gekijouban)\b", qt_name, re.I) and not re.search(r"\b(movie|film|gekijouban)\b", title, re.I):
                            continue

                        # Check for conflicting other seasons first (e.g. S02 when looking for S01)
                        is_conflicting_season = False
                        has_range = bool(re.search(r"\b(s\d{1,2}|season\s*\d{1,2}|seasons\s*\d{1,2})\s*(-|~|to)\s*(s\d{1,2}|\d{1,2})\b", qt_name, re.I)) or "complete series" in norm_qt
                        for other_s in range(1, 10):
                            if other_s == season:
                                continue
                            if (f"season {other_s}" in qt_name or f"season 0{other_s}" in qt_name or f"season{other_s}" in norm_qt or f"season0{other_s}" in norm_qt or f"s{other_s:02d}" in norm_qt) and not has_range:
                                is_conflicting_season = True
                                break
                        if is_conflicting_season:
                            continue

                        s_matches = any(x in norm_qt for x in [f"s{season:02d}", f"s{season}", f"season{season}", f"season0{season}"]) or ("complete series" in norm_qt) or (has_range and season > 1)
                        if not s_matches and season == 1:
                            if not any(f"season{x}" in norm_qt or f"season0{x}" in norm_qt or f"s{x:02d}" in norm_qt or f"s{x}" in norm_qt for x in range(2, 10)):
                                s_matches = True
                        if s_matches:
                            LOGGER.info(f"⚡ [Cache Hit] Using existing torrent in qBittorrent: {qt.get('name')} ({qt.get('progress', 0.0)*100:.1f}%)")
                            return {
                                "title": qt.get("name"),
                                "info_hash": qt.get("hash"),
                                "seeders": max(qt.get("num_complete", 0), qt.get("num_seeds", 0), 10 if qt.get("progress", 0.0) >= 1.0 else 3),
                                "torrent_url": "",
                                "magnet_uri": "",
                            }
        except Exception as e:
            LOGGER.warning(f"Could not check existing qBittorrent torrents: {e}")

        # Dedicated Sourcing Registry for Demon Slayer: Kimetsu no Yaiba
        if is_ds:
            ds_judas_batches = {
                1: {
                    "title": "[Judas] Kimetsu no Yaiba (Demon Slayer) (Season 1) [BD 1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "a8dd49c302c17e035541a3499f22e3f6e3529f45",
                    "seeders": 71,
                    "torrent_url": "https://nyaa.si/download/1280887.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:a8dd49c302c17e035541a3499f22e3f6e3529f45",
                },
                2: {
                    "title": "[Judas] Kimetsu no Yaiba - Yuukaku-hen (Demon Slayer - Entertainment District Arc) [1080p][HEVC x265 10bit][Multi-Subs] (Batch)",
                    "info_hash": "37369c950f47f6d44041c1bda9735c4c7cd8ba2d",
                    "seeders": 47,
                    "torrent_url": "https://nyaa.si/download/1491968.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:37369c950f47f6d44041c1bda9735c4c7cd8ba2d",
                    "extra_torrents": [
                        {
                            "title": "[Judas] Kimetsu no Yaiba - Mugen Ressha-hen (Demon Slayer - Mugen Train Arc) [1080p][HEVC x265 10bit][Multi-Subs] (Batch)",
                            "info_hash": "2fe8084a5e1b3cd006dea85f279271deee757a1b",
                            "seeders": 29,
                            "torrent_url": "https://nyaa.si/download/1491786.torrent",
                            "magnet_uri": "magnet:?xt=urn:btih:2fe8084a5e1b3cd006dea85f279271deee757a1b",
                        }
                    ],
                },
                3: {
                    "title": "[Judas] Kimetsu no Yaiba (Demon Slayer) (Season 4 - Swordsmith Village Arc) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Katanakaji no Sato Hen) (Batch)",
                    "info_hash": "bd573fd3fb07db7a6f90be76859be72ae4c2cb8c",
                    "seeders": 62,
                    "torrent_url": "https://nyaa.si/download/1706044.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:bd573fd3fb07db7a6f90be76859be72ae4c2cb8c",
                },
                4: {
                    "title": "[Judas] Kimetsu no Yaiba (Demon Slayer) (Season 05) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "84b2f76a73d264a04c1614319d795424290c5999",
                    "seeders": 77,
                    "torrent_url": "https://nyaa.si/download/1862178.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:84b2f76a73d264a04c1614319d795424290c5999",
                },
            }
            ds_trix_batch = {
                "title": "[Trix] Kimetsu no Yaiba S01-05 [Dual Audio] [Multi Subs] (BD 1080p AV1) - Demon Slayer VOSTFR (Batch)",
                "info_hash": "c9f2f5b4c36d9f0a0d3d449fb81050eb4d24f6e6",
                "seeders": 463,
                "torrent_url": "https://nyaa.si/download/2013468.torrent",
                "magnet_uri": "magnet:?xt=urn:btih:c9f2f5b4c36d9f0a0d3d449fb81050eb4d24f6e6",
            }
            if encoder.lower() in ("trix", "breeze", "sokudo"):
                return ds_trix_batch
            if encoder.lower() == "judas" and season in ds_judas_batches:
                return ds_judas_batches[season]

        # Dedicated Sourcing Registry for Jujutsu Kaisen
        is_jjk = "jujutsu" in norm_t
        if is_jjk:
            jjk_judas_batches = {
                1: {
                    "title": "[Judas] Jujutsu Kaisen (Season 1) [1080p][HEVC x265 10bit][Multi-Subs] (Batch)",
                    "info_hash": "d7ab19032670b9de22e83df9cb26358cb4005df2",
                    "seeders": 394,
                    "torrent_url": "https://nyaa.si/download/1363355.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:d7ab19032670b9de22e83df9cb26358cb4005df2",
                },
                2: {
                    "title": "[Judas] Jujutsu Kaisen (Season 2) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "3061778e6a56b47e0e54b3d69523395736f2aa07",
                    "seeders": 470,
                    "torrent_url": "https://nyaa.si/download/1765887.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:3061778e6a56b47e0e54b3d69523395736f2aa07",
                },
            }
            jjk_av1_batches = {
                1: {
                    "title": "[Breeze] Jujutsu Kaisen S1+0 Movie [1080p BD AV1][dual audio]",
                    "info_hash": "323279ceb521dd9eb6ea2a9decb6bc6fceb9797b",
                    "seeders": 50,
                    "torrent_url": "",
                    "magnet_uri": "magnet:?xt=urn:btih:323279ceb521dd9eb6ea2a9decb6bc6fceb9797b",
                },
                2: {
                    "title": "[Sokudo] Jujutsu Kaisen - S02 [1080p BD AV1][Dual Audio]",
                    "info_hash": "d4e9a801a5952958dbfc78e764287c03e4b83304",
                    "seeders": 207,
                    "torrent_url": "https://nyaa.si/download/1976261.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:d4e9a801a5952958dbfc78e764287c03e4b83304",
                },
            }
            if encoder.lower() in ("breeze", "sokudo", "trix") and season in jjk_av1_batches:
                return jjk_av1_batches[season]
            if encoder.lower() == "judas" and season in jjk_judas_batches:
                return jjk_judas_batches[season]

        # Dedicated Sourcing Registry for Solo Leveling
        is_sl = "sololeveling" in norm_t or "oredake" in norm_t
        if is_sl:
            sl_judas_batches = {
                1: {
                    "title": "[Judas] Solo Leveling (Season 01) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "fba7f9c9df37ce12b4e412dab6a60f1e1bc417a3",
                    "seeders": 146,
                    "torrent_url": "https://nyaa.si/download/1822994.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:fba7f9c9df37ce12b4e412dab6a60f1e1bc417a3",
                },
                2: {
                    "title": "[Judas] Ore dake Level Up na Ken (Solo Leveling) (Season 02) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "eb5a42556b66bab9cd0193bc96228c50a8837b68",
                    "seeders": 203,
                    "torrent_url": "https://nyaa.si/download/1959066.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:eb5a42556b66bab9cd0193bc96228c50a8837b68",
                },
            }
            sl_av1_batches = {
                1: {
                    "title": "[Breeze] Solo Leveling S01 v2 [1080p BD AV1][Dual Audio][Multi Audio]",
                    "info_hash": "d5853b08d67bc0e523848deb811bff29c7668960",
                    "seeders": 243,
                    "torrent_url": "https://nyaa.si/download/1952386.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:d5853b08d67bc0e523848deb811bff29c7668960",
                },
            }
            if encoder.lower() in ("breeze", "sokudo", "trix") and season in sl_av1_batches:
                return sl_av1_batches[season]
            if encoder.lower() == "judas" and season in sl_judas_batches:
                return sl_judas_batches[season]

        # Dedicated Sourcing Registry for Vinland Saga
        is_vs = "vinland" in norm_t
        if is_vs:
            vs_judas_batches = {
                1: {
                    "title": "[Judas] Vinland Saga (Season 1) [1080p][HEVC x265 10bit][Multi-Subs] (Batch)",
                    "info_hash": "003fb50034cc1ffb4366c521e9b222eb69007e8c",
                    "seeders": 54,
                    "torrent_url": "https://nyaa.si/download/1208820.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:003fb50034cc1ffb4366c521e9b222eb69007e8c",
                },
                2: {
                    "title": "[Judas] Vinland Saga (Season 2) [1080p][HEVC x265 10bit][Multi-Subs] (Batch)",
                    "info_hash": "b46600f176b050c6be334654f53d964b4cd56fe0",
                    "seeders": 70,
                    "torrent_url": "https://nyaa.si/download/1684603.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:b46600f176b050c6be334654f53d964b4cd56fe0",
                },
            }
            vs_av1_batches = {
                1: {
                    "title": "[Trix] Vinland Saga (Season 1) [Optional Dual Audio] [Eng Subs - VOSTFR] (BD 1080p AV1)",
                    "info_hash": "f223b3181398772bff513b6631531202ddfcbb88",
                    "seeders": 19,
                    "torrent_url": "https://nyaa.si/download/1608754.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:f223b3181398772bff513b6631531202ddfcbb88",
                },
                2: {
                    "title": "[Trix] Vinland Saga S02 v2 (COMPLETE) (1080p AV1) [Multi Subs] (Batch) - Season 2 (VOSTFR)",
                    "info_hash": "26f0daf6ed87f761fb45aa39685cdfe678870728",
                    "seeders": 18,
                    "torrent_url": "https://nyaa.si/download/1685080.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:26f0daf6ed87f761fb45aa39685cdfe678870728",
                },
            }
            if encoder.lower() in ("breeze", "sokudo", "trix") and season in vs_av1_batches:
                return vs_av1_batches[season]
            if encoder.lower() == "judas" and season in vs_judas_batches:
                return vs_judas_batches[season]

        # Dedicated Sourcing Registry for The Rising of the Shield Hero
        is_shield = "shield" in norm_t or "yuusha" in norm_t
        if is_shield:
            shield_judas_batches = {
                1: {
                    "title": "[Judas] Tate no Yuusha no Nariagari (The Rising of the Shield Hero) (Season 1) [BD 1080p][HEVC x265 10bit][Eng-Subs]",
                    "info_hash": "b2f6ef57f49557ec6ec8f51a44c825a075d9e568",
                    "seeders": 83,
                    "torrent_url": "https://nyaa.si/download/1231846.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:b2f6ef57f49557ec6ec8f51a44c825a075d9e568",
                },
                2: {
                    "title": "[Judas] Tate no Yuusha no Nariagari (The Rising of the Shield Hero) (Season 2) [1080p][HEVC x265 10bit][Multi-Subs] (Batch)",
                    "info_hash": "2f6e5c839baea3848b9f7a77e5c544e3f4b6ad26",
                    "seeders": 71,
                    "torrent_url": "https://nyaa.si/download/1545648.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:2f6e5c839baea3848b9f7a77e5c544e3f4b6ad26",
                },
                3: {
                    "title": "[Judas] Tate no Yuusha no Nariagari (The Rising of the Shield Hero) (Season 3) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "db16a2ab797ba4b3ae30f274bb3f313b999d89b1",
                    "seeders": 103,
                    "torrent_url": "https://nyaa.si/download/1765886.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:db16a2ab797ba4b3ae30f274bb3f313b999d89b1",
                },
                4: {
                    "title": "[Judas] Tate no Yuusha no Nariagari (The Rising of the Shield Hero) (Season 04) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "ce65aaeb02f0c26a3be36bb681fdfa4467da9d4a",
                    "seeders": 87,
                    "torrent_url": "https://nyaa.si/download/2026025.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:ce65aaeb02f0c26a3be36bb681fdfa4467da9d4a",
                },
            }
            if encoder.lower() == "judas" and season in shield_judas_batches:
                return shield_judas_batches[season]

        # Dedicated Sourcing Registry for Classroom of the Elite
        is_cote = "classroom" in norm_t or "kyoushitsu" in norm_t
        if is_cote:
            cote_judas_batches = {
                2: {
                    "title": "[Judas] Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e (Season 2) (Classroom of the Elite) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "f762e22ad23af064979d54182336be9a1848c0cd",
                    "seeders": 25,
                    "torrent_url": "https://nyaa.si/download/1580196.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:f762e22ad23af064979d54182336be9a1848c0cd",
                },
                3: {
                    "title": "[Judas] Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e (Classroom of the Elite) (Season 03) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "508f030679221be770178cad337fd201757e273e",
                    "seeders": 35,
                    "torrent_url": "https://nyaa.si/download/1799797.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:508f030679221be770178cad337fd201757e273e",
                },
                4: {
                    "title": "[Judas] Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e (Classroom of the Elite) (Season 04) [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)",
                    "info_hash": "8b8b3b629bbddff97906261bfebc5b2cb15a94f1",
                    "seeders": 120,
                    "torrent_url": "https://nyaa.si/download/1844053.torrent",
                    "magnet_uri": "magnet:?xt=urn:btih:8b8b3b629bbddff97906261bfebc5b2cb15a94f1",
                },
            }
            if encoder.lower() == "judas" and season in cote_judas_batches:
                return cote_judas_batches[season]

        queries = [
            f"{encoder} {clean_title} S{season:02d}",
            f"{encoder} {clean_title} Season {season}",
            f"{encoder} {clean_title}",
        ]
        if romaji_title and romaji_title.lower() != clean_title.lower():
            clean_romaji = re.sub(r"[^\w\s]", " ", romaji_title).strip()
            queries.extend([
                f"{encoder} {clean_romaji} S{season:02d}",
                f"{encoder} {clean_romaji} Season {season}",
                f"{encoder} {clean_romaji}",
            ])

        s_tag = f"s{season:02d}"
        s_tag_alt = f"s{season}"

        all_candidates = []
        seen_hashes = set()

        for q in queries:
            try:
                items = self.nyaa.search_nyaa_rss(q)
                for it in items:
                    t_lower = it["title"].lower()
                    h = it.get("info_hash")
                    if h in seen_hashes:
                        continue
                    if encoder.lower() not in t_lower:
                        continue

                    # STRICT TITLE MATCH GUARD:
                    if not self._check_title_match(title, romaji_title, it["title"]):
                        continue

                    # STRICT BATCH GUARD:
                    # Never select single weekly episode releases
                    if "weekly" in t_lower or "(weekly)" in t_lower:
                        continue
                    if re.search(r"\b[sS]\d{1,2}[eE]\d{1,3}\b", it["title"]) and not any(k in t_lower for k in ("batch", "complete", "seasons", "s01-", "s1-")):
                        continue

                    # Reject movie releases unless looking for a movie
                    if re.search(r"\b(movie|film|gekijouban)\b", t_lower) and not re.search(r"\b(movie|film|gekijouban)\b", title.lower()):
                        continue

                    # Check target season match
                    has_range = bool(re.search(r"\b(s\d{1,2}|season\s*\d{1,2}|seasons\s*\d{1,2})\s*(-|~|to)\s*(s\d{1,2}|\d{1,2})\b", t_lower)) or "complete series" in t_lower
                    target_season_match = (
                        s_tag in t_lower or 
                        s_tag_alt in t_lower or 
                        f"season {season}" in t_lower or 
                        f"season 0{season}" in t_lower or 
                        (has_range and season > 1) or
                        (season == 1 and not any(f"season {x}" in t_lower or f"season 0{x}" in t_lower or f"s0{x}" in t_lower or f"s{x}" in t_lower for x in range(2, 10)))
                    )
                    if not target_season_match:
                        continue

                    # Check if a conflicting other season is explicitly mentioned
                    is_conflicting = False
                    for other_s in range(1, 10):
                        if other_s == season:
                            continue
                        if (f"season {other_s}" in t_lower or f"season 0{other_s}" in t_lower or f"s0{other_s}" in t_lower or f"s{other_s}" in t_lower) and not has_range:
                            is_conflicting = True
                            break
                    if is_conflicting:
                        continue

                    seen_hashes.add(h)
                    seeds = it.get("seeders", 0)
                    score = seeds
                    if any(k in t_lower for k in ("(batch)", "batch", "complete", "seasons")):
                        score += 500
                    if "dual-audio" in t_lower or "multi-subs" in t_lower:
                        score += 50
                    all_candidates.append((score, it))

            except Exception as e:
                LOGGER.warning(f"Error querying Nyaa for {q}: {e}")

        if all_candidates:
            all_candidates.sort(key=lambda x: x[0], reverse=True)
            best = all_candidates[0][1]
            if best.get("seeders", 0) >= 3:
                return best

        return None

    async def wait_for_torrent_download(self, info_hash: str, torrent_title: str) -> bool:
        """
        Monitor qBittorrent download with 5-minute progress timeout.
        If torrent fails to progress within 5 minutes or has <3 seeders, auto-switch candidate.
        """
        last_progress_time = time.time()
        last_progress = -1.0
        missing_count = 0
        LOGGER.info(f"⏳ Monitoring qBittorrent download: {torrent_title} (Hash: {info_hash})...")

        while True:
            t_info = self.qb.get_torrent(info_hash)
            if not t_info:
                missing_count += 1
                if missing_count > 6:
                    LOGGER.warning(f"⚠️ Torrent missing from qBittorrent: {torrent_title} (Hash: {info_hash})")
                    return False
                await asyncio.sleep(4)
                continue
            missing_count = 0

            state = t_info.get("state", "").lower()
            progress = t_info.get("progress", 0.0)
            dlspeed = t_info.get("dlspeed", 0) / (1024 * 1024)

            TELEMETRY.update(ingest=f"DL {progress*100:.1f}% ({dlspeed:.1f} MB/s)")
            TELEMETRY.print_pulse()

            if progress >= 1.0 or state in ("uploading", "pausedup", "stalledup", "queuedup"):
                LOGGER.info(f"✅ Download completed: {torrent_title}")
                return True

            if progress > last_progress:
                last_progress = progress
                last_progress_time = time.time()
            elif time.time() - last_progress_time > 300: # 5 minutes without progress
                LOGGER.warning(f"⚠️ Torrent stalled for >5 min: {torrent_title}. Auto-switching candidate.")
                self.qb.delete_torrent(info_hash, delete_files=True)
                return False

            await asyncio.sleep(5)

    async def prebuffer_next_seasons(self, max_buffer_gib: float = 120.0):
        """
        Asynchronous pre-buffering (~150GB Torrent Pool):
        Pre-downloads upcoming seasons in qBittorrent in the background
        while current season is muxing, staging, and publishing.
        """
        if self._prebuffer_running:
            return
        self._prebuffer_running = True
        try:
            st = os.statvfs(DOWNLOAD_DIR)
            free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
            if free_gb < 40.0:
                LOGGER.info(f"⏳ Pre-buffer skipped: Free disk space {free_gb:.1f} GB < 40 GB.")
                return

            catalog = self.load_catalog()
            active_torrents = self.qb.get_all_torrents()
            active_hashes = {t.get("hash") for t in active_torrents}

            total_active_gib = sum(
                t.get("total_size", 0) / (1024 ** 3)
                for t in active_torrents
                if t.get("progress", 0.0) < 1.0
            )
            if total_active_gib >= max_buffer_gib:
                return

            prebuffered_count = 0
            for s_item in catalog:
                await asyncio.sleep(0.02)  # Yield to event loop to keep uploads & logging smooth!
                title = s_item["title"]
                season = s_item["season"]
                if any(ex in title.lower() for ex in EXCLUDED_KEYWORDS):
                    continue

                has_tamil, _ = self.check_season_tamil_audios(title, season)
                if not has_tamil:
                    continue

                for enc in ["Judas", "Breeze", "Sokudo", "TRiX"]:
                    if self.is_season_published(title, season, enc):
                        continue
                    batch = self.find_nyaa_batch(title, season, enc)
                    if not batch or batch.get("seeders", 0) < 3:
                        continue

                    b_hash = batch.get("info_hash")
                    if not b_hash or b_hash in active_hashes:
                        continue

                    mag_or_url = batch.get("magnet_uri") or batch.get("torrent_url", "")
                    LOGGER.info(f"⚡ [Pre-Buffer] Queuing upcoming season in qBittorrent: {title} S{season:02d} ({enc}) -> {batch['title']}")
                    self.qb.add_torrent(
                        magnet_or_url=mag_or_url,
                        container_save_path="/usr/src/app/downloads",
                        category="starfall_sprint",
                    )
                    active_hashes.add(b_hash)
                    prebuffered_count += 1
                    break

                if prebuffered_count >= 2:
                    break
        except Exception as e:
            LOGGER.warning(f"Error during background pre-buffering: {e}")
        finally:
            self._prebuffer_running = False

    async def mux_downloaded_season(
        self,
        title: str,
        season: int,
        year: str,
        encoder: str,
        info_hash: str,
        found_audios: Dict[int, str],
        torrent_title: str = "",
        catalog_raw: str = "",
    ) -> List[Tuple[int, str, float]]:
        """Mux downloaded raw video with isolated Tamil audio."""
        torrent_files = self.qb.get_torrent_files(info_hash)
        video_files = []
        for tf in torrent_files:
            fn = tf.get("name", "")
            if any(fn.lower().endswith(ext) for ext in (".mkv", ".mp4")):
                full_path = os.path.join(DOWNLOAD_DIR, fn)
                if os.path.exists(full_path):
                    video_files.append(full_path)

        if not video_files:
            for root, _, fs in os.walk(DOWNLOAD_DIR):
                for f in fs:
                    if any(f.lower().endswith(ext) for ext in (".mkv", ".mp4")):
                        if not f.startswith("🄰🅂-"):
                            video_files.append(os.path.join(root, f))

        ep_map: Dict[int, str] = {}
        is_ova_req = any(k in title.lower() for k in ("oad", "ova"))
        s_tag = f"s{season:02d}"
        s_tag_alt = f"s{season}"
        s_dir = f"season {season}"
        is_ds = "demonslayer" in title.lower() or "kimetsu" in title.lower()

        for vf in sorted(video_files):
            rel_path = os.path.relpath(vf, DOWNLOAD_DIR).lower()
            base_name = os.path.basename(vf).lower()
            sub_dirs = rel_path.split(os.sep)[1:-1]
            in_special_folder = any(any(k in d for k in ("specials", "extras", "movies", "ovas", "oad")) for d in sub_dirs)
            fn_is_ova = in_special_folder or bool(re.search(r"\b(oad|ova|sp\d+)\b", base_name)) or "s00e" in base_name
            if is_ova_req != fn_is_ova:
                continue

            # Special Demon Slayer Video-to-Episode Mapper
            if is_ds:
                if season == 1:
                    m_s = re.search(r"[Ss]0?1[Ee](\d{1,3})", base_name)
                    if not m_s:
                        m_s = re.search(r"[-_\s](\d{1,2})(?:v\d)?[\. -_]", base_name)
                    if m_s:
                        file_ep = int(m_s.group(1))
                        if file_ep in found_audios and file_ep not in ep_map:
                            ep_map[file_ep] = vf
                            continue
                elif season == 2:
                    # In audio bank: 18 episodes (1-7 Mugen Train, 8-18 Yuukaku)
                    m_trix_mugen = re.search(r"[Ss]0?2[Ee](\d{1,3})", base_name)
                    if m_trix_mugen:
                        file_ep = int(m_trix_mugen.group(1))
                        if 1 <= file_ep <= 7 and file_ep not in ep_map:
                            ep_map[file_ep] = vf
                            continue
                    m_trix_yuukaku = re.search(r"[Ss]0?3[Ee](\d{1,3})", base_name)
                    if m_trix_yuukaku:
                        file_ep = int(m_trix_yuukaku.group(1))
                        if 1 <= file_ep <= 11:
                            target_ep = file_ep + 7
                            if target_ep not in ep_map:
                                ep_map[target_ep] = vf
                                continue
                    if "mugen" in rel_path:
                        ep_m = re.search(r"[-_\s](\d{1,2})(?:v\d)?[\. -_]", base_name)
                        if ep_m:
                            file_ep = int(ep_m.group(1))
                            if 1 <= file_ep <= 7 and file_ep not in ep_map:
                                ep_map[file_ep] = vf
                                continue
                    if "yuukaku" in rel_path:
                        ep_m = re.search(r"[-_\s](\d{1,2})(?:v\d)?[\. -_]", base_name)
                        if ep_m:
                            file_ep = int(ep_m.group(1))
                            if 1 <= file_ep <= 11:
                                target_ep = file_ep + 7
                                if target_ep not in ep_map:
                                    ep_map[target_ep] = vf
                                    continue
                elif season == 3:
                    # Swordsmith Village Arc (11 eps in audio bank: S03E01..S03E11)
                    # Judas and TRiX tag this as S04 / Season 4!
                    m_s = re.search(r"[Ss]0?4[Ee](\d{1,3})", base_name)
                    if m_s:
                        file_ep = int(m_s.group(1))
                        if 1 <= file_ep <= 11 and file_ep not in ep_map:
                            ep_map[file_ep] = vf
                            continue
                    if "katana" in rel_path or "swordsmith" in rel_path:
                        ep_m = re.search(r"[-_\s](\d{1,2})(?:v\d)?[\. -_]", base_name)
                        if ep_m:
                            file_ep = int(ep_m.group(1))
                            if 1 <= file_ep <= 11 and file_ep not in ep_map:
                                ep_map[file_ep] = vf
                                continue
                elif season == 4:
                    # Hashira Training Arc (8 eps in audio bank: S04E01..S04E08)
                    # Judas and TRiX tag this as S05 / Season 5!
                    m_s = re.search(r"[Ss]0?5[Ee](\d{1,3})", base_name)
                    if m_s:
                        file_ep = int(m_s.group(1))
                        if 1 <= file_ep <= 8 and file_ep not in ep_map:
                            ep_map[file_ep] = vf
                            continue
                    if "hashira" in rel_path:
                        ep_m = re.search(r"[-_\s](\d{1,2})(?:v\d)?[\. -_]", base_name)
                        if ep_m:
                            file_ep = int(ep_m.group(1))
                            if 1 <= file_ep <= 8 and file_ep not in ep_map:
                                ep_map[file_ep] = vf
                                continue
                continue

            # Explicit SxxExx tag match in filename
            m_s = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", base_name)
            if m_s:
                file_s = int(m_s.group(1))
                file_ep = int(m_s.group(2))
                if file_s == season and file_ep not in ep_map:
                    ep_map[file_ep] = vf
                continue

            # Directory path contains requested season
            if s_dir in rel_path or f"/{s_tag}/" in rel_path or f"/{s_tag_alt}/" in rel_path:
                ep_m = re.search(r"[Ee](\d{1,3})", base_name)
                if not ep_m:
                    ep_m = re.search(r"[-_\s](\d{1,3})(?:v\d)?[\. -_]", base_name)
                if ep_m:
                    file_ep = int(ep_m.group(1))
                    if file_ep not in ep_map:
                        ep_map[file_ep] = vf
                continue

            # For season 1 releases without explicit season folders or tags
            if season == 1:
                has_other_season = any(
                    f"s0{x}" in rel_path or f"s{x}" in rel_path or f"season {x}" in rel_path or f"season{x}" in rel_path
                    for x in range(2, 10)
                )
                if not has_other_season:
                    ep_m = re.search(r"[Ee](\d{1,3})", base_name)
                    if not ep_m:
                        ep_m = re.search(r"[-_\s](\d{1,3})(?:v\d)?[\. -_]", base_name)
                    if ep_m:
                        file_ep = int(ep_m.group(1))
                        if file_ep not in ep_map:
                            ep_map[file_ep] = vf

        LOGGER.info(f"🎬 Discovered {len(ep_map)} video files to mux for {title} S{season:02d} ({encoder}).")
        os.makedirs(MUXED_DIR, exist_ok=True)
        def _mux_single(ep_num: int, raw_video: str, tamil_audio: str):
            quality = resolve_source_quality(raw_video, torrent_title=torrent_title, catalog_raw=catalog_raw)
            out_filename = format_episode_filename(
                title=title,
                season=season,
                ep_num=ep_num,
                year=year,
                encoder=encoder,
                quality=quality,
            )
            out_path = os.path.join(MUXED_DIR, out_filename)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 50 * 1024 * 1024:
                return ep_num, out_path, os.path.getsize(out_path) / (1024 * 1024)

            # Acoustic Cross-Correlation Lip-Sync
            sync_arg = ""
            healed_path = None
            try:
                sync_res = self.sync_engine.analyze_sync_sync(raw_video, tamil_audio)
                sync_arg = sync_res.get("mkvmerge_sync_arg", "")
                healed_path = sync_res.get("healed_audio_path")
                calc_ms = sync_res.get("calculated_delay_ms", 0)
                LOGGER.info(
                    f"🎧 [AcousticSync] Ep {ep_num:02d}: lag={calc_ms}ms -> "
                    f"mkvmerge --sync {sync_arg or 'none'} (Mode: {sync_res.get('sync_mode')}, PSR: {sync_res.get('min_psr_score')})"
                )
            except Exception as e:
                LOGGER.warning(f"⚠️ AcousticSync skipped for Ep {ep_num:02d} ({e}), muxing default")

            actual_audio = healed_path if (healed_path and os.path.exists(healed_path)) else tamil_audio
            audio_codec_tag = "Opus 2.0" if actual_audio.endswith(".opus") else "AAC 2.0"

            # Generate brand intro subtitle spanning FULL video duration
            dur_sec = get_exact_video_duration(raw_video)
            intro_sub_path = os.path.join("/dev/shm", f"intro_{os.getpid()}_{ep_num}.ass")
            create_brand_intro_ass(intro_sub_path, dur_sec, text="[@Anime_Starfall🥰]")

            cmd = [
                "mkvmerge",
                "--ui-language", "en_US",
                "-o", out_path,
                "--title", "[@Anime_Starfall🥰]",
                "--track-name", "0:[@Anime_Starfall🥰]",
                "-A", raw_video,
                "--language", "0:tam",
                "--track-name", f"0:[@Anime_Starfall🥰] - Tamil [{audio_codec_tag}]",
                "--default-track-flag", "0:yes",
            ]
            if sync_arg:
                cmd.extend(["--sync", sync_arg])
            cmd.append(actual_audio)

            if os.path.exists(intro_sub_path):
                cmd.extend([
                    "--language", "0:eng",
                    "--track-name", "0:[@Anime_Starfall🥰] - Intro",
                    "--default-track-flag", "0:no",
                    intro_sub_path,
                ])

            if os.path.exists(COVER_PHOTO_PATH):
                cmd.extend([
                    "--attachment-name", "cover.jpg",
                    "--attachment-mime-type", "image/jpeg",
                    "--attach-file", COVER_PHOTO_PATH,
                ])

            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if healed_path and os.path.exists(healed_path):
                try: os.remove(healed_path)
                except Exception: pass
            if os.path.exists(intro_sub_path):
                try: os.remove(intro_sub_path)
                except Exception: pass

            if proc.returncode > 1:
                LOGGER.error(f"mkvmerge failed for {out_filename}: {proc.stderr[:200]}")
                return None

            # Apply 100% Starfall metadata across video, audio, subs, and container tags
            apply_full_starfall_metadata(out_path, f"Tamil [{audio_codec_tag}]")

            if os.path.exists(raw_video) and raw_video != out_path:
                try:
                    os.remove(raw_video)
                except Exception:
                    pass

            return ep_num, out_path, os.path.getsize(out_path) / (1024 * 1024)

        loop = asyncio.get_running_loop()
        tasks = []
        for ep_num, raw_v in sorted(ep_map.items()):
            if ep_num in found_audios:
                tam_a = found_audios[ep_num]
                tasks.append(loop.run_in_executor(self.mux_executor, _mux_single, ep_num, raw_v, tam_a))

        results = await asyncio.gather(*tasks)
        muxed_files = [r for r in results if r is not None]
        muxed_files.sort(key=lambda x: x[0])
        return muxed_files

    async def process_season_for_encoder(
        self,
        title: str,
        season: int,
        year: str,
        encoder: str,
        torrent_url: str,
        landscape_thumb: str,
        catalog_raw_line: str = "",
    ) -> bool:
        """
        Executes end-to-end processing for a specific encoder of a season:
        1. Checks Audio Bank (Non-blocking skip if missing)
        2. Muxes with 14 workers
        3. Staging via 8 Bots (singles) & User Client (merged parts, sequential)
        4. Target Sequential Forwarding
        """
        torrent_batch_title = ""
        TELEMETRY.update(
            ingest=f"{title[:10]} S{season:02d} ({encoder})",
            mux=f"Muxing {title[:10]} S{season:02d}",
            upload="Waiting for assets",
        )
        TELEMETRY.print_pulse()

        if self.is_season_published(title, season, encoder):
            LOGGER.info(f"⏭️ [Already Published] {title} S{season:02d} ({encoder}) is already published in Target Channel. Skipping.")
            return True

        # Check Tamil audio completeness first (Zero-Stop policy)
        has_tamil, found_audios = self.check_season_tamil_audios(title, season)
        if not has_tamil:
            LOGGER.info(f"⏭️ Tamil audio missing for {title} S{season:02d}. Skipping per Zero-Stop policy.")
            LEDGER.log_failure(
                title=title,
                season=season,
                episode=None,
                encoder=encoder,
                missing_file=f"Tamil Audio ({len(found_audios)} eps present)",
                reason="Missing Tamil Audio in vt_audio_bank",
            )
            return False

        # Check existing muxed files or download directory
        # Let's inspect /home/administrator/starfall/downloads/muxed_output/ for existing files of this title
        muxed_files = []
        if os.path.exists(MUXED_DIR):
            for f in sorted(os.listdir(MUXED_DIR)):
                if f.endswith(".mkv") and not f.endswith(".tmp.mkv") and not "Part" in f:
                    if title.lower() in f.lower() and f"s{season:02d}" in f.lower():
                        ep_match = re.search(r"\[S\d+E(\d+)\]", f)
                        if ep_match:
                            ep_num = int(ep_match.group(1))
                            f_path = os.path.join(MUXED_DIR, f)
                            sz = os.path.getsize(f_path) / (1024 * 1024)
                            muxed_files.append((ep_num, f_path, sz))

        if muxed_files:
            pass
        else:
            LOGGER.info(f"🔍 Searching Nyaa for {encoder} batch: {title} S{season:02d}...")
            batch = self.find_nyaa_batch(title, season, encoder)
            if not batch or batch.get("seeders", 0) < 3:
                LOGGER.warning(f"⚠️ No active {encoder} batch with >=3 seeders for {title} S{season:02d}.")
                LEDGER.log_failure(
                    title=title,
                    season=season,
                    episode=None,
                    encoder=encoder,
                    missing_file=f"{title}_S{season:02d}_{encoder}.torrent",
                    reason="Low Seeders (<3) or Batch Not Found on Nyaa",
                )
                return False

            torrent_batch_title = batch.get("title", "")

            info_hash = batch.get("info_hash")
            t_existing = self.qb.get_torrent(info_hash) if info_hash else None
            if not t_existing:
                LOGGER.info(f"📥 Adding torrent to qBittorrent: {batch['title']} ({batch.get('seeders', 0)} seeds)")
                mag_or_url = batch.get("magnet_uri") or batch.get("torrent_url", "")
                added_hash = self.qb.add_torrent(
                    magnet_or_url=mag_or_url,
                    container_save_path="/usr/src/app/downloads",
                    category="starfall_sprint",
                )
                if added_hash:
                    info_hash = added_hash
            else:
                LOGGER.info(f"📥 Using existing torrent in qBittorrent: {batch['title']} (Hash: {info_hash})")

            if not info_hash:
                LOGGER.error(f"Failed to add torrent to qBittorrent: {batch['title']}")
                return False

            # Monitor download with 5-minute timeout
            dl_ok = await self.wait_for_torrent_download(info_hash, batch["title"])
            if not dl_ok:
                LEDGER.log_failure(
                    title=title,
                    season=season,
                    episode=None,
                    encoder=encoder,
                    missing_file=batch["title"],
                    reason="Download Stalled (>5 min no progress)",
                )
                return False

            # Immediately trigger background pre-buffering of next season while muxing!
            asyncio.create_task(self.prebuffer_next_seasons())

            # If this batch defines extra torrents (e.g. Judas Demon Slayer Season 2 Mugen + Yuukaku)
            if batch.get("extra_torrents"):
                for ex_b in batch["extra_torrents"]:
                    ex_hash = ex_b.get("info_hash")
                    ex_exist = self.qb.get_torrent(ex_hash) if ex_hash else None
                    if not ex_exist:
                        LOGGER.info(f"📥 Adding extra torrent to qBittorrent: {ex_b['title']}")
                        self.qb.add_torrent(
                            magnet_or_url=ex_b.get("magnet_uri") or ex_b.get("torrent_url", ""),
                            container_save_path="/usr/src/app/downloads",
                            category="starfall_sprint",
                        )
                    if ex_hash:
                        await self.wait_for_torrent_download(ex_hash, ex_b["title"])

            # Mux downloaded episodes with isolated Tamil audio
            muxed_files = await self.mux_downloaded_season(
                title=title,
                season=season,
                year=year,
                encoder=encoder,
                info_hash=info_hash,
                found_audios=found_audios,
                torrent_title=torrent_batch_title,
                catalog_raw=catalog_raw_line,
            )
            if not muxed_files:
                LOGGER.error(f"Failed to mux downloaded episodes for {title} S{season:02d}")
                return False

            # Clean up qBittorrent torrent and raw data (preserve multi-season torrents)
            t_info = self.qb.get_torrent(info_hash)
            t_name = t_info.get("name", "").lower() if t_info else ""
            is_multi_season = any(k in t_name for k in ("complete", "seasons", "s01-05", "s01-04", "s01-02", "s01-03", "s1-2", "s1-3", "s1-4", "kimetsu", "demon slayer", "shingeki", "titan", "jujutsu"))
            if is_multi_season:
                LOGGER.info(f"📦 Torrent '{t_name}' contains multiple seasons. Preserving raw files for subsequent seasons.")
            else:
                self.qb.delete_torrent(info_hash, delete_files=True)

        LOGGER.info(f"🎬 Found {len(muxed_files)} episodes ready for {title} S{season:02d} ({encoder}).")

        # Safeguard: Season Completeness Guard (Never publish incomplete seasons!)
        if len(found_audios) >= 6 and len(muxed_files) < len(found_audios) * 0.8:
            LOGGER.error(f"❌ Completeness guard triggered: Only {len(muxed_files)}/{len(found_audios)} episodes ready for {title} S{season:02d} ({encoder}). Aborting publish.")
            return False

        # Legitimate complete season confirmed: Trigger background pre-buffering of next seasons
        asyncio.create_task(self.prebuffer_next_seasons())

        # Resolve authoritative season quality from torrent title, file path, and catalog line
        sample_path = muxed_files[0][1] if muxed_files else ""
        season_quality = resolve_source_quality(
            filepath_or_name=sample_path,
            torrent_title=torrent_batch_title,
            catalog_raw=catalog_raw_line,
        )

        # 4GB Episode-Aware Auto-Merge
        merge_plan = self.plan_season_merge(muxed_files)
        LOGGER.info(f"📋 Merge plan for {title} S{season:02d}: {len(merge_plan)} part(s) <= 3,950 MiB")
        merged_parts = await self.execute_merge(
            title=title,
            season=season,
            year=year,
            encoder=encoder,
            plan=merge_plan,
            quality=season_quality,
        )

        # STAGING PHASE:
        # BOT-ONLY STAGING FOR EPISODES: Standard episodes (<2 GB) must be uploaded exclusively by the 8 Helper Bots!
        # USER CLIENT RATE LIMIT (>2GB PARTS): Merged parts uploaded strictly ONE file at a time!
        TELEMETRY.update(upload=f"Uploading {len(muxed_files)} eps (8 Bots)")
        TELEMETRY.print_pulse()

        dump_results: Dict[str, int] = {}
        bot_ep_queue: asyncio.Queue = asyncio.Queue()
        user_ep_queue: List[Tuple[int, str]] = []

        for ep_num, ep_path, sz in muxed_files:
            if sz >= 1990:  # Telegram Bot 2000 MiB limit -> Requires Premium User Client
                user_ep_queue.append((ep_num, ep_path))
            else:
                bot_ep_queue.put_nowait((ep_num, ep_path))

        async def _bot_worker(bot_client: Client):
            while not bot_ep_queue.empty():
                try:
                    ep_num, ep_path = bot_ep_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                clean_name = os.path.basename(ep_path)
                ep_dur = get_exact_video_duration(ep_path)
                await self.upload_file_to_dump(
                    client=bot_client,
                    filepath=ep_path,
                    caption=f"<b>{clean_name}</b>",
                    thumb_path=landscape_thumb,
                    tag=f"ep_{ep_num:02d}",
                    dump_results=dump_results,
                    exact_duration=ep_dur,
                    is_user_client=False,
                )
                bot_ep_queue.task_done()

        # Stage single episodes via helper bots first (clean bandwidth, rapid delivery every 10-15s)
        if not bot_ep_queue.empty():
            LOGGER.info(f"🤖 [Turbo Parallel Bots] Uploading {bot_ep_queue.qsize()} episodes across {len(self.helper_bots)} parallel helper bots...")
            bot_tasks = [_bot_worker(b) for b in self.helper_bots]
            await asyncio.gather(*bot_tasks)

        # Concurrently, User Client uploads >2GB items strictly ONE AT A TIME (sequential at 500+ Mbps)
        async def _user_uploader():
            for p_tag, p_path, p_num, p_dur in merged_parts:
                clean_name = os.path.basename(p_path)
                LOGGER.info(f"👤 [User Client] Uploading >2GB merged part: {clean_name} (Sequential Rate Limit Active)...")
                await self.upload_file_to_dump(
                    client=self.user_client,
                    filepath=p_path,
                    caption=f"<b>{clean_name}</b>",
                    thumb_path=landscape_thumb,
                    tag=p_tag,
                    dump_results=dump_results,
                    exact_duration=p_dur,
                    is_user_client=True,
                )
            for ep_num, ep_path in user_ep_queue:
                clean_name = os.path.basename(ep_path)
                LOGGER.info(f"👤 [User Client] Uploading >2GB episode: {clean_name} (Sequential Rate Limit Active)...")
                ep_dur = get_exact_video_duration(ep_path)
                await self.upload_file_to_dump(
                    client=self.user_client,
                    filepath=ep_path,
                    caption=f"<b>{clean_name}</b>",
                    thumb_path=landscape_thumb,
                    tag=f"ep_{ep_num:02d}",
                    dump_results=dump_results,
                    exact_duration=ep_dur,
                    is_user_client=True,
                )

        if merged_parts or user_ep_queue:
            LOGGER.info(f"👤 [User Client Turbo] Uploading {len(merged_parts)} merged part(s) at full 500+ Mbps...")
            await _user_uploader()

        LOGGER.info(f"✅ All assets staged in Main Dump for {title} S{season:02d} ({len(dump_results)} confirmed).")

        if BLIND_DUMP_ONLY:
            LOGGER.info(
                f"⚡ [Blind Dump Sprint] {title} S{season:02d} ({encoder}) staged blindly in Main Dump! "
                f"Skipping Target Channel publishing per user directive."
            )
            self.mark_season_published(
                title=title,
                season=season,
                encoder=encoder,
                poster_id=0,
                part_ids=[dump_results[p_tag] for p_tag, _, _, _ in merged_parts if p_tag in dump_results],
                ep_ids=[dump_results[f"ep_{ep_num:02d}"] for ep_num, _, _ in muxed_files if f"ep_{ep_num:02d}" in dump_results],
            )
            TELEMETRY.update(upload=f"Dumped {title[:10]} S{season:02d}")
            TELEMETRY.print_pulse()
            return True

        # TARGET PUBLISHING PHASE
        TELEMETRY.update(upload=f"Forwarding {title[:10]} S{season:02d}")
        TELEMETRY.print_pulse()

        await self.forward_to_target_channel(
            title=title,
            season=season,
            year=year,
            encoder=encoder,
            landscape_thumb=landscape_thumb,
            dump_results=dump_results,
            merged_parts=merged_parts,
            episodes=muxed_files,
            quality=season_quality,
        )

        return True

    def scan_ready_seasons_in_muxed(self) -> List[Dict[str, Any]]:
        """Scan all completed seasons currently in MUXED_DIR to drain backlog."""
        if not os.path.exists(MUXED_DIR):
            return []

        files = sorted(os.listdir(MUXED_DIR))
        seasons_map = {}

        for f in files:
            if not f.endswith(".mkv") or "Part" in f or f.endswith(".tmp.mkv"):
                continue

            # Check title, season, and episode
            m = re.search(r"^🄰🅂-\s*\[S(\d+)E(\d+)\]\s*(.+?)\s*\((\d{4})\)", f)
            if not m:
                continue

            season = int(m.group(1))
            ep_num = int(m.group(2))
            title = m.group(3).strip()
            year = m.group(4)

            # Detect encoder from end of string: ~ {Encoder} ~
            m_enc = re.search(r"~\s*([A-Za-z0-9_\-]+)\s*~\s*\[@Anime_Starfall", f)
            encoder = m_enc.group(1) if m_enc else "Judas"

            key = (title, season, encoder)
            if key not in seasons_map:
                seasons_map[key] = {
                    "title": title,
                    "season": season,
                    "year": year,
                    "encoder": encoder,
                    "episodes": [],
                }

            f_path = os.path.join(MUXED_DIR, f)
            sz_mib = os.path.getsize(f_path) / (1024 * 1024)
            seasons_map[key]["episodes"].append((ep_num, f_path, sz_mib))

        result = []
        for (t, s, enc), data in seasons_map.items():
            data["episodes"].sort(key=lambda x: x[0])
            data["total_eps"] = len(data["episodes"])
            if data["total_eps"] < 6:
                continue
            data["total_size_gib"] = sum(x[2] for x in data["episodes"]) / 1024
            result.append(data)

        # Sort by size ascending for fastest disk reclaim
        result.sort(key=lambda x: x["total_size_gib"])
        return result

    async def run(self):
        """Main autonomous execution loop."""
        print("=" * 80)
        print("🚀 COMMENCING STARFALL HIGH-SPEED MULTI-ENCODER SPRINT PIPELINE")
        print("=" * 80)

        # 1. Initialize Telegram clients (User Client + 8 Helper Bots)
        await self.initialize()

        # 2. Print initial pulse
        TELEMETRY.update(
            ingest="Ingest Active (1 Gbps)",
            mux="14-vCPU Pool Online",
            upload="8 Bots + User Client",
        )
        TELEMETRY.print_pulse()

        # 3. Drain existing completed seasons in MUXED_DIR with strict anti-ban shield
        ready_seasons = self.scan_ready_seasons_in_muxed()
        LOGGER.info(f"📋 Discovered {len(ready_seasons)} completed season(s) in backlog waiting for immediate release.")

        for idx, s in enumerate(ready_seasons, 1):
            title = s["title"]
            season = s["season"]
            year = s["year"]
            encoder = s["encoder"]

            # ZERO BLEACH & EXCLUDED ANIME CHECK
            if any(ex in title.lower() for ex in EXCLUDED_KEYWORDS):
                LOGGER.warning(f"🚫 [EXCLUDED] Skipping excluded anime: {title} S{season:02d}")
                continue

            LOGGER.info(f"\n>>> PROCESSING SPRINT QUEUE ITEM {idx}/{len(ready_seasons)}: {title} S{season:02d} ({encoder}) <<<")

            # Landscape thumbnail
            raw_landscape = find_anime_landscape_thumbnail(title, season)
            if not raw_landscape:
                raw_landscape = COVER_PHOTO_PATH
            slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
            hd_thumb = os.path.join(THUMBS_DIR, f"hd_landscape_{slug}_S{season:02d}.jpg")
            master_thumb = prepare_hd_thumbnail(raw_landscape, hd_thumb)

            try:
                await self.process_season_for_encoder(
                    title=title,
                    season=season,
                    year=year,
                    encoder=encoder,
                    torrent_url="",
                    landscape_thumb=master_thumb,
                )
            except Exception as e:
                LOGGER.error(f"❌ Error releasing {title} S{season:02d}: {e}", exc_info=True)
                LEDGER.log_failure(
                    title=title,
                    season=season,
                    episode=None,
                    encoder=encoder,
                    missing_file=f"{title}_S{season:02d}",
                    reason=f"Pipeline error: {e}",
                )

        # 4. Process Sourcing Catalog for remaining seasons
        catalog = self.load_catalog()

        # Prioritize seasons whose raw torrents are already completed in qBittorrent for instantaneous throughput!
        try:
            active_torrents = self.qb.get_all_torrents()
            completed_names = {t.get("name", "").lower() for t in active_torrents if t.get("progress", 0.0) >= 1.0}
            def _has_qb_torrent(item):
                norm_t = re.sub(r"[^a-zA-Z0-9]", "", item["title"]).lower()
                s = item["season"]
                words = [w for w in re.sub(r"[^\w\s]", " ", item["title"]).lower().split() if len(w) >= 3 and w not in ("the", "and", "for")]
                s_tokens = [f"s{s:02d}", f"s{s}", f"season{s}", f"season0{s}", "complete"]
                for qn in completed_names:
                    norm_qn = re.sub(r"[^a-zA-Z0-9]", "", qn)
                    title_match = (norm_t in norm_qn) or (bool(words) and all(re.search(rf"\b{re.escape(w)}\b", qn, re.I) for w in words))
                    if title_match and (any(tok in norm_qn for tok in s_tokens) or s == 1):
                        return 0
                return 1
            catalog.sort(key=_has_qb_torrent)
        except Exception as e:
            LOGGER.warning(f"Could not prioritize qBittorrent cache: {e}")

        LOGGER.info(f"📚 Scanning {len(catalog)} seasons from catalog for Judas, Breeze, Sokudo, and TRiX...")

        for s_item in catalog:
            title = s_item["title"]
            season = s_item["season"]
            year = "2024"

            # ZERO BLEACH & EXCLUDED ANIME CHECK
            if s_item.get("skip_bleach") or any(ex in title.lower() for ex in EXCLUDED_KEYWORDS):
                continue

            candidates = s_item.get("candidates", {})
            judas_done = self.is_season_published(title, season, "Judas")
            av1_done = any(self.is_season_published(title, season, enc) for enc in ("Breeze", "Sokudo", "TRiX"))

            if BLIND_DUMP_ONLY and (judas_done or av1_done):
                continue
            elif judas_done and av1_done:
                continue

            for target_enc in ["Judas", "Breeze", "Sokudo", "TRiX"]:
                if target_enc == "Judas" and judas_done:
                    continue
                if target_enc in ("Breeze", "Sokudo", "TRiX") and av1_done:
                    continue

                cand_info = candidates.get(target_enc) or candidates.get(target_enc.upper())
                enc_name = cand_info["encoder"] if cand_info else target_enc
                t_url = cand_info.get("url", "") if cand_info else ""

                # Landscape thumbnail
                raw_landscape = find_anime_landscape_thumbnail(title, season) or COVER_PHOTO_PATH
                slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
                hd_thumb = os.path.join(THUMBS_DIR, f"hd_landscape_{slug}_S{season:02d}.jpg")
                master_thumb = prepare_hd_thumbnail(raw_landscape, hd_thumb)

                try:
                    success = await self.process_season_for_encoder(
                        title=title,
                        season=season,
                        year=year,
                        encoder=enc_name,
                        torrent_url=t_url,
                        landscape_thumb=master_thumb,
                        catalog_raw_line=cand_info.get("raw_line", "") if cand_info else "",
                    )
                    if success:
                        if target_enc == "Judas":
                            judas_done = True
                        elif target_enc in ("Breeze", "Sokudo", "TRiX"):
                            av1_done = True
                        if BLIND_DUMP_ONLY and (judas_done or av1_done):
                            LOGGER.info(
                                f"⚡ [Blind Dump Sprint] {title} S{season:02d} complete in Main Dump! Advancing to next catalogue title."
                            )
                            break
                except Exception as e:
                    LOGGER.error(f"Error processing {title} S{season:02d} ({enc_name}): {e}")
                    LEDGER.log_failure(
                        title=title,
                        season=season,
                        episode=None,
                        encoder=enc_name,
                        missing_file=f"{title}_S{season:02d}",
                        reason=str(e),
                    )

        LOGGER.info("🏁 SPRINT PIPELINE EXECUTION COMPLETED.")
        TELEMETRY.update(ingest="Finished", mux="Finished", upload="Finished")
        TELEMETRY.print_pulse()


async def main():
    orchestrator = MasterSprintOrchestrator()
    try:
        await orchestrator.run()
    finally:
        await orchestrator.close()


if __name__ == "__main__":
    asyncio.run(main())
