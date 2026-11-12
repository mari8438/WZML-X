#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Starfall Document-Type Master Archival Pipeline Orchestrator.
Reads /home/administrator/starfall/master_anime_archival_catalogue_600.csv and executes:
1. Source Ingestion via qBittorrent (NO audio replacing, NO audio muxing, NO video merging).
2. Lossless Starfall Intro Subtitle Injection (data/intro_starfall.ass as 1st subtitle track, default=1)
   + Container Art (data/intro_preroll.jpg as cover.jpg).
3. Upgraded Template 11 Hero Poster Card (Dynamic 2-line Title Auto-Fit, Live Probed Video/Audio Badges,
   Smart Aspect-Ratio Cover Fit, 100% Completion Progress Bar).
4. Document-Type Telegram Dispatch (send_document with data/document_thumb.jpg) to Main Dump (-1003887117402),
   followed by sequential forwarding to Target Channel (-1004415413346) with 3.5s anti-flood delay.
5. Telegram Premium Custom Emoji Poster Caption + InlineKeyboardMarkup [ 🎬 Click Here ] button
   jumping directly to Episode 01 in Target Channel.
6. Rolling Disk Purge maintaining >= 150 GB free NVMe space.
"""

import asyncio
import concurrent.futures
import csv
import glob
import html
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, "/home/administrator/starfall")

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.helper.poster_engine.template_eleven import _template_eleven
from bot.helper.telegram_helper.tg_ui_formatter import (
    PREMIUM_BOLT,
    PREMIUM_DOC,
    PREMIUM_MOVIE,
    PREMIUM_SCREEN,
    PREMIUM_STAR,
    PREMIUM_STATS,
)
from bot.modules.starfall_pipeline.anilist_client import AniListClient
from bot.modules.starfall_pipeline.archival_injector import inject_starfall_archival_branding
from bot.modules.starfall_pipeline.archival_renamer import (
    format_archival_filename,
    parse_episode_or_special_tag,
    probe_media_streams,
)
from bot.modules.starfall_pipeline.config import (
    DATA_DIR,
    HELPER_TOKENS,
    TELEGRAM_API,
    TELEGRAM_HASH,
    USER_SESSION_STRING,
)
from bot.modules.starfall_pipeline.nyaa_matcher import TRACKERS
from bot.modules.starfall_pipeline.qbittorrent_bridge import QBittorrentBridge

# Unlock WZGram / Pyrogram internal AdaptiveBucket upload rate & media pool for maximum throughput
os.environ.setdefault("WZGRAM_UPLOAD_RATE_BOT", "300")
os.environ.setdefault("WZGRAM_UPLOAD_POOL_BOT", "16")
os.environ.setdefault("WZGRAM_UPLOAD_RATE_PREMIUM", "500")
os.environ.setdefault("WZGRAM_UPLOAD_POOL_PREMIUM", "16")
os.environ.setdefault("WZGRAM_UPLOAD_RATE_USER", "300")
os.environ.setdefault("WZGRAM_UPLOAD_POOL_USER", "16")

# Logging Setup
os.makedirs("/home/administrator/starfall/logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/home/administrator/starfall/logs/archival_pipeline.log", mode="a", encoding="utf-8"),
    ],
)
LOGGER = logging.getLogger("MasterArchival")

for _log in ("pyrogram", "pyrogram.session", "pyrogram.connection", "pyrogram.dispatcher", "urllib3"):
    logging.getLogger(_log).setLevel(logging.ERROR)

# Operational Constants
CATALOG_CSV_PATH = "/home/administrator/starfall/master_anime_archival_catalogue_600.csv"
ARCHIVAL_STATE_PATH = "/home/administrator/starfall/data/archival_published_catalogue.json"
STAGED_WAVE_PATH = "/home/administrator/starfall/data/archival_staged_wave.json"
DOWNLOAD_DIR = "/home/administrator/starfall/downloads"
STAGING_DIR = "/home/administrator/starfall/downloads/archival_staging"
THUMBS_DIR = "/home/administrator/starfall/thumbnails"
DOCUMENT_THUMB_PATH = "/home/administrator/starfall/data/document_thumb.jpg"
DEFAULT_BANNER_PATH = "/home/administrator/starfall/data/intro_preroll.jpg"
INTRO_ASS_PATH = "/home/administrator/starfall/data/intro_starfall.ass"

DUMP_CHANNEL_ID = -1003887117402
TARGET_CHANNEL_ID = -1004415413346
TARGET_CLEAN_ID = "4415413346"
DUMP_CLEAN_ID = "3887117402"

FORWARD_DELAY_SEC = 3.0
MIN_FREE_DISK_GIB = 50.0
MAX_CONCURRENT_TORRENTS = 14
BLIND_DUMP_WAVE_SIZE = 10
UPLOAD_CONCURRENCY = 6

PREFERRED_ENCODERS = [
    "judas", "ember", "breeze", "sokudo", "trix", "yurasuka", "animerg",
    "subsplease", "erai-raws", "psa", "cleo", "dkb", "asw", "mtbb",
    "minimtbb", "lys1th3a", "itachiuchiha", "anime time", "animetime", "tenrai",
]


def get_free_disk_gib(path: str = "/home/administrator/starfall") -> float:
    """Return available NVMe disk space in GiB."""
    st = shutil.disk_usage(path)
    return st.free / (1024 ** 3)


def parse_size_to_gib(size_str: str) -> float:
    """Convert Nyaa size string (e.g. '4.5 GiB', '850 MiB') to GiB float."""
    s = str(size_str).strip().upper()
    m = re.search(r"([\d.]+)\s*(TIB|GIB|MIB|KIB|TB|GB|MB)", s)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit in ("TIB", "TB"):
        return val * 1024.0
    if unit in ("GIB", "GB"):
        return val
    if unit in ("MIB", "MB"):
        return val / 1024.0
    return val / (1024.0 * 1024.0)


def build_magnet(info_hash: str, name: str) -> str:
    """Build a magnet URI with standard Nyaa trackers."""
    dn = urllib.parse.quote(name)
    tr_params = "&".join(f"tr={urllib.parse.quote(tr)}" for tr in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash.lower()}&dn={dn}&{tr_params}"


class MasterArchivalPipeline:
    def __init__(self):
        self.qb = QBittorrentBridge()
        self.anilist = AniListClient()
        self.user_client: Optional[Client] = None
        self.helper_bots: List[Client] = []
        self.published_state: Dict[str, Any] = {}
        self.staged_wave: List[Dict[str, Any]] = []
        self.inject_executor = concurrent.futures.ThreadPoolExecutor(max_workers=12)
        self._upload_rr_idx = 0
        self._upload_sem: Optional[asyncio.Semaphore] = None

        os.makedirs(STAGING_DIR, exist_ok=True)
        os.makedirs(THUMBS_DIR, exist_ok=True)
        self.load_state()

    def load_state(self):
        if os.path.exists(ARCHIVAL_STATE_PATH):
            try:
                with open(ARCHIVAL_STATE_PATH, "r", encoding="utf-8") as f:
                    self.published_state = json.load(f)
            except Exception as e:
                LOGGER.warning(f"Failed to load state {ARCHIVAL_STATE_PATH}: {e}")
                self.published_state = {}
        if os.path.exists(STAGED_WAVE_PATH):
            try:
                with open(STAGED_WAVE_PATH, "r", encoding="utf-8") as f:
                    loaded_wave = json.load(f)
                    if isinstance(loaded_wave, list):
                        self.staged_wave = []
                        for item in loaded_wave:
                            if str(item.get("gid")) in self.published_state:
                                continue
                            if "season_num" not in item:
                                # Infer season_num from first staged tag (e.g. "S02E01" -> 2)
                                msgs = item.get("staged_dump_msgs") or []
                                first_tag = msgs[0][0] if msgs else "S01E01"
                                sm = re.search(r"S0*(\d+)", str(first_tag))
                                item["season_num"] = int(sm.group(1)) if sm else 1
                            self.staged_wave.append(item)
            except Exception as e:
                LOGGER.warning(f"Failed to load staged wave {STAGED_WAVE_PATH}: {e}")
                self.staged_wave = []

    def save_state(self):
        try:
            if os.path.exists(ARCHIVAL_STATE_PATH):
                try:
                    with open(ARCHIVAL_STATE_PATH, "r", encoding="utf-8") as rf:
                        disk_state = json.load(rf)
                    for k, v in disk_state.items():
                        if k not in self.published_state:
                            self.published_state[k] = v
                        elif v.get("poster_msg_id") and not self.published_state[k].get("poster_msg_id"):
                            self.published_state[k]["poster_msg_id"] = v["poster_msg_id"]
                except Exception:
                    pass
            with open(ARCHIVAL_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.published_state, f, indent=2)
            with open(STAGED_WAVE_PATH, "w", encoding="utf-8") as wf:
                json.dump(self.staged_wave, wf, indent=2)
        except Exception as e:
            LOGGER.error(f"Failed to save state: {e}")

    async def initialize_clients(self):
        LOGGER.info("🚀 Initializing Telegram Premium User Client & Helper Bot Pool (Turbo Mode)...")
        self._upload_sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
        self.user_client = Client(
            "archival_user_hsin",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            session_string=USER_SESSION_STRING,
            in_memory=True,
            ipv6=False,
            workers=24,
            max_concurrent_transmissions=20,
        )
        await self.user_client.start()
        u_me = await self.user_client.get_me()
        LOGGER.info(f"✅ User Client online: @{u_me.username} (ID: {u_me.id}, Premium={u_me.is_premium})")

        sessions_dir = os.path.join(DATA_DIR, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        raw_tokens = [t.strip() for t in HELPER_TOKENS.split() if t.strip()]

        self.helper_bots = []
        for idx, tok in enumerate(raw_tokens, 1):
            try:
                bot = Client(
                    f"archival_helper_{idx}",
                    api_id=TELEGRAM_API,
                    api_hash=TELEGRAM_HASH,
                    bot_token=tok,
                    in_memory=True,
                    ipv6=False,
                    workers=24,
                    max_concurrent_transmissions=20,
                )
                await bot.start()
                b_me = await bot.get_me()
                LOGGER.info(f"✅ Helper Bot #{idx} online: @{b_me.username}")
                self.helper_bots.append(bot)
            except Exception as e:
                LOGGER.warning(f"Helper Bot #{idx} failed to start: {e}")

    async def close_clients(self):
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
        self.inject_executor.shutdown(wait=False)

    def load_catalogue(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        with open(CATALOG_CSV_PATH, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    @staticmethod
    def parse_required_seasons(chronology: str) -> List[int]:
        """
        Inspects Complete_Chronology to determine all seasons in chronological watch order [1, 2, ...].
        """
        c = (chronology or "").lower()
        max_s = 1
        for m in re.finditer(r"\b(?:seasons?|s)\s*0*1\s*(?:[–\-~&]|to|and)\s*(?:seasons?|s)?\s*0*(\d{1,2})\b", c):
            val = int(m.group(1))
            if 1 <= val <= 8:
                max_s = max(max_s, val)
        for m in re.finditer(r"\b(?:season|s)\s*0*(\d{1,2})\b", c):
            val = int(m.group(1))
            if 1 <= val <= 8:
                max_s = max(max_s, val)
        if any(
            k in c
            for k in (
                "second stage", "second barrage", "perfect order", "sotsu",
                "succession", "r2", "steins;gate 0", "battle for planet nine", "2nd season",
            )
        ):
            max_s = max(max_s, 2)
        return list(range(1, max_s + 1))

    @staticmethod
    def detect_torrent_seasons(
        t_title: str,
        req_seasons: List[int],
        sz_gib: float,
        expected_eps: int,
    ) -> List[int]:
        """
        Determines which seasons [1], [2], [1, 2], [1, 2, 3] a Nyaa torrent covers.
        """
        tl = t_title.lower()
        m_range = re.search(
            r"\b(?:seasons?|s|r)\s*0*1\s*(?:[–\-~+&]|to|and)\s*(?:seasons?|s|r)?\s*0*([2-8])\b",
            tl,
        )
        if m_range:
            end_s = int(m_range.group(1))
            return list(range(1, end_s + 1))
        if re.search(r"\bs0?1\b.*\bs0?2\b|\bseason\s*0?1\b.*\bseason\s*0?2\b|\br1\b.*\br2\b", tl):
            if re.search(r"\bs0?3\b|\bseason\s*0?3\b", tl):
                return [1, 2, 3]
            return [1, 2]
        for s_idx in range(8, 1, -1):
            if re.search(rf"\b(?:season\s*0*{s_idx}|s0*{s_idx}|{s_idx}(?:nd|rd|th)\s*season)\b", tl):
                return [s_idx]
        if any(
            k in tl
            for k in (
                "second stage", "second barrage", "perfect order", "sotsu",
                "succession", "steins;gate 0", "battle for planet nine",
            )
        ) and not any(k in tl for k in ("season 1", "s01", "s1", "first stage", "gou")):
            return [2]
        if re.search(r"\b(?:season\s*0*1|s0*1|1st\s*season|first\s*stage|r1)\b", tl):
            return [1]
        if len(req_seasons) > 1 and sz_gib >= max(3.5, expected_eps * 0.14):
            return list(req_seasons)
        return [1]

    def resolve_nyaa_chronological_batches(self, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Resolves ALL seasons of a franchise in strict Chronological Watch Order (Season 1 -> Season 2 -> Season 3...).
        Returns a list of torrent batch dicts, each annotated with `seasons` (e.g. [1], [2], or [1, 2])
        and `target_season` (int).
        """
        url = row.get("Verified_Nyaa_URL", "").strip()
        franchise = row.get("Franchise_Title", "").strip()
        chronology = row.get("Complete_Chronology", "Complete Series").strip()
        req_seasons = self.parse_required_seasons(chronology)
        pref_enc_raw = row.get("Encoder_And_Codec", "").split("(")[0].strip().lower()
        expected_eps = int(re.sub(r"\D", "", str(row.get("Episodes", "12"))) or 12)

        # Case 1: Direct /view/<id> URL (for single-season franchises)
        m_view = re.search(r"nyaa\.si/view/(\d+)", url)
        if m_view and len(req_seasons) == 1:
            tid = m_view.group(1)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                page_html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
                title_m = re.search(r"<h3 class=\"panel-title\">\s*([\s\S]*?)\s*</h3>", page_html)
                t_title = html.unescape(title_m.group(1).strip()) if title_m else f"{franchise} [1080p]"
                mag_m = re.search(r'href="(magnet:\?[^"]+)"', page_html)
                magnet_uri = html.unescape(mag_m.group(1)) if mag_m else ""
                ih_m = re.search(r"urn:btih:([a-fA-F0-9]{40})", magnet_uri)
                info_hash = ih_m.group(1).lower() if ih_m else ""
                return [{
                    "title": t_title,
                    "torrent_url": f"https://nyaa.si/download/{tid}.torrent",
                    "magnet_uri": magnet_uri,
                    "info_hash": info_hash,
                    "seeders": int(row.get("Active_Seeders", 10) or 10),
                    "size_gib": parse_size_to_gib(row.get("Batch_Size_GiB", "5.0 GiB")),
                    "seasons": [1],
                    "target_season": 1,
                    "required_seasons": req_seasons,
                }]
            except Exception as e:
                LOGGER.warning(f"Direct view fetch failed for {url}: {e}")

        # Case 2: Search URL -> Query RSS feeds
        base_title = re.sub(r"\(.*?\)", "", franchise).strip()
        if "&" in base_title:
            base_title = base_title.split("&")[0].strip()
        alt_match = re.search(r"\((.*?)\)", franchise)
        alt_title = alt_match.group(1).strip() if alt_match else ""

        rss_urls = []
        if base_title and pref_enc_raw:
            q1 = urllib.parse.quote_plus(f"{base_title} {pref_enc_raw} 1080p")
            rss_urls.append(f"https://nyaa.si/?f=0&c=1_2&q={q1}&s=seeders&o=desc&page=rss")
        if alt_title and len(alt_title) >= 4 and pref_enc_raw:
            q1_alt = urllib.parse.quote_plus(f"{alt_title} {pref_enc_raw} 1080p")
            rss_urls.append(f"https://nyaa.si/?f=0&c=1_2&q={q1_alt}&s=seeders&o=desc&page=rss")
        if "nyaa.si/?" in url:
            rss_urls.append(f"{url}&page=rss")
        if base_title:
            q2 = urllib.parse.quote_plus(f"{base_title} 1080p")
            u2 = f"https://nyaa.si/?f=0&c=1_2&q={q2}&s=seeders&o=desc&page=rss"
            if u2 not in rss_urls:
                rss_urls.append(u2)
        if alt_title and len(alt_title) >= 4:
            q3 = urllib.parse.quote_plus(f"{alt_title} 1080p")
            u3 = f"https://nyaa.si/?f=0&c=1_2&q={q3}&s=seeders&o=desc&page=rss"
            if u3 not in rss_urls:
                rss_urls.append(u3)

        title_keywords = []
        for kw_src in (base_title, alt_title):
            words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", kw_src) if len(w) >= 3]
            if words:
                title_keywords.append(words)

        ns = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
        candidates: List[Dict[str, Any]] = []
        seen_hashes = set()

        def _fetch_rss_candidates(u_list: List[str]):
            for rss_u in u_list:
                try:
                    req = urllib.request.Request(rss_u, headers={"User-Agent": "Mozilla/5.0"})
                    xml_bytes = urllib.request.urlopen(req, timeout=15).read()
                    root = ET.fromstring(xml_bytes)
                    for item in root.findall("./channel/item"):
                        t_title = (item.findtext("title") or "").strip()
                        t_link = (item.findtext("link") or "").strip()
                        ih = (item.findtext("nyaa:infoHash", default="", namespaces=ns) or "").strip().lower()
                        seeds = int(item.findtext("nyaa:seeders", default="0", namespaces=ns) or 0)
                        sz_str = (item.findtext("nyaa:size", default="0 GiB", namespaces=ns) or "0 GiB").strip()
                        sz_gib = parse_size_to_gib(sz_str)

                        if not ih or ih in seen_hashes or seeds < 3:
                            continue
                        seen_hashes.add(ih)

                        if sz_gib > 45.0 or sz_gib < 0.4:
                            continue

                        t_low = t_title.lower()
                        core_t = re.sub(r"^\[[^\]]+\]\s*", "", t_low)
                        core_t = re.split(
                            r"\s*(?:\[|\((?:19|20)\d\d|\(season|\(complete|\(bd|\(batch|\bs0?\d\b|-\s*(?:complete|season|batch|s0?\d|\d{2}))",
                            core_t,
                        )[0].strip()
                        core_words = [
                            w for w in re.findall(r"[a-zA-Z0-9]+", core_t)
                            if w not in ("tv", "bd", "remastered", "uncensored", "uncut", "complete", "series", "batch", "the", "animation")
                        ]

                        matches_kw = False
                        for kw_group in title_keywords:
                            if len(kw_group) == 1:
                                single_w = kw_group[0]
                                if core_words and core_words[0] == single_w and len(core_words) <= 2:
                                    matches_kw = True
                                    break
                            else:
                                match_cnt = sum(1 for w in kw_group if re.search(rf"\b{re.escape(w)}\b", t_low))
                                if match_cnt >= max(2, int(len(kw_group) * 0.7)):
                                    matches_kw = True
                                    break
                        if not matches_kw:
                            continue

                        if expected_eps > 3:
                            if sz_gib < 1.0:
                                continue
                            if re.search(r"\bs\d{1,2}e\d{1,4}\b", t_low) and not re.search(r"e\d{1,3}\s*[-~]\s*e?\d{1,3}", t_low):
                                continue
                            if re.search(r"\s-\s*\d{2,4}(?:v\d)?\s*(?:\[|\()", t_low) and not any(
                                b in t_low for b in ("batch", "complete", "season", "s01", "s1", "s02", "s2", "s03", "s3", "01-")
                            ):
                                continue

                        cov_seasons = self.detect_torrent_seasons(t_title, req_seasons, sz_gib, expected_eps)
                        eps_in_pack = expected_eps if len(cov_seasons) >= len(req_seasons) else max(12, int(expected_eps / max(1, len(req_seasons))))
                        gb_per_ep = sz_gib / max(1, eps_in_pack)

                        score = float(min(seeds, 250)) * 2.5
                        if seeds < 5 and sz_gib > 6.0:
                            score -= 200.0  # Penalize heavy packs with very few seeds to prevent slow crawls
                        if pref_enc_raw and pref_enc_raw in t_low:
                            score += 250.0
                        for idx_e, enc_name in enumerate(PREFERRED_ENCODERS):
                            if enc_name in t_low:
                                score += max(20.0, 160.0 - idx_e * 8.0)
                                break
                        if any(b in t_low for b in ("batch", "complete", "season", "s01", "s1", "dual audio", "dual-audio")):
                            score += 120.0
                        if "1080p" in t_low:
                            score += 80.0
                        # Strongly prefer compact HEVC/AV1 archival encodes (150-500 MiB/ep) for 3-day sprint speed
                        if 0.12 <= gb_per_ep <= 0.50:
                            score += 240.0
                        elif 0.50 < gb_per_ep <= 0.75:
                            score += 70.0
                        elif gb_per_ep > 0.85:
                            score -= 320.0
                        if len(cov_seasons) > 1 and set(req_seasons).issubset(set(cov_seasons)):
                            score += 220.0

                        candidates.append({
                            "title": t_title,
                            "torrent_url": t_link,
                            "magnet_uri": build_magnet(ih, t_title),
                            "info_hash": ih,
                            "seeders": seeds,
                            "size_gib": sz_gib,
                            "score": score,
                            "seasons": cov_seasons,
                            "target_season": cov_seasons[0] if cov_seasons else 1,
                            "required_seasons": req_seasons,
                        })
                except Exception as e:
                    LOGGER.debug(f"RSS search warning for {rss_u}: {e}")

        _fetch_rss_candidates(rss_urls[:2])
        # If any required season is still missing, query remaining generic feeds
        covered_so_far = {s for c in candidates for s in c["seasons"]}
        if not set(req_seasons).issubset(covered_so_far):
            _fetch_rss_candidates(rss_urls[2:])

        if not candidates:
            return []

        # 1. Check if a single multi-season pack covers all required seasons
        multi_packs = [c for c in candidates if set(req_seasons).issubset(set(c["seasons"]))]
        if multi_packs:
            multi_packs.sort(key=lambda x: x["score"], reverse=True)
            best_multi = multi_packs[0]
            if len(req_seasons) == 1 or best_multi["score"] >= 520.0:
                return [best_multi]

        # 2. Otherwise pick the best batch for EACH required season in chronological order (Season 1 -> Season 2 -> Season 3...)
        result: List[Dict[str, Any]] = []
        used_ih = set()
        for s_num in req_seasons:
            s_cands = [c for c in candidates if s_num in c["seasons"] and c["info_hash"] not in used_ih]
            if s_cands:
                s_cands.sort(key=lambda x: (x["seasons"] == [s_num], x["score"]), reverse=True)
                chosen = dict(s_cands[0])
                chosen["target_season"] = s_num
                used_ih.add(chosen["info_hash"])
                result.append(chosen)

        return result

    def extract_encoder_name(self, torrent_title: str, fallback_csv_encoder: str) -> str:
        """Extract clean encoder name from torrent title or CSV column."""
        m = re.match(r"^\[([^\]]+)\]", torrent_title.strip())
        if m:
            return m.group(1).strip()
        m_end = re.search(r"-([A-Za-z0-9_]+)(?:\.mkv)?$", torrent_title.strip())
        if m_end:
            return m_end.group(1).strip()
        clean_fb = fallback_csv_encoder.split("(")[0].strip()
        return clean_fb if clean_fb else "Judas"

    async def download_anilist_artwork(
        self,
        title: str,
        anilist_info: Optional[Dict[str, Any]],
    ) -> Tuple[str, str]:
        """
        Downloads high-res AniList coverImage (portrait) and bannerImage (widescreen).
        Returns (banner_bg_path, cover_side_path).
        """
        slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:40]
        cover_path = os.path.join(THUMBS_DIR, f"archival_cover_{slug}.jpg")
        banner_path = os.path.join(THUMBS_DIR, f"archival_banner_{slug}.jpg")

        if anilist_info:
            cover_url = (
                (anilist_info.get("coverImage") or {}).get("extraLarge")
                or (anilist_info.get("coverImage") or {}).get("large")
            )
            banner_url = anilist_info.get("bannerImage")

            if cover_url and not os.path.exists(cover_path):
                try:
                    req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                    data = urllib.request.urlopen(req, timeout=15).read()
                    if len(data) > 1000:
                        with open(cover_path, "wb") as f:
                            f.write(data)
                except Exception as e:
                    LOGGER.warning(f"Cover download failed for {title}: {e}")

            if banner_url and not os.path.exists(banner_path):
                try:
                    req = urllib.request.Request(banner_url, headers={"User-Agent": "Mozilla/5.0"})
                    data = urllib.request.urlopen(req, timeout=15).read()
                    if len(data) > 1000:
                        with open(banner_path, "wb") as f:
                            f.write(data)
                except Exception as e:
                    LOGGER.warning(f"Banner download failed for {title}: {e}")

        final_cover = cover_path if os.path.exists(cover_path) else DEFAULT_BANNER_PATH
        final_banner = banner_path if os.path.exists(banner_path) else final_cover
        return final_banner, final_cover

    def build_premium_poster_caption(
        self,
        title: str,
        romaji: str,
        year: str,
        chronology: str,
        total_eps: int,
        fmt: str,
        rating: str,
        studio: str,
        genres: str,
        encoder: str,
        stream_info: Dict[str, Any],
        synopsis: str,
    ) -> str:
        """
        Generates the Telegram Premium Custom Emoji caption for the Template 11 Poster Card
        using verified DocumentAttributeCustomEmoji IDs.
        """
        e_movie = '<tg-emoji id="5287575882108522821">📺</tg-emoji>'
        e_bolt = '<tg-emoji id="5201873494798776013">✨</tg-emoji>'
        e_stats = '<tg-emoji id="5231200819986047254">📊</tg-emoji>'
        e_screen = '<tg-emoji id="5375099322666859339">🖥</tg-emoji>'
        e_doc = '<tg-emoji id="5235630047959727475">💎</tg-emoji>'
        e_star = '<tg-emoji id="5208801655004350721">🌟</tg-emoji>'
        e_love = '<tg-emoji id="5384085485171778811">🥰</tg-emoji>'

        v_main = stream_info.get("video_stream_main", "1080p HD").replace("Ultra ", "")
        v_sub = stream_info.get("video_stream_sub", "HEVC • 10-bit SDR")
        qual = stream_info.get("quality", "BluRay")
        aud_badge = stream_info.get("audio_badge", "Japanese FLAC 2.0")
        sub_tag = stream_info.get("sub_tag", "ESub")

        short_syn = synopsis[:520].rsplit(" ", 1)[0] + "..." if len(synopsis) > 520 else synopsis

        lines = [
            f'{e_movie} <b>「 {html.escape(title)} ({html.escape(year)}) 」</b>',
        ]
        if romaji and romaji.lower() != title.lower():
            lines.append(f'{e_bolt} <i>{html.escape(romaji)}</i>')

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━",
            f"╭── {e_stats} <b>𝗔𝗡𝗜𝗟𝗜𝗦𝗧 &amp; 𝗦𝗘𝗥𝗜𝗘𝗦 𝗜𝗡𝗙𝗢</b>",
            f"├ <b>Chronology :</b> {html.escape(chronology)}",
            f"├ <b>Episodes :</b> {total_eps} ({html.escape(fmt)})",
            f"├ <b>Rating :</b> ★ {html.escape(rating)} / 10",
            f"├ <b>Studio :</b> {html.escape(studio)}",
            f"╰ <b>Genres :</b> {html.escape(genres)}",
            "",
            f"╭── {e_screen} <b>𝗠𝗘𝗗𝗜𝗔𝗜𝗡𝗙𝗢 &amp; 𝗦𝗧𝗥𝗘𝗔𝗠 𝗦𝗣𝗘𝗖𝗦</b>",
            f"├ <b>Video :</b> {html.escape(v_main)} • {html.escape(v_sub)} ({html.escape(qual)})",
            f"├ <b>Audio :</b> {html.escape(aud_badge)}",
            f"├ <b>Subtitles :</b> Anime Starfall Intro + {html.escape(sub_tag)}",
            f"╰ <b>Encoder :</b> {html.escape(encoder)}",
            "",
            f"{e_doc} <b>𝗦𝗬𝗡𝗢𝗣𝗦𝗜𝗦 :</b>",
            f"<blockquote expandable>{html.escape(short_syn)}</blockquote>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{e_star} <b>Powered by @Anime_Starfall</b> {e_love}",
        ])
        return "\n".join(lines)

    async def upload_document_to_dump(
        self,
        filepath: str,
        title: str,
        year: str,
        tag: str,
        stream_info: Dict[str, Any],
    ) -> Optional[int]:
        """
        Uploads an archival MKV as a DOCUMENT (send_document) to Main Dump (-1003887117402)
        with the official Starfall document thumbnail (data/document_thumb.jpg) and ONLY the bold
        autorename .mkv filename as caption (no extra downside metadata block), then deletes the local file.
        """
        fname = os.path.basename(filepath)
        if not os.path.exists(filepath):
            return None

        fsize_mib = os.path.getsize(filepath) / (1024 * 1024)
        if fsize_mib > 3950.0:
            LOGGER.error(f"❌ File {fname} ({fsize_mib:.1f} MiB) exceeds Telegram maximum 4000 MiB upload limit! Skipping.")
            try:
                os.remove(filepath)
            except Exception:
                pass
            return None

        doc_caption = f"<b>{html.escape(fname)}</b>"

        all_clients: List[Client] = list(self.helper_bots)
        if self.user_client:
            all_clients.append(self.user_client)

        if fsize_mib > 1950.0 and self.user_client:
            chosen_client = self.user_client
        else:
            chosen_client = all_clients[self._upload_rr_idx % len(all_clients)]
            self._upload_rr_idx += 1

        thumb = DOCUMENT_THUMB_PATH if os.path.exists(DOCUMENT_THUMB_PATH) else None

        async with self._upload_sem:
            t0 = time.time()
            dyn_timeout = max(1200.0, fsize_mib * 3.0)
            for attempt in range(1, 6):
                try:
                    msg: Message = await asyncio.wait_for(
                        chosen_client.send_document(
                            chat_id=DUMP_CHANNEL_ID,
                            document=filepath,
                            thumb=thumb,
                            caption=doc_caption,
                            parse_mode=ParseMode.HTML,
                            force_document=True,
                        ),
                        timeout=dyn_timeout,
                    )
                    elapsed = max(0.1, time.time() - t0)
                    mbps = (fsize_mib * 8.0) / elapsed
                    LOGGER.info(
                        f"✅ [Dump Document] {fname} ({fsize_mib:.1f} MiB) -> Msg #{msg.id} in {elapsed:.1f}s ({mbps:.1f} Mbps)"
                    )
                    # Instant Rolling Disk Purge
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    return msg.id

                except FloodWait as fw:
                    wait_s = int(getattr(fw, "value", 15)) + 5
                    LOGGER.warning(f"FloodWait ({wait_s}s) on document upload, switching client & sleeping...")
                    await asyncio.sleep(wait_s)
                    if fsize_mib <= 1950.0:
                        chosen_client = all_clients[self._upload_rr_idx % len(all_clients)]
                        self._upload_rr_idx += 1
                except asyncio.TimeoutError:
                    LOGGER.warning(f"Upload timed out (360s) for {fname} on attempt {attempt}, switching client & retrying...")
                    await asyncio.sleep(2.0 * attempt)
                    if fsize_mib <= 1950.0:
                        chosen_client = all_clients[self._upload_rr_idx % len(all_clients)]
                        self._upload_rr_idx += 1
                except Exception as e:
                    LOGGER.warning(f"Document upload attempt {attempt} failed for {fname}: {e}")
                    await asyncio.sleep(2.0 * attempt)
                    if fsize_mib <= 1950.0:
                        chosen_client = all_clients[self._upload_rr_idx % len(all_clients)]
                        self._upload_rr_idx += 1

        return None

    async def stage_franchise_to_dump(
        self,
        row: Dict[str, Any],
        batch_info: Dict[str, Any],
        info_hash: str,
    ) -> List[Dict[str, Any]]:
        """
        Phase A (Blind Dump Stage):
        1. Classifies & sorts all episodes/OVAs/extras and groups them by Season (S01, S02, S03...)
           so multi-season packs are split into chronological seasons.
        2. Injects Starfall Intro Subtitle + Cover Art and streams uploads across 16 concurrent MTProto
           transfers into Main Dump (-1003887117402) with bold .mkv filename captions.
        3. Purges local files & raw torrent immediately.
        4. Pre-renders the upgraded Template 11 Poster Card (without 'Ultra') for each season and appends
           to self.staged_wave to be forwarded in strict chronological watch order!
        """
        gid = str(row.get("Global_ID", "")).strip()
        raw_franchise = row.get("Franchise_Title", "Unknown Anime").strip()
        base_title = re.sub(r"\(.*?\)", "", raw_franchise).strip() or raw_franchise
        chronology = row.get("Complete_Chronology", "Complete Series").strip()
        torrent_title = batch_info.get("title", raw_franchise)
        encoder = self.extract_encoder_name(torrent_title, row.get("Encoder_And_Codec", "Judas"))
        default_season = int(batch_info.get("target_season", 1) or 1)
        req_seasons = list(batch_info.get("required_seasons") or self.parse_required_seasons(chronology))

        LOGGER.info("=" * 75)
        LOGGER.info(
            f"⚡ [BLIND DUMP WAVE {len(self.staged_wave) + 1}/{BLIND_DUMP_WAVE_SIZE}] "
            f"#{gid}: {raw_franchise} (Target S{default_season:02d}) | Encoder: {encoder}"
        )
        LOGGER.info(f"   Torrent: {torrent_title}")
        LOGGER.info("=" * 75)

        # 1. Gather video files from torrent (skipping any Sample/ trailer files)
        t_files = self.qb.get_torrent_files(info_hash)
        video_paths: List[str] = []
        for tf in t_files:
            rel_name = tf.get("name", "")
            rel_low = rel_name.lower()
            if "/sample/" in rel_low or "sample" in os.path.basename(rel_low):
                continue
            if rel_low.endswith((".mkv", ".mp4")):
                full_p = os.path.join(DOWNLOAD_DIR, rel_name)
                if os.path.exists(full_p) and os.path.getsize(full_p) > 5 * 1024 * 1024:
                    video_paths.append(full_p)

        if not video_paths:
            LOGGER.error(f"No valid video files found on disk for #{gid} ({raw_franchise})!")
            return []

        # 2. Fetch AniList Metadata & Artwork
        anilist_data = self.anilist.search_anime(raw_franchise) or self.anilist.search_anime(base_title)
        year = "2024"
        romaji = base_title
        rating = "8.3"
        fmt = "TV"
        studio = "MAPPA / Madhouse"
        genres_list = ["Action", "Drama", "Fantasy"]
        synopsis = f"Complete master archival release of {raw_franchise} ({chronology}) in 1080p high definition."

        if anilist_data:
            if anilist_data.get("seasonYear"):
                year = str(anilist_data["seasonYear"])
            t_obj = anilist_data.get("title") or {}
            if t_obj.get("romaji"):
                romaji = t_obj["romaji"]
            if anilist_data.get("averageScore"):
                rating = f"{float(anilist_data['averageScore']) / 10.0:.1f}"
            if anilist_data.get("format"):
                fmt = str(anilist_data["format"])
            if anilist_data.get("genres"):
                genres_list = list(anilist_data["genres"])[:4]
            st_nodes = (anilist_data.get("studios") or {}).get("nodes") or []
            if st_nodes and st_nodes[0].get("name"):
                studio = st_nodes[0]["name"]
            desc = anilist_data.get("description") or ""
            if desc:
                clean_desc = re.sub(r"<[^>]+>", "", desc).strip()
                clean_desc = re.sub(r"\(Source:.*?\)", "", clean_desc, flags=re.IGNORECASE).strip()
                if clean_desc:
                    synopsis = clean_desc

        banner_bg_path, cover_side_path = await self.download_anilist_artwork(base_title, anilist_data)

        # 3. Classify & Group all video files by chronological season (S01, S02, S03...)
        classified_items: List[Tuple[int, int, int, str, str]] = []
        for vp in video_paths:
            cat_rank, s_num, e_num, tag_str = parse_episode_or_special_tag(vp, default_season=default_season)
            classified_items.append((cat_rank, s_num, e_num, tag_str, vp))

        classified_items.sort(key=lambda x: (x[1], x[0], x[2], os.path.basename(x[4])))

        by_season: Dict[int, List[Tuple[int, int, int, str, str]]] = {}
        for item in classified_items:
            s_key = item[1] if item[1] >= 1 else default_season
            by_season.setdefault(s_key, []).append(item)

        loop = asyncio.get_running_loop()
        staged_packages: List[Dict[str, Any]] = []

        for s_num in sorted(by_season.keys()):
            s_items_raw = by_season[s_num]
            s_items_raw.sort(key=lambda x: (x[0], x[2], os.path.basename(x[4])))

            # Skip if this exact (gid, s_num) is already staged in self.staged_wave
            if any(str(p.get("gid")) == gid and int(p.get("season_num", 1)) == s_num for p in self.staged_wave):
                LOGGER.info(f"⏭️ Season {s_num:02d} of #{gid} ({raw_franchise}) already staged in wave, skipping.")
                continue

            used_tags: Dict[str, int] = {}
            final_items: List[Tuple[str, str]] = []
            for cat_rank, _, e_num, tag_str, vp in s_items_raw:
                if tag_str in used_tags:
                    used_tags[tag_str] += 1
                    tag_str = f"{tag_str} v{used_tags[tag_str]}"
                else:
                    used_tags[tag_str] = 1
                final_items.append((tag_str, vp))

            primary_stream_info = probe_media_streams(final_items[0][1], torrent_name=torrent_title)
            primary_stream_info["video_stream_main"] = primary_stream_info["video_stream_main"].replace("Ultra ", "")
            LOGGER.info(
                f"🔍 [#{gid} S{s_num:02d}] Probed Specs: Video={primary_stream_info['video_stream_main']} "
                f"({primary_stream_info['video_stream_sub']}) | Audio={primary_stream_info['audio_badge']} | Sub={primary_stream_info['sub_tag']}"
            )

            t_stage_start = time.time()
            total_bytes = sum(os.path.getsize(vp) for _, vp in final_items if os.path.exists(vp))

            async def _inject_and_upload_one(idx_ep: int, tag_str: str, src_vp: str) -> List[Tuple[int, str, Optional[int], str]]:
                s_info = probe_media_streams(src_vp, torrent_name=torrent_title)
                out_fname = format_archival_filename(
                    title=base_title,
                    tag=tag_str,
                    year=year,
                    encoder=encoder,
                    stream_info=s_info,
                )
                out_path = os.path.join(STAGING_DIR, f"{gid}_S{s_num:02d}_{idx_ep:03d}_{out_fname}")
                ok = await loop.run_in_executor(
                    self.inject_executor,
                    inject_starfall_archival_branding,
                    src_vp,
                    out_path,
                    INTRO_ASS_PATH,
                    DEFAULT_BANNER_PATH,
                )
                if not ok or not os.path.exists(out_path):
                    return [(idx_ep, tag_str, None, out_fname)]

                ep_dir = os.path.join(STAGING_DIR, f"{gid}_S{s_num:02d}_{idx_ep:03d}")
                os.makedirs(ep_dir, exist_ok=True)
                clean_out_path = os.path.join(ep_dir, out_fname)
                os.replace(out_path, clean_out_path)

                fsize_mib = os.path.getsize(clean_out_path) / (1024 * 1024)
                if fsize_mib > 3950.0:
                    LOGGER.info(
                        f"✂️ File {out_fname} ({fsize_mib:.1f} MiB) exceeds Telegram 4000 MiB limit! "
                        f"Splitting into keyframe-accurate playable MKV parts (< 1950 MiB) via mkvmerge..."
                    )
                    split_tpl = os.path.join(ep_dir, "split_part_%02d.mkv")
                    cmd = [
                        "mkvmerge",
                        "-o", split_tpl,
                        "--split", "size:1950M",
                        clean_out_path,
                    ]
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, err = await proc.communicate()
                    # Remove original oversize file immediately to preserve NVMe space
                    try:
                        os.remove(clean_out_path)
                    except Exception:
                        pass

                    part_files = sorted(glob.glob(os.path.join(ep_dir, "split_part_*.mkv")))
                    if not part_files:
                        LOGGER.error(f"Failed to split {out_fname}: {err.decode() if err else 'No parts generated'}")
                        try:
                            os.rmdir(ep_dir)
                        except Exception:
                            pass
                        return [(idx_ep, tag_str, None, out_fname)]

                    LOGGER.info(f"✅ Generated {len(part_files)} playable split parts for {out_fname}")
                    part_results: List[Tuple[int, str, Optional[int], str]] = []
                    for p_idx, part_fp in enumerate(part_files, 1):
                        part_tag = f"{tag_str} Part {p_idx:02d}"
                        old_tag_sub = f"[{tag_str}]"
                        new_tag_sub = f"[{part_tag}]"
                        if old_tag_sub in out_fname:
                            part_fname = out_fname.replace(old_tag_sub, new_tag_sub, 1)
                        else:
                            part_fname = f"🄰🅂- [{part_tag}] {out_fname.replace('🄰🅂- ', '', 1)}"

                        final_part_path = os.path.join(ep_dir, part_fname)
                        os.replace(part_fp, final_part_path)

                        p_mid = await self.upload_document_to_dump(
                            filepath=final_part_path,
                            title=base_title,
                            year=year,
                            tag=part_tag,
                            stream_info=s_info,
                        )
                        try:
                            if os.path.exists(final_part_path):
                                os.remove(final_part_path)
                        except Exception:
                            pass
                        part_results.append((idx_ep, part_tag, p_mid, part_fname))

                    try:
                        os.rmdir(ep_dir)
                    except Exception:
                        pass
                    return part_results

                mid = await self.upload_document_to_dump(
                    filepath=clean_out_path,
                    title=base_title,
                    year=year,
                    tag=tag_str,
                    stream_info=s_info,
                )
                try:
                    if os.path.exists(clean_out_path):
                        os.remove(clean_out_path)
                    os.rmdir(ep_dir)
                except Exception:
                    pass
                return [(idx_ep, tag_str, mid, out_fname)]

            staged_results: List[Tuple[int, str, Optional[int], str]] = []
            wave_step = 6
            for w_start in range(0, len(final_items), wave_step):
                slice_items = final_items[w_start : w_start + wave_step]
                coros = [
                    _inject_and_upload_one(w_start + offset, t_str, s_vp)
                    for offset, (t_str, s_vp) in enumerate(slice_items)
                ]
                res_slice = await asyncio.gather(*coros)
                for r in res_slice:
                    staged_results.extend(r)

            staged_results.sort(key=lambda x: (x[0], x[1]))
            staged_dump_msgs: List[List[Any]] = [
                [t_str, mid, fname_str] for _, t_str, mid, fname_str in staged_results if mid is not None
            ]

            stage_elapsed = max(0.1, time.time() - t_stage_start)
            agg_mbps = ((total_bytes / (1024 * 1024)) * 8.0) / stage_elapsed
            LOGGER.info(
                f"🚀 [Blind Dump Complete] #{gid} {raw_franchise} (Season {s_num:02d}): "
                f"{len(staged_dump_msgs)}/{len(final_items)} episodes uploaded in {stage_elapsed:.1f}s "
                f"(Aggregate Throughput: {agg_mbps:.1f} Mbps)"
            )

            if not staged_dump_msgs:
                continue

            total_eps = len(staged_dump_msgs)
            is_multi = len(req_seasons) > 1 or len(by_season) > 1
            season_display_title = f"{base_title} - Season {s_num}" if (is_multi and s_num > 1) else base_title
            season_chronology = f"Season {s_num:02d} ({chronology})" if is_multi else chronology

            t11_data = {
                "title": season_display_title,
                "romaji_title": romaji if romaji.lower() != base_title.lower() else "",
                "year": year,
                "format": fmt,
                "season": s_num,
                "episodes": total_eps,
                "is_batch": True,
                "status_badge": (
                    f"Season {s_num:02d} • {total_eps} Episodes"
                    if is_multi
                    else (f"Batch • {total_eps} Episodes" if total_eps > 1 else "Complete Movie")
                ),
                "rating": rating,
                "genres": genres_list,
                "resolution": primary_stream_info["resolution"],
                "codec": primary_stream_info["codec_badge"],
                "bit_depth": primary_stream_info["bit_num"],
                "audio": primary_stream_info["audio_badge"],
                "video_stream_main": primary_stream_info["video_stream_main"],
                "video_stream_sub": primary_stream_info["video_stream_sub"],
                "release_card_main": f"Season {s_num:02d} Batch" if is_multi else ("Complete Series" if total_eps > 1 else "Feature Film"),
                "release_card_sub": f"{year} • {total_eps} Episodes",
                "progress_label": f"SEASON {s_num:02d} COMPLETE • 100%",
                "synopsis": synopsis,
                "brand": "Anime_Starfall",
            }

            slug = re.sub(r"[^\w\s-]", "", base_title).strip().replace(" ", "_")[:36]
            hero_card_path = os.path.join(THUMBS_DIR, f"archival_t11_{gid}_S{s_num:02d}_{slug}.jpg")
            poster_img = _template_eleven(bg=banner_bg_path, side=cover_side_path, data=t11_data)
            poster_img.save(hero_card_path, "JPEG", quality=95)

            caption_html = self.build_premium_poster_caption(
                title=season_display_title,
                romaji=romaji,
                year=year,
                chronology=season_chronology,
                total_eps=total_eps,
                fmt=fmt,
                rating=rating,
                studio=studio,
                genres=", ".join(genres_list),
                encoder=encoder,
                stream_info=primary_stream_info,
                synopsis=synopsis,
            )

            staged_pkg = {
                "gid": gid,
                "season_num": s_num,
                "required_seasons": req_seasons,
                "raw_franchise": raw_franchise,
                "base_title": base_title,
                "encoder": encoder,
                "total_eps": total_eps,
                "staged_dump_msgs": staged_dump_msgs,
                "hero_card_path": hero_card_path,
                "caption_html": caption_html,
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }
            self.staged_wave.append(staged_pkg)
            staged_packages.append(staged_pkg)
            self.save_state()

        # 6. Purge raw torrent from qBittorrent immediately after staging all seasons in torrent to Main Dump!
        LOGGER.info(f"🧹 [Disk Guard] Deleting raw torrent {torrent_title} from qBittorrent...")
        self.qb.delete_torrent(info_hash, delete_files=True)
        LOGGER.info(f"💾 Free NVMe Space: {get_free_disk_gib():.1f} GiB")

        return staged_packages

    async def flush_staged_wave_to_target(self, active_downloads: Optional[Dict[str, Any]] = None, force_all: bool = False):
        """
        Phase B (Orderly Forward Wave in Strict Chronological Watch Order):
        1. Sorts self.staged_wave by (int(gid), int(season_num)) so that for every anime,
           Season 1 (Episodes + Season 1 Poster Card) is ALWAYS forwarded first, followed immediately
           by Season 2 (Episodes + Season 2 Poster Card), followed by Season 3!
        2. If a multi-season franchise still has another season actively downloading (or Season 1 is not yet staged),
           holds that franchise in self.staged_wave until all of its seasons are staged (unless force_all=True).
        """
        if not self.staged_wave:
            return

        # Sort strictly by (Global_ID, Season_Num) so Season 1 -> Season 2 -> Season 3 order is guaranteed!
        self.staged_wave.sort(key=lambda p: (int(p.get("gid", 999999)), int(p.get("season_num", 1))))
        self.save_state()

        active_gids = set()
        if active_downloads and not force_all:
            for _, (r_row, _, _) in active_downloads.items():
                active_gids.add(str(r_row.get("Global_ID", "")).strip())

        ready_packages: List[Dict[str, Any]] = []
        deferred_packages: List[Dict[str, Any]] = []

        # Group by gid to verify chronological completeness before forwarding a multi-season franchise
        by_gid: Dict[str, List[Dict[str, Any]]] = {}
        for pkg in self.staged_wave:
            by_gid.setdefault(str(pkg.get("gid")), []).append(pkg)

        for gid, pkgs in by_gid.items():
            pkgs.sort(key=lambda x: int(x.get("season_num", 1)))
            staged_s_nums = [int(x.get("season_num", 1)) for x in pkgs]
            if not force_all and (gid in active_gids or 1 not in staged_s_nums):
                LOGGER.info(
                    f"⏳ Holding #{gid} ({pkgs[0].get('raw_franchise')}) in Dump until all chronological seasons "
                    f"(currently staged: {staged_s_nums}) finish staging..."
                )
                deferred_packages.extend(pkgs)
            else:
                ready_packages.extend(pkgs)

        if not ready_packages:
            return

        LOGGER.info("=" * 75)
        LOGGER.info(
            f"📨 [ORDERLY CHRONOLOGICAL FORWARD WAVE] Forwarding {len(ready_packages)} staged season batches "
            f"to Target Channel ({TARGET_CHANNEL_ID})..."
        )
        LOGGER.info("=" * 75)

        for pkg in ready_packages:
            gid = str(pkg["gid"])
            s_num = int(pkg.get("season_num", 1))
            raw_franchise = pkg["raw_franchise"]
            encoder = pkg["encoder"]
            staged_dump_msgs = pkg["staged_dump_msgs"]
            hero_card_path = pkg["hero_card_path"]
            caption_html = pkg["caption_html"]
            total_eps = len(staged_dump_msgs)

            LOGGER.info(
                f"📦 [Forwarding #{gid} • Season {s_num:02d}] {raw_franchise} ({total_eps} episodes) -> Target Channel..."
            )
            target_ep_msg_ids: List[int] = []
            first_ep_msg_id: Optional[int] = None

            for idx, item_entry in enumerate(staged_dump_msgs, 1):
                tag_str = item_entry[0]
                dump_mid = int(item_entry[1])
                fname_override = item_entry[2] if len(item_entry) > 2 else None

                for attempt in range(1, 5):
                    try:
                        copy_kwargs: Dict[str, Any] = {
                            "chat_id": TARGET_CHANNEL_ID,
                            "from_chat_id": DUMP_CHANNEL_ID,
                            "message_id": dump_mid,
                        }
                        if fname_override:
                            copy_kwargs["caption"] = f"<b>{html.escape(fname_override)}</b>"
                            copy_kwargs["parse_mode"] = ParseMode.HTML

                        fwd_msg = await self.user_client.copy_message(**copy_kwargs)
                        if first_ep_msg_id is None:
                            first_ep_msg_id = fwd_msg.id
                        target_ep_msg_ids.append(fwd_msg.id)
                        LOGGER.info(
                            f"   [{idx:02d}/{total_eps:02d}] Forwarded [{tag_str}] -> Target Msg #{fwd_msg.id}"
                        )
                        break
                    except FloodWait as fw:
                        await asyncio.sleep(int(fw.value) + 5)
                    except Exception as e:
                        LOGGER.warning(f"Forward error on {tag_str} (attempt {attempt}): {e}")
                        await asyncio.sleep(3.0 * attempt)

                await asyncio.sleep(FORWARD_DELAY_SEC)

            if not first_ep_msg_id:
                LOGGER.error(f"Failed to forward episodes for #{gid} S{s_num:02d} ({raw_franchise})!")
                if pkg in self.staged_wave:
                    self.staged_wave.remove(pkg)
                    self.save_state()
                continue

            click_here_url = f"https://t.me/c/{TARGET_CLEAN_ID}/{first_ep_msg_id}"
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎬 Click Here", url=click_here_url)]]
            )

            poster_msg_id = 0
            for attempt in range(1, 4):
                try:
                    use_caption = caption_html if attempt < 3 else re.sub(r"</?tg-emoji[^>]*>", "", caption_html)
                    card_msg = await self.user_client.send_photo(
                        chat_id=TARGET_CHANNEL_ID,
                        photo=hero_card_path,
                        caption=use_caption,
                        parse_mode=ParseMode.HTML,
                    )
                    poster_msg_id = card_msg.id
                    if self.helper_bots:
                        await asyncio.sleep(0.4)
                        await self.helper_bots[0].edit_message_reply_markup(
                            chat_id=TARGET_CHANNEL_ID,
                            message_id=poster_msg_id,
                            reply_markup=reply_markup,
                        )
                    LOGGER.info(
                        f"🎉 Published Template 11 Poster Card for #{gid} S{s_num:02d} ({raw_franchise}) -> Msg #{poster_msg_id} "
                        f"with [🎬 Click Here] -> {click_here_url}"
                    )
                    break
                except FloodWait as fw:
                    await asyncio.sleep(int(fw.value) + 5)
                except Exception as e:
                    LOGGER.warning(f"Poster card send/markup attempt {attempt} warning: {e}")
                    await asyncio.sleep(2.5 * attempt)

            prev_entry = self.published_state.get(gid, {})
            published_seasons = list(prev_entry.get("published_seasons", []))
            if s_num not in published_seasons:
                published_seasons.append(s_num)
                published_seasons.sort()

            self.published_state[gid] = {
                "global_id": int(gid),
                "franchise_title": raw_franchise,
                "encoder": encoder,
                "published_seasons": published_seasons,
                "episodes_published": int(prev_entry.get("episodes_published", 0)) + total_eps,
                "first_ep_msg_id": prev_entry.get("first_ep_msg_id") or first_ep_msg_id,
                "poster_msg_id": poster_msg_id,
                "click_here_url": click_here_url,
                "target_ep_msg_ids": list(prev_entry.get("target_ep_msg_ids", [])) + target_ep_msg_ids,
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
            if pkg in self.staged_wave:
                self.staged_wave.remove(pkg)
            self.save_state()
            await asyncio.sleep(FORWARD_DELAY_SEC)

    async def run(self):
        await self.initialize_clients()
        catalogue = self.load_catalogue()
        LOGGER.info(f"📚 Loaded {len(catalogue)} franchises from {CATALOG_CSV_PATH}")

        # Active downloads map: info_hash -> (row, batch_info, added_time)
        active_downloads: Dict[str, Tuple[Dict[str, Any], Dict[str, Any], float]] = {}
        cat_idx = 0

        try:
            while cat_idx < len(catalogue) or active_downloads:
                # 1. Fill pre-buffer slots in qBittorrent enforcing STRICT >= 550.0 GiB Free SSD Floor
                while (
                    cat_idx < len(catalogue)
                    and len(active_downloads) < MAX_CONCURRENT_TORRENTS
                ):
                    free_gib = get_free_disk_gib()
                    if free_gib < MIN_FREE_DISK_GIB:
                        LOGGER.warning(
                            f"🛡️ [Disk Guard] Free NVMe space {free_gib:.1f} GiB < {MIN_FREE_DISK_GIB:.1f} GiB floor! "
                            f"Holding off new torrent downloads until space clears."
                        )
                        break

                    row = catalogue[cat_idx]
                    gid = str(row.get("Global_ID", "")).strip()

                    if gid in self.published_state:
                        cat_idx += 1
                        continue

                    batches = self.resolve_nyaa_chronological_batches(row)
                    if not batches:
                        LOGGER.warning(f"⚠️ Could not resolve active Nyaa batch for #{gid}: {row.get('Franchise_Title')}")
                        cat_idx += 1
                        continue

                    # Filter batches not already staged or active
                    batches_to_add = []
                    for batch in batches:
                        t_season = int(batch.get("target_season", 1))
                        if any(
                            str(p.get("gid")) == gid and int(p.get("season_num", 1)) == t_season
                            for p in self.staged_wave
                        ):
                            continue
                        ih = batch["info_hash"]
                        if ih in active_downloads:
                            continue
                        batches_to_add.append(batch)

                    if not batches_to_add:
                        cat_idx += 1
                        continue

                    total_batch_sz = sum(b.get("size_gib", 0.0) for b in batches_to_add)
                    if (free_gib - total_batch_sz) < MIN_FREE_DISK_GIB:
                        LOGGER.warning(
                            f"🛡️ [Disk Guard] Adding #{gid} ({total_batch_sz:.1f} GiB) would lower free space to "
                            f"{free_gib - total_batch_sz:.1f} GiB (< {MIN_FREE_DISK_GIB:.1f} GiB floor). Holding queue..."
                        )
                        break

                    cat_idx += 1
                    for batch in batches_to_add:
                        t_season = int(batch.get("target_season", 1))
                        ih = batch["info_hash"]
                        mag_or_url = batch.get("magnet_uri") or batch.get("torrent_url", "")
                        LOGGER.info(
                            f"📥 [Queue #{gid} • S{t_season:02d}] Adding to qBittorrent: {batch['title']} "
                            f"({batch['size_gib']:.2f} GiB | {batch['seeders']} seeds | Seasons {batch.get('seasons')})"
                        )
                        added_hash = self.qb.add_torrent(
                            magnet_or_url=mag_or_url,
                            container_save_path="/usr/src/app/downloads",
                            category="starfall_archival",
                        )
                        final_hash = (added_hash or ih).lower()
                        active_downloads[final_hash] = (row, batch, time.time())
                        await asyncio.sleep(0.8)

                if not active_downloads:
                    if cat_idx >= len(catalogue):
                        break
                    await asyncio.sleep(5.0)
                    continue

                # 2. Poll active downloads for 100% completion or stalled timeout & monitor SSD floor
                free_gib = get_free_disk_gib()
                if free_gib < MIN_FREE_DISK_GIB:
                    LOGGER.warning(
                        f"⚠️ [Disk Guard Alert] Free NVMe space ({free_gib:.1f} GiB) dipped below {MIN_FREE_DISK_GIB:.1f} GiB floor! "
                        f"Purging temporary staging artifacts..."
                    )
                    for root, dirs, files in os.walk(STAGING_DIR):
                        for f in files:
                            fp = os.path.join(root, f)
                            try:
                                if time.time() - os.path.getmtime(fp) > 180:
                                    os.remove(fp)
                            except Exception:
                                pass

                completed_hashes: List[str] = []
                stalled_hashes: List[str] = []

                for ih, (row, batch, start_ts) in list(active_downloads.items()):
                    t_info = self.qb.get_torrent(ih)
                    gid = row.get("Global_ID")
                    if not t_info:
                        if time.time() - start_ts > 120:
                            LOGGER.warning(f"Torrent #{gid} ({ih}) disappeared from qBittorrent, dropping.")
                            stalled_hashes.append(ih)
                        continue

                    prog = float(t_info.get("progress", 0.0))
                    state = str(t_info.get("state", ""))
                    dlspeed_kib = float(t_info.get("dlspeed", 0)) / 1024.0
                    elapsed_min = (time.time() - start_ts) / 60.0

                    if prog >= 0.999 or state in ("uploading", "stalledUP", "queuedUP", "pausedUP", "checkingUP"):
                        completed_hashes.append(ih)
                    elif elapsed_min > 3.0 and prog < 0.02 and dlspeed_kib < 100.0:
                        LOGGER.warning(f"⚠️ Torrent #{gid} crawling at {dlspeed_kib:.1f} KiB/s ({prog*100:.1f}%) after {elapsed_min:.1f}m, dropping to unblock queue.")
                        self.qb.delete_torrent(ih, delete_files=True)
                        stalled_hashes.append(ih)
                    elif elapsed_min > 6.0 and prog < 0.05 and dlspeed_kib < 250.0:
                        LOGGER.warning(f"⚠️ Torrent #{gid} crawling at {dlspeed_kib:.1f} KiB/s ({prog*100:.1f}%) after {elapsed_min:.1f}m, dropping.")
                        self.qb.delete_torrent(ih, delete_files=True)
                        stalled_hashes.append(ih)
                    elif elapsed_min > 30.0 and prog < 0.85:
                        LOGGER.warning(f"⚠️ Torrent #{gid} exceeded 30m download window ({prog*100:.1f}%), purging.")
                        self.qb.delete_torrent(ih, delete_files=True)
                        stalled_hashes.append(ih)

                for sh in stalled_hashes:
                    active_downloads.pop(sh, None)

                # 3. Process any completed torrents into Main Dump (Blind Dump Wave of 10)!
                if completed_hashes:
                    for ch in completed_hashes:
                        row, batch, _ = active_downloads.pop(ch)
                        try:
                            await self.stage_franchise_to_dump(row, batch, ch)
                        except Exception as e:
                            LOGGER.error(f"Error staging franchise #{row.get('Global_ID')}: {e}", exc_info=True)
                            self.qb.delete_torrent(ch, delete_files=True)

                        # As soon as 10 season batches are staged in Main Dump, flush ready franchises in chronological order!
                        if len(self.staged_wave) >= BLIND_DUMP_WAVE_SIZE:
                            await self.flush_staged_wave_to_target(active_downloads=active_downloads, force_all=False)
                else:
                    # Log live download progress every 8 seconds
                    status_parts = []
                    for ih, (row, batch, _) in active_downloads.items():
                        ti = self.qb.get_torrent(ih)
                        if ti:
                            p = float(ti.get("progress", 0.0)) * 100.0
                            spd = float(ti.get("dlspeed", 0)) / (1024 * 1024)
                            s_lbl = f"S{int(batch.get('target_season', 1)):02d}"
                            status_parts.append(f"#{row.get('Global_ID')}({s_lbl}) {row.get('Franchise_Title')[:15]}: {p:.1f}% ({spd:.1f} MB/s)")
                    if status_parts:
                        LOGGER.info(
                            f"⏳ [Staged in Dump: {len(self.staged_wave)}/{BLIND_DUMP_WAVE_SIZE}] "
                            + " | ".join(status_parts)
                        )
                    await asyncio.sleep(8.0)

            # Flush any remaining staged anime at end of catalogue
            if self.staged_wave:
                await self.flush_staged_wave_to_target(active_downloads=None, force_all=True)

        finally:
            await self.close_clients()


def main():
    pipeline = MasterArchivalPipeline()
    asyncio.run(pipeline.run())


if __name__ == "__main__":
    main()
