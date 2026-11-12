#!/usr/bin/env python3
"""
Starfall Exhaustive Metadata Posters & A-Z Master Indexer
Reads `manifests/the_ultimate_uncensored_anime_database_2026.csv` and:
- Posts rich posters to Index Channel (-1004480002635) with all columns formatted
- Rotates helper bots and User Session with global 1.5s rate-limit & FloodWait backoff
- Generates/updates the pinned A-Z directory navigation index with anchors & deep links
"""

import argparse
import asyncio
import csv
import html
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional

APP_ROOT = "/usr/src/app" if os.path.exists("/usr/src/app") else "/home/administrator/starfall"
sys.path.insert(0, APP_ROOT)
os.chdir(APP_ROOT)

from config import TELEGRAM_API, TELEGRAM_HASH, USER_SESSION_STRING, HELPER_TOKENS
from pyrogram import Client, enums
from pyrogram.errors import FloodWait, MessageNotModified

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] - %(message)s',
    datefmt='%d-%b-%y %I:%M:%S %p'
)
logger = logging.getLogger("AZIndexer")

INDEX_CHANNEL_ID = -1004480002635       # Anime Starfall Index [Tamil]
INDEX_USERNAME = "Anime_Starfall_Index_Tam"
MOVIES_CHANNEL_ID = -1004494800840      # Starfall Anime Movies
SOURCE_TARGET_ID = -1004415413346       # Target Dump

DB_PATH = os.path.join(APP_ROOT, "data/starfall_state.db")
UNCENSORED_CSV = os.path.join(APP_ROOT, "manifests/the_ultimate_uncensored_anime_database_2026.csv")
POSTERS_PROGRESS_JSON = os.path.join(APP_ROOT, "data/uncensored_posters_progress.json")


class GlobalRateLimiter:
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


async def execute_with_floodwait(coro_fn, *args, **kwargs):
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
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS uncensored_posters (
            rank TEXT PRIMARY KEY,
            title TEXT,
            romaji TEXT,
            year TEXT,
            studio TEXT,
            score REAL,
            poster_msg_id INTEGER,
            deep_link TEXT,
            target_msg_link TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS az_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def clean_text(val: Optional[str]) -> str:
    if not val:
        return "N/A"
    return html.escape(str(val).strip())


def strip_article(title: str) -> str:
    """Strips leading articles ('The ', 'A ', 'An ') for correct natural alphabetical ordering"""
    clean = str(title or "").strip()
    for prefix in ["The ", "A ", "An "]:
        if clean.lower().startswith(prefix.lower()):
            return clean[len(prefix):].strip()
    return clean


def format_poster_card(row: dict, target_msg_link: Optional[str] = None) -> str:
    """Formats rich poster according to master specification"""
    title = clean_text(row.get("Anime_Title"))
    romaji = clean_text(row.get("Alternative_Titles"))
    year = clean_text(row.get("Release_Year"))
    era = clean_text(row.get("Era"))
    studio = clean_text(row.get("Animation_Studio"))
    rank = clean_text(row.get("Rank"))
    score = clean_text(row.get("Overall_Weighted_Score_100"))
    censor = clean_text(row.get("Censorship_Level"))
    nudity_score = clean_text(row.get("Combined_Nudity_Score"))
    nudity_freq = clean_text(row.get("Frequency_Of_Nudity_25pct"))
    nudity_amt = clean_text(row.get("Amount_Of_Visible_Nudity_25pct"))
    official_release = clean_text(row.get("Official_Uncensored_Availability"))
    bd_info = clean_text(row.get("BluRay_DVD_Release_Info"))
    diffs = clean_text(row.get("TV_vs_Home_Video_Differences"))
    desc = clean_text(row.get("Content_Description"))

    nyaa_url = row.get("Nyaa_BD_Archive_URL", "").strip()
    ep_link = target_msg_link or (nyaa_url if nyaa_url else f"https://t.me/{INDEX_USERNAME}")

    caption = (
        f"🎬 <b>{title} ({year})</b>\n"
        f"🇯🇵 <b>Romaji:</b> <i>{romaji}</i>\n"
        f"🏢 <b>Studio:</b> {studio} | <b>Era:</b> {era}\n"
        f"⭐ <b>Rank:</b> <code>{rank}</code> | <b>Overall Score:</b> <code>{score}/100</code>\n"
        f"🔞 <b>Censorship Level:</b> {censor}\n"
        f"🔥 <b>Nudity Score:</b> <code>{nudity_score}/100</code> (Freq: {nudity_freq}%, Amount: {nudity_amt}%)\n"
        f"💿 <b>Official Release:</b> {official_release}\n"
        f"📦 <b>BD/DVD Info:</b> {bd_info}\n"
        f"📺 <b>Differences:</b> {diffs}\n\n"
        f"<blockquote expandable>📖 <b>Description:</b>\n{desc}</blockquote>\n\n"
        f"📥 <b>Direct Episode Files:</b> <a href=\"{ep_link}\"><b>[ 🎬 Click Here to View/Download ]</b></a>"
    )
    return caption


class IndexManager:
    def __init__(self):
        self.user_app: Optional[Client] = None
        self.helper_bots: List[Client] = []
        self.current_bot_idx = 0

    async def initialize(self):
        init_database()

        # Connect User Session (HSIN)
        self.user_app = Client(
            "index_user_session",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            session_string=USER_SESSION_STRING,
            in_memory=True
        )
        await self.user_app.start()
        me = await self.user_app.get_me()
        logger.info(f"Connected User Session: @{me.username} (ID: {me.id})")

        # Connect Helper Bots
        helper_token_list = HELPER_TOKENS.split()
        for idx, token in enumerate(helper_token_list, 1):
            try:
                bot = Client(
                    f"index_hbot_{idx}",
                    api_id=TELEGRAM_API,
                    api_hash=TELEGRAM_HASH,
                    bot_token=token,
                    in_memory=True
                )
                await bot.start()
                self.helper_bots.append(bot)
                logger.info(f"  • Helper Bot {idx} Active: @{bot.me.username}")
            except Exception as e:
                logger.warning(f"  • Helper Bot {idx} failed to start: {e}")

    async def stop(self):
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

    def get_sender(self) -> Client:
        """Rotates between helper bots and user session"""
        if self.helper_bots:
            bot = self.helper_bots[self.current_bot_idx % len(self.helper_bots)]
            self.current_bot_idx += 1
            return bot
        return self.user_app

    def get_posted_ranks(self) -> Dict[str, int]:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT rank, poster_msg_id FROM uncensored_posters')
        rows = cur.fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}

    def record_poster(self, rank: str, title: str, romaji: str, year: str, studio: str, score: float, msg_id: int, target_link: str):
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        deep_link = f"https://t.me/{INDEX_USERNAME}/{msg_id}"
        cur.execute('''
            INSERT OR REPLACE INTO uncensored_posters
            (rank, title, romaji, year, studio, score, poster_msg_id, deep_link, target_msg_link, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (rank, title, romaji, year, studio, score, msg_id, deep_link, target_link))
        conn.commit()
        conn.close()

    async def post_single_poster(self, row: dict) -> Optional[int]:
        sender = self.get_sender()
        rank = row.get("Rank", "").strip()
        title = row.get("Anime_Title", "").strip()

        # Check if episode files were routed to Starfall Anime Movies
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT message_id FROM routed_movies WHERE title LIKE ? LIMIT 1', (f"%{title}%",))
        m_row = cur.fetchone()
        conn.close()

        target_link = f"https://t.me/c/{str(MOVIES_CHANNEL_ID).replace('-100', '')}/{m_row[0]}" if m_row else None
        caption = format_poster_card(row, target_msg_link=target_link)

        logger.info(f"Posting Poster for [{rank}] '{title}' via @{sender.me.username}...")
        try:
            msg = await execute_with_floodwait(
                sender.send_message,
                chat_id=INDEX_CHANNEL_ID,
                text=caption,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )
            score = float(row.get("Overall_Weighted_Score_100", 0.0) or 0.0)
            self.record_poster(
                rank=rank,
                title=title,
                romaji=row.get("Alternative_Titles", ""),
                year=row.get("Release_Year", ""),
                studio=row.get("Animation_Studio", ""),
                score=score,
                msg_id=msg.id,
                target_link=target_link or ""
            )
            logger.info(f"  ✅ Poster posted -> Msg #{msg.id} (Link: https://t.me/{INDEX_USERNAME}/{msg.id})")
            return msg.id
        except Exception as e:
            logger.error(f"  ❌ Error posting poster for '{title}': {e}")
            return None

    async def update_pinned_az_directory(self):
        """Generates exhaustive alphabetical master directory (stripping articles) and publishes Instant View Telegraph Article"""
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT rank, title, year, score, poster_msg_id FROM uncensored_posters')
        items = cur.fetchall()
        cur.execute('SELECT value FROM az_index_meta WHERE key = "pinned_nav_msg_id"')
        pinned_row = cur.fetchone()
        conn.close()

        if not items:
            logger.info("No posted items found to build A-Z directory.")
            return

        logger.info(f"Building A-Z Master Directory for {len(items)} titles (article-aware ordering)...")

        # Sort items ignoring leading articles ("The ", "A ", "An ")
        sorted_items = sorted(items, key=lambda x: strip_article(x[1]).lower())

        # Group by first letter of stripped title
        groups = {}
        for rank, title, year, score, msg_id in sorted_items:
            sort_name = strip_article(title)
            first_char = sort_name[0].upper() if sort_name else "#"
            bucket = first_char if first_char.isalpha() else "#0_9"
            if bucket not in groups:
                groups[bucket] = []
            groups[bucket].append((rank, title, sort_name, year, score, msg_id))

        # 1. Publish Telegraph Instant View Article using user string / telegraph
        article_url = None
        try:
            from bot.helper.telegram_helper.tg_ui_formatter import publish_telegraph_article
            article_html = "<h3>🏛️ Starfall Uncensored & BD A–Z Master Index</h3>"
            article_html += f"<p><b>Total Catalog Releases:</b> {len(sorted_items)} | <b>Generated:</b> {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</p><hr/>"
            
            nav_anchors = []
            for b in sorted(groups.keys(), key=lambda x: (x != "#0_9", x)):
                nav_anchors.append(f"<a href='#{b.lstrip('#')}'>{b}</a>")
            article_html += f"<p><b>Alphabetical Quick Anchors:</b><br/>{' · '.join(nav_anchors)}</p><hr/>"

            for bucket in sorted(groups.keys(), key=lambda x: (x != "#0_9", x)):
                b_tag = bucket.lstrip('#')
                article_html += f"<h4 id='{b_tag}'>🏷️ {bucket}</h4><ul>"
                for rank, title, sort_name, year, score, msg_id in groups[bucket]:
                    link = f"https://t.me/{INDEX_USERNAME}/{msg_id}"
                    article_html += f"<li><b><a href='{link}'>{html.escape(title)}</a></b> ({year}) — <code>{rank}</code> [★ {score:.1f}/100]</li>"
                article_html += "</ul>"

            article_url = await publish_telegraph_article("Starfall A–Z Master Anime Index", article_html)
            logger.info(f"  📄 Published Instant View Telegraph Article: {article_url}")
        except Exception as tel_err:
            logger.warning(f"Note on Telegraph article generation: {tel_err}")

        # 2. Generate Telegram Channel Directory Messages
        article_banner = f"⚡ <a href=\"{article_url}\"><b>[ 📖 Open Full Instant View Article ]</b></a>\n\n" if article_url else ""
        header = (
            "🏛️ <b>★ STARFALL A–Z MASTER DIRECTORY ★</b> 🏛️\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Direct Links to Verified Uncensored & BD Archival Catalog</i>\n\n"
            f"{article_banner}"
            "📍 <b>Quick Index Anchors:</b>\n"
            "<code>#0_9 · #A · #B · #C · #D · #E · #F · #G · #H · #I · #J · #K · #L · #M\n"
            "#N · #O · #P · #Q · #R · #S · #T · #U · #V · #W · #X · #Y · #Z</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )

        sections = []
        current_section = header

        all_buckets = sorted(groups.keys(), key=lambda x: (x != "#0_9", x))
        for bucket in all_buckets:
            anchor_tag = f"#{bucket.lstrip('#')}"
            block = f"\n🏷️ <b>{anchor_tag}</b>\n"
            for rank, title, sort_name, year, score, msg_id in groups[bucket]:
                clean_t = html.escape(title)
                link = f"https://t.me/{INDEX_USERNAME}/{msg_id}"
                line = f"• <a href=\"{link}\"><b>{clean_t}</b></a> ({year}) — <code>{rank}</code> [★ {score:.1f}]\n"
                block += line

            if len(current_section) + len(block) > 3900:
                sections.append(current_section)
                current_section = f"🏛️ <b>★ STARFALL A–Z MASTER DIRECTORY (Cont.) ★</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━\n" + block
            else:
                current_section += block

        if current_section:
            sections.append(current_section)

        logger.info(f"Generated {len(sections)} directory sections. Updating channel via User Session...")

        posted_nav_ids = []
        for s_idx, sec_text in enumerate(sections, 1):
            msg = await execute_with_floodwait(
                self.user_app.send_message,
                chat_id=INDEX_CHANNEL_ID,
                text=sec_text,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True
            )
            posted_nav_ids.append(msg.id)
            logger.info(f"  • Posted Directory Section {s_idx}/{len(sections)} -> Msg #{msg.id}")

        # Pin the primary index header message
        if posted_nav_ids:
            primary_nav_id = posted_nav_ids[0]
            try:
                await execute_with_floodwait(
                    self.user_app.pin_chat_message,
                    chat_id=INDEX_CHANNEL_ID,
                    message_id=primary_nav_id,
                    both_sides=True
                )
                logger.info(f"📌 Successfully pinned Master Directory Header Msg #{primary_nav_id}!")
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute('INSERT OR REPLACE INTO az_index_meta (key, value) VALUES ("pinned_nav_msg_id", ?)', (str(primary_nav_id),))
                conn.commit()
                conn.close()
            except Exception as pin_err:
                logger.warning(f"Note on pinning directory: {pin_err}")


async def run_indexer(limit: int = 0, update_pinned_only: bool = False, specific_rank: Optional[str] = None):
    if not os.path.isfile(UNCENSORED_CSV):
        logger.error(f"Uncensored database not found at {UNCENSORED_CSV}")
        return

    with open(UNCENSORED_CSV, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    manager = IndexManager()
    await manager.initialize()

    try:
        if update_pinned_only:
            await manager.update_pinned_az_directory()
            return

        posted_ranks = manager.get_posted_ranks()
        logger.info(f"Already posted in Index Channel: {len(posted_ranks)} titles.")

        if specific_rank:
            rows = [r for r in rows if r.get("Rank", "").strip().upper() == specific_rank.strip().upper()]
        elif limit > 0:
            rows = rows[:limit]

        new_posts = 0
        for idx, row in enumerate(rows, 1):
            rank = row.get("Rank", "").strip()
            title = row.get("Anime_Title", "").strip()
            if rank in posted_ranks:
                logger.info(f"[{idx}/{len(rows)}] [{rank}] '{title}' already posted. Skipping.")
                continue

            logger.info(f"[{idx}/{len(rows)}] Processing [{rank}]: {title}...")
            msg_id = await manager.post_single_poster(row)
            if msg_id:
                new_posts += 1
                posted_ranks[rank] = msg_id

        logger.info(f"\nPosting complete! Posted {new_posts} new rich posters.")

        # Update Pinned Directory if new posters were added
        if new_posts > 0 or not manager.get_posted_ranks():
            await manager.update_pinned_az_directory()

    finally:
        await manager.stop()


def print_status():
    init_database()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT count(*), avg(score) FROM uncensored_posters')
    count, avg_score = cur.fetchone()
    cur.execute('SELECT value FROM az_index_meta WHERE key = "pinned_nav_msg_id"')
    p = cur.fetchone()
    conn.close()
    print("\n=== Exhaustive A-Z Index Status ===")
    print(f"  • Total Posters Published: {count}/100")
    print(f"  • Average Overall Score: {avg_score:.2f}" if avg_score else "  • Average Score: N/A")
    print(f"  • Pinned Master Navigation Message ID: {p[0] if p else 'None'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Starfall A-Z Index & Poster Generator")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of posters to generate (0 = all)")
    parser.add_argument("--rank", type=str, default=None, help="Process a specific Rank (e.g. #001)")
    parser.add_argument("--update-pinned", action="store_true", help="Only rebuild and pin the A-Z directory message")
    parser.add_argument("--status", action="store_true", help="Print current index status and exit")
    args = parser.parse_args()

    if args.status:
        print_status()
    else:
        asyncio.run(run_indexer(limit=args.limit, update_pinned_only=args.update_pinned, specific_rank=args.rank))
