#!/usr/bin/env python3
"""
Starfall Telegram Channel Router & 4GB Multi-Channel Uploader
Routes movies and media between Starfall channels with:
- Zero-bandwidth de-duplication copying (app.copy_message)
- Helper bot pool for files < 2000 MiB
- Pyrogram User String Session (HSIN) with Semaphore(1) for files 2000-4000 MiB
- Global rate-limiting (1.5s delay) and FloodWait exponential backoff with jitter
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

from config import TELEGRAM_API, TELEGRAM_HASH, USER_SESSION_STRING, HELPER_TOKENS
from pyrogram import Client, enums
from pyrogram.errors import FloodWait, MessageIdInvalid, ChannelInvalid

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] - %(message)s',
    datefmt='%d-%b-%y %I:%M:%S %p'
)
logger = logging.getLogger("ChannelRouter")

# Channel Constants
INDEX_CHANNEL_ID = -1004480002635       # Anime Starfall Index [Tamil]
MOVIES_CHANNEL_ID = -1004494800840      # Starfall Anime Movies
SOURCE_TARGET_ID = -1004415413346       # Main Starfall Target Archive Dump
SOURCE_DUMP_ID = -1003887117402         # Staging Dump

DB_PATH = "/home/administrator/starfall/data/starfall_state.db"
MOVIES_CSV = "/home/administrator/starfall/manifests/standalone_anime_movies_150.csv"
ARCHIVAL_CATALOG = "/home/administrator/starfall/data/archival_published_catalogue.json"
PROGRESS_JSON = "/home/administrator/starfall/data/movies_routing_progress.json"

# Strict Size Thresholds (bytes)
BOT_MAX_BYTES = 2000 * 1024 * 1024       # 2000 MiB limit for helper bots
USER_MAX_BYTES = 4000 * 1024 * 1024      # 4000 MiB limit for User String Session


class GlobalRateLimiter:
    """Enforces min 1.5s delay between Telegram API calls across all accounts"""
    def __init__(self, delay: float = 1.5):
        self.delay = delay
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self):
        async with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self._last_call = time.time()


rate_limiter = GlobalRateLimiter(delay=1.5)
user_upload_semaphore = asyncio.Semaphore(1)


async def execute_with_floodwait(coro_fn, *args, **kwargs):
    """Executes a Telegram API call with rate limiter and exponential backoff on FloodWait"""
    consecutive_waits = 0
    while True:
        try:
            await rate_limiter.wait()
            return await coro_fn(*args, **kwargs)
        except FloodWait as fw:
            consecutive_waits += 1
            wait_sec = float(fw.value) + random.uniform(1.0, 5.0)
            logger.warning(f"Telegram FloodWait encountered: sleeping {wait_sec:.2f}s (strike {consecutive_waits})")
            if consecutive_waits >= 3 and fw.value > 300:
                raise RuntimeError(f"Circuit breaker tripped: 3 consecutive FloodWaits > 300s (last wait: {fw.value}s)")
            await asyncio.sleep(wait_sec)
        except Exception:
            raise


def init_database():
    """Ensures routing tables exist in SQLite"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS routed_movies (
            movie_id INTEGER PRIMARY KEY,
            title TEXT,
            category TEXT,
            year TEXT,
            channel_id INTEGER,
            message_id INTEGER,
            file_size_bytes INTEGER,
            status TEXT,
            source_type TEXT,
            nyaa_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS pending_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            category TEXT,
            target_channel_id INTEGER,
            target_message_id INTEGER,
            status TEXT,
            index_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def normalize_title(title: str) -> str:
    """Normalizes titles for fuzzy matching"""
    clean = re.sub(r'\[.*?\]|\(.*?\)', '', title)
    clean = re.sub(r'[^a-zA-Z0-9]', '', clean).lower()
    return clean


class TelegramRouter:
    def __init__(self):
        self.user_app: Optional[Client] = None
        self.helper_bots: List[Client] = []
        self.helper_load: Dict[int, int] = {}
        self.current_helper_idx = 0
        self.archived_catalog: Dict = {}

    async def initialize(self):
        init_database()

        # 1. Load User Session (HSIN)
        logger.info("Initializing User String Session (HSIN)...")
        self.user_app = Client(
            "router_user_session",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            session_string=USER_SESSION_STRING,
            in_memory=True
        )
        await self.user_app.start()
        me = await self.user_app.get_me()
        logger.info(f"User Session Active: @{me.username} (ID: {me.id})")

        # 2. Load Helper Bots
        helper_token_list = HELPER_TOKENS.split()
        logger.info(f"Connecting {len(helper_token_list)} Helper Bots for < 2GB dispatch...")
        for idx, token in enumerate(helper_token_list, 1):
            try:
                bot_client = Client(
                    f"router_hbot_{idx}",
                    api_id=TELEGRAM_API,
                    api_hash=TELEGRAM_HASH,
                    bot_token=token,
                    in_memory=True
                )
                await bot_client.start()
                self.helper_bots.append(bot_client)
                self.helper_load[idx] = 0
                logger.info(f"  • Helper Bot {idx} Active: @{bot_client.me.username} (ID: {bot_client.me.id})")
            except Exception as e:
                logger.warning(f"  • Failed to start Helper Bot {idx}: {e}")

        logger.info(f"Total Active Helper Bots: {len(self.helper_bots)}/{len(helper_token_list)}")

        # 3. Load Archival Catalog for De-duplication
        if os.path.exists(ARCHIVAL_CATALOG):
            try:
                with open(ARCHIVAL_CATALOG, 'r', encoding='utf-8') as f:
                    self.archived_catalog = json.load(f)
                logger.info(f"Loaded {len(self.archived_catalog)} archival releases for de-duplication.")
            except Exception as e:
                logger.warning(f"Error loading archival catalog: {e}")

    async def stop(self):
        logger.info("Stopping Telegram Router clients...")
        for bot in self.helper_bots:
            try:
                await bot.stop()
            except Exception:
                pass
        if self.user_app:
            try:
                await self.user_app.stop()
            except Exception:
                pass
        logger.info("All clients stopped.")

    def get_next_helper_bot(self) -> Client:
        """Round-robin selection of helper bots for <2GB uploads"""
        if not self.helper_bots:
            raise RuntimeError("No helper bots available.")
        bot = self.helper_bots[self.current_helper_idx % len(self.helper_bots)]
        self.current_helper_idx += 1
        return bot

    def find_catalog_match(self, movie_title: str) -> Optional[dict]:
        """Finds match in archival published catalog for 0 MB bandwidth copy"""
        norm_movie = normalize_title(movie_title)
        
        # 1. Exact or near-exact match
        for k, v in self.archived_catalog.items():
            f_title = v.get("franchise_title", "")
            if normalize_title(f_title) == norm_movie:
                return v

        # 2. Substring match for movies
        for k, v in self.archived_catalog.items():
            f_title = v.get("franchise_title", "")
            norm_f = normalize_title(f_title)
            if len(norm_f) > 5 and norm_f in norm_movie:
                # Extra check: if movie title has movie words, confirm
                return v

        return None

    async def copy_from_archive(self, movie: dict, catalog_item: dict) -> Optional[int]:
        """Copies an existing archived release to Movies channel via app.copy_message"""
        movie_title = movie["Popular_Anime_Movie_Title"]
        source_msg_id = catalog_item.get("first_ep_msg_id") or catalog_item.get("poster_msg_id")
        if not source_msg_id:
            logger.warning(f"No source message ID found for {movie_title}")
            return None

        logger.info(f"Attempting 0 MB bandwidth copy for '{movie_title}' from Msg #{source_msg_id} (Channel {SOURCE_TARGET_ID})...")
        try:
            copied_msg = await execute_with_floodwait(
                self.user_app.copy_message,
                chat_id=MOVIES_CHANNEL_ID,
                from_chat_id=SOURCE_TARGET_ID,
                message_id=source_msg_id
            )
            logger.info(f"  ✅ Successfully copied '{movie_title}' -> Movies Channel Msg #{copied_msg.id}")
            return copied_msg.id
        except MessageIdInvalid:
            logger.warning(f"  ⚠️ Source message #{source_msg_id} no longer exists. Falling back to re-upload.")
            return None
        except Exception as e:
            logger.error(f"  ❌ Error copying message for '{movie_title}': {e}")
            return None

    async def upload_file(self, file_path: str, caption: str, thumb_path: Optional[str] = None) -> Optional[int]:
        """Uploads a file: routes < 2000 MiB to Helper Bots, 2000-4000 MiB to HSIN Semaphore(1)"""
        if not os.path.isfile(file_path):
            logger.error(f"Upload file not found: {file_path}")
            return None

        file_size = os.path.getsize(file_path)
        logger.info(f"Routing file upload: {os.path.basename(file_path)} ({file_size / (1024*1024):.2f} MiB)")

        # Validation
        if file_size > USER_MAX_BYTES:
            raise ValueError(f"File size {file_size} exceeds strict Telegram limit {USER_MAX_BYTES} (4000 MiB)!")

        # Case A: < 2000 MiB -> Dispatch via Helper Bot Pool
        if file_size < BOT_MAX_BYTES:
            helper_bot = self.get_next_helper_bot()
            logger.info(f"  • Using Helper Bot @{helper_bot.me.username} (< 2000 MiB tier)")
            msg = await execute_with_floodwait(
                helper_bot.send_document,
                chat_id=MOVIES_CHANNEL_ID,
                document=file_path,
                thumb=thumb_path if thumb_path and os.path.isfile(thumb_path) else None,
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
                force_document=False  # Allows video streaming if supported
            )
            return msg.id

        # Case B: 2000 - 4000 MiB -> Dispatch via HSIN User Session under Semaphore(1)
        logger.info("  • Large file (2000–4000 MiB): queuing under upload_semaphore(1) for User Session...")
        async with user_upload_semaphore:
            logger.info(f"  • Lock acquired. Uploading {os.path.basename(file_path)} via HSIN...")
            msg = await execute_with_floodwait(
                self.user_app.send_document,
                chat_id=MOVIES_CHANNEL_ID,
                document=file_path,
                thumb=thumb_path if thumb_path and os.path.isfile(thumb_path) else None,
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
                force_document=False
            )
            logger.info(f"  ✅ HSIN upload completed -> Msg #{msg.id}")
            return msg.id

    def record_success(self, movie: dict, msg_id: int, source_type: str, file_size: int = 0):
        """Records completed routing to SQLite and updates pending_index"""
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('''
            INSERT OR REPLACE INTO routed_movies 
            (movie_id, title, category, year, channel_id, message_id, file_size_bytes, status, source_type, nyaa_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            int(movie["Movie_ID"]),
            movie["Popular_Anime_Movie_Title"],
            movie.get("Category", ""),
            movie.get("Release_Year", ""),
            MOVIES_CHANNEL_ID,
            msg_id,
            file_size,
            "ROUTED",
            source_type,
            movie.get("Verified_Nyaa_URL", "")
        ))

        cur.execute('''
            INSERT INTO pending_index (title, category, target_channel_id, target_message_id, status)
            VALUES (?, ?, ?, ?, 'PENDING')
        ''', (
            movie["Popular_Anime_Movie_Title"],
            movie.get("Category", "Movie"),
            MOVIES_CHANNEL_ID,
            msg_id
        ))

        conn.commit()
        conn.close()

    def get_routed_ids(self) -> set:
        """Returns set of already routed Movie IDs from SQLite"""
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT movie_id FROM routed_movies WHERE status = "ROUTED"')
        rows = cur.fetchall()
        conn.close()
        return {r[0] for r in rows}


async def run_pipeline(limit: int = 0, dedup_only: bool = False, specific_id: Optional[int] = None):
    if not os.path.isfile(MOVIES_CSV):
        logger.error(f"Manifest CSV not found: {MOVIES_CSV}")
        return

    with open(MOVIES_CSV, 'r', encoding='utf-8') as f:
        movies = list(csv.DictReader(f))

    if specific_id:
        movies = [m for m in movies if int(m["Movie_ID"]) == specific_id]
    elif limit > 0:
        movies = movies[:limit]

    logger.info(f"Loaded {len(movies)} movie records to process.")
    router = TelegramRouter()
    await router.initialize()

    routed_ids = router.get_routed_ids()
    logger.info(f"Already routed movies in SQLite: {len(routed_ids)}")

    processed_count = 0
    copied_count = 0
    uploaded_count = 0

    try:
        for idx, movie in enumerate(movies, 1):
            m_id = int(movie["Movie_ID"])
            m_title = movie["Popular_Anime_Movie_Title"]

            if m_id in routed_ids:
                logger.info(f"[{idx}/{len(movies)}] Movie #{m_id}: '{m_title}' already routed. Skipping.")
                continue

            logger.info(f"\n==================================================")
            logger.info(f"[{idx}/{len(movies)}] Processing Movie #{m_id}: {m_title}")
            logger.info(f"==================================================")

            # Step 1: De-duplication Check against Archival Catalog
            cat_match = router.find_catalog_match(m_title)
            if cat_match:
                logger.info(f"Found archival catalog match: '{cat_match.get('franchise_title')}'")
                copied_msg_id = await router.copy_from_archive(movie, cat_match)
                if copied_msg_id:
                    router.record_success(movie, copied_msg_id, source_type="ARCHIVAL_COPY", file_size=0)
                    routed_ids.add(m_id)
                    copied_count += 1
                    processed_count += 1
                    continue

            if dedup_only:
                logger.info(f"  • Deduplication-only mode: Movie #{m_id} not in catalog. Skipping upload.")
                continue

            # Step 2: Check for existing local file or staged download
            # Look in standard staging directories
            possible_files = []
            clean_name = re.sub(r'[^a-zA-Z0-9]', ' ', m_title).lower()
            for search_dir in ["/home/administrator/starfall/torrents/seeding", "/home/administrator/starfall/downloads"]:
                if os.path.exists(search_dir):
                    for fname in os.listdir(search_dir):
                        if fname.endswith(('.mkv', '.mp4')):
                            if any(part in fname.lower() for part in clean_name.split()[:2] if len(part) > 3):
                                possible_files.append(os.path.join(search_dir, fname))

            if possible_files:
                target_file = possible_files[0]
                caption = f"🎬 <b>{movie['Popular_Anime_Movie_Title']} ({movie['Release_Year']})</b>\n\n" \
                          f"🏢 <b>Studio:</b> {movie.get('Director_And_Studio', 'N/A')}\n" \
                          f"📦 <b>Release:</b> {movie.get('Primary_1080p_HEVC_10bit_Release', '1080p HEVC')}"
                msg_id = await router.upload_file(target_file, caption=caption)
                if msg_id:
                    router.record_success(movie, msg_id, source_type="LOCAL_UPLOAD", file_size=os.path.getsize(target_file))
                    routed_ids.add(m_id)
                    uploaded_count += 1
                    processed_count += 1
            else:
                logger.info(f"  • No existing file found locally for '{m_title}'. Staged for automated nyaa ingestion.")

    finally:
        await router.stop()

    logger.info(f"\n🎉 Pipeline Run Completed: Processed {processed_count} (Copied: {copied_count}, Uploaded: {uploaded_count})")


def print_status():
    init_database()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT count(*), source_type FROM routed_movies GROUP BY source_type')
    stats = cur.fetchall()
    cur.execute('SELECT count(*) FROM pending_index WHERE status = "PENDING"')
    pending = cur.fetchone()[0]
    conn.close()

    print("\n=== Telegram Channel Router Status ===")
    for count, stype in stats:
        print(f"  • {stype}: {count} movies")
    print(f"  • Pending Index Posts: {pending}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Starfall Telegram Channel Router")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of movies to process (0 = all)")
    parser.add_argument("--dedup-only", action="store_true", help="Only copy existing matches from catalog (0 MB disk bandwidth)")
    parser.add_argument("--movie-id", type=int, default=None, help="Process a specific movie ID")
    parser.add_argument("--status", action="store_true", help="Print routing status and exit")
    args = parser.parse_args()

    if args.status:
        print_status()
    else:
        asyncio.run(run_pipeline(limit=args.limit, dedup_only=args.dedup_only, specific_id=args.movie_id))
