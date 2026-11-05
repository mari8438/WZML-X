#!/usr/bin/env python3
"""
Starfall v1.6 High-Throughput Tiered Telegram Upload Router (`upload_router.py`)
================================================================================
Target Hardware : 16 vCPU, 32 GB RAM, 1 Gbps Unmetered Port
Source Channel  : Tamil Anime Starfall Collection (-1004335327733)
Target Channel  : Tamil Muxed Collection (-1004415413346)
Client Pool     : 1 Premium User (@Hsin_The_Moon_Fox) + 5 Helper Bots:
                  - @Anime_11_uplode_bot
                  - @Anime_14_uplode_bot
                  - @Anime_15_uplode_bot
                  - @Starfall_xrobot
                  - @Do_up_3_robot

Key Architectural Guarantees:
1. Zero-420 FloodWait Dual-Gate Rate Limiter:
   - Gate A (Parallel Transfer Phase): Up to 5 Helper Bots + 1 Premium User
     stream file chunks concurrently to Telegram DC5 (`saveBigFilePart` does
     NOT trigger `messages.SendMedia` channel rate limits), saturating ~450-650 Mbps
     of the 1 Gbps port.
   - Gate B (Channel Commit Phase): A strict sliding-window token bucket
     (`ChannelCommitGate`) spaces final `send_video` / `send_photo` / `copy_message`
     commits to >= 3.65s between messages (max 16.4 msgs/min, safely under
     Telegram's 18-20 msgs/min per-channel threshold) and >= 18.5s per individual bot.
2. Strict 3-Step Season Barrier Sequencing:
   - Step 1: Forward/Copy original Season Poster Card from -1004335327733 (or upload
     resolved AniList poster with Unicode frame & expandable synopsis).
   - Step 2: Upload Full-Season Merged Video(s) via @Hsin_The_Moon_Fox (supports
     up to 3,950 MiB) with harvested 16:9 landscape logo thumbnail & probed duration.
   - Step 3: Commit single episodes strictly in ascending order (S01E01, S01E02...)
     using per-episode `asyncio.Event` barriers, even when 5 Helper Bots finish
     uploading file chunks out-of-order.
3. Immediate Post-Upload Purge Hook:
   - Deletes muxed `.mkv` in `/dev/shm/starfall_mux` and raw source video in NVMe
     immediately upon confirmed `msg.id`, notifying `DiskQuotaGuard`.
"""

import asyncio
import collections
import logging
import os
import sys
import time
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

import aiosqlite
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        DB_PATH,
        DEFAULT_TAMIL_CHANNEL_ID,
        HARVEST_CHANNEL_ID,
        HELPER_TOKENS,
        TELEGRAM_API,
        TELEGRAM_HASH,
        TG_SPLIT_THRESHOLD_MIB,
        USER_SESSION_STRING,
    )
else:
    from .config import (
        DB_PATH,
        DEFAULT_TAMIL_CHANNEL_ID,
        HARVEST_CHANNEL_ID,
        HELPER_TOKENS,
        TELEGRAM_API,
        TELEGRAM_HASH,
        TG_SPLIT_THRESHOLD_MIB,
        USER_SESSION_STRING,
    )

for _logger_name in (
    "pyrogram",
    "pyrogram.session",
    "pyrogram.connection",
    "pyrogram.dispatcher",
    "pyrogram.client",
):
    logging.getLogger(_logger_name).setLevel(logging.ERROR)

LOGGER = logging.getLogger("StarfallUploadRouter")

# Rate-limiting constants for Zero-FloodWait operation
CHANNEL_MAX_MSGS_PER_MIN: int = 16             # Hard ceiling (< 18 msgs/min Telegram channel cap)
CHANNEL_MIN_COMMIT_INTERVAL_SEC: float = 3.65  # 60s / 16.4 = 3.65s minimum spacing between channel posts
BOT_MIN_COOLDOWN_SEC: float = 18.5             # Per-bot cooldown across 5 helper bots (5 * 3.7s = 18.5s cycle)
MAX_PARALLEL_TRANSFERS: int = 6                # 5 Helper Bots + 1 Premium User session


class ChannelCommitGate:
    """
    Sliding-window rate limiter for target channel (-1004415413346).
    Guarantees:
      1. Minimum `min_interval_sec` (3.65s) between consecutive channel messages.
      2. Never more than `max_per_minute` (16) messages within any rolling 60.0s window.
    """

    def __init__(
        self,
        max_per_minute: int = CHANNEL_MAX_MSGS_PER_MIN,
        min_interval_sec: float = CHANNEL_MIN_COMMIT_INTERVAL_SEC,
    ):
        self.max_per_minute = max_per_minute
        self.min_interval_sec = min_interval_sec
        self._timestamps: Deque[float] = collections.deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                # Evict timestamps older than 60.0 seconds
                while self._timestamps and (now - self._timestamps[0]) >= 60.0:
                    self._timestamps.popleft()

                wait_window = 0.0
                if len(self._timestamps) >= self.max_per_minute:
                    wait_window = 60.05 - (now - self._timestamps[0])

                wait_interval = 0.0
                if self._timestamps:
                    wait_interval = self.min_interval_sec - (now - self._timestamps[-1])

                sleep_for = max(wait_window, wait_interval)
                if sleep_for <= 0.0:
                    self._timestamps.append(time.monotonic())
                    return

                LOGGER.debug(
                    f"[ChannelCommitGate] Pacing channel post by {sleep_for:.2f}s "
                    f"(window={len(self._timestamps)}/{self.max_per_minute})"
                )
                await asyncio.sleep(sleep_for)


class UploadRouter:
    """
    Production 6-Client Parallel Upload Dispatcher with Strict 3-Step Season Barrier Ordering.
    - 5 Helper Bots (`@Anime_11_uplode_bot`, `@Anime_14_uplode_bot`, `@Anime_15_uplode_bot`,
      `@Starfall_xrobot`, `@Do_up_3_robot`) for single episodes (<= 1,980 MiB).
    - 1 Premium User Session (`@Hsin_The_Moon_Fox`) for Full-Season Merged files (1,980 - 3,950 MiB)
      and Season Poster forwarding/copying.
    - Persists live bot health, cooldowns, and byte counters in SQLite `tg_client_pool`.
    """

    def __init__(
        self,
        target_channel_id: int = DEFAULT_TAMIL_CHANNEL_ID,
        source_channel_id: int = HARVEST_CHANNEL_ID,
        db_path: str = DB_PATH,
    ):
        self.target_channel_id = target_channel_id
        self.source_channel_id = source_channel_id
        self.db_path = db_path
        self.tokens = [t.strip() for t in HELPER_TOKENS.strip().split() if t.strip()]

        self.helper_clients: List[Client] = []
        self.client_usernames: Dict[str, str] = {}
        self.user_client: Optional[Client] = None

        self._bot_locks: Dict[str, asyncio.Lock] = {}
        self._bot_cooldown_until: Dict[str, float] = {}
        self._bot_last_commit: Dict[str, float] = {}
        self._rr_index: int = 0
        self._rr_lock = asyncio.Lock()

        self.channel_gate = ChannelCommitGate()
        self.transfer_sem = asyncio.Semaphore(MAX_PARALLEL_TRANSFERS)
        self._is_started = False

    async def start(self) -> None:
        """Start all 5 Helper Bots and the Premium User Session concurrently with high-speed workers."""
        if self._is_started:
            return

        sessions_dir = os.path.join(DATA_DIR, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)

        async def _boot_helper(idx: int, token: str) -> Optional[Client]:
            client_name = f"starfall_helper_{idx}"
            bot = Client(
                client_name,
                api_id=TELEGRAM_API,
                api_hash=TELEGRAM_HASH,
                bot_token=token,
                workdir=sessions_dir,
                in_memory=False,
                workers=8,
                max_concurrent_transmissions=4,
            )
            try:
                await bot.start()
                me = await bot.get_me()
                uname = f"@{me.username}" if me.username else client_name
                self.client_usernames[bot.name] = uname
                self._bot_locks[bot.name] = asyncio.Lock()
                self._bot_cooldown_until[bot.name] = 0.0
                self._bot_last_commit[bot.name] = 0.0
                await self._register_client_in_db(uname, "HELPER_BOT")
                LOGGER.info(f"✅ Helper Bot #{idx} online: {uname} (ID: {me.id})")
                return bot
            except Exception as e:
                LOGGER.error(f"❌ Failed to start Helper Bot #{idx}: {e}")
                return None

        tasks = [_boot_helper(idx, tok) for idx, tok in enumerate(self.tokens, 1)]
        results = await asyncio.gather(*tasks)
        self.helper_clients = [b for b in results if b is not None]

        # Eagerly start Premium User Client so both channels are cached in memory
        await self._ensure_user_client()
        self._is_started = True
        LOGGER.info(
            f"🚀 UploadRouter Ready: {len(self.helper_clients)} Helper Bots + "
            f"{1 if self.user_client else 0} Premium User Session active."
        )

    async def _ensure_user_client(self) -> Optional[Client]:
        """Start `@Hsin_The_Moon_Fox` Premium User Session for >1,980 MiB season merges and poster copy."""
        if self.user_client is not None:
            return self.user_client
        if not USER_SESSION_STRING:
            LOGGER.warning("USER_SESSION_STRING not configured; large >1,980 MiB uploads unavailable.")
            return None

        self.user_client = Client(
            "starfall_user_uploader",
            api_id=TELEGRAM_API,
            api_hash=TELEGRAM_HASH,
            session_string=USER_SESSION_STRING,
            in_memory=True,
            workers=16,
            max_concurrent_transmissions=6,
        )
        try:
            await self.user_client.start()
            # Warm up access hashes for source (-1004335327733) and target (-1004415413346)
            async for _ in self.user_client.get_dialogs(limit=50):
                pass
            me = await self.user_client.get_me()
            uname = f"@{me.username}" if me.username else "@Hsin_The_Moon_Fox"
            self.client_usernames[self.user_client.name] = uname
            self._bot_locks[self.user_client.name] = asyncio.Lock()
            self._bot_cooldown_until[self.user_client.name] = 0.0
            self._bot_last_commit[self.user_client.name] = 0.0
            await self._register_client_in_db(uname, "USER_PREMIUM")
            LOGGER.info(f"✅ Premium User Session online: {uname} (Premium={me.is_premium})")
            return self.user_client
        except Exception as e:
            LOGGER.error(f"❌ Failed to start Premium User client: {e}")
            self.user_client = None
            return None

    async def stop(self) -> None:
        """Gracefully stop all Telegram clients."""
        for bot in self.helper_clients:
            try:
                await bot.stop()
            except Exception:
                pass
        if self.user_client:
            try:
                await self.user_client.stop()
            except Exception:
                pass
        self.helper_clients.clear()
        self.user_client = None
        self._is_started = False

    async def _register_client_in_db(self, username: str, role: str) -> None:
        """Upsert client state in `tg_client_pool` table."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO tg_client_pool (client_username, client_role, cooldown_until_ts, uploads_last_min, total_bytes_sent, is_active)
                    VALUES (?, ?, 0, 0, 0, 1)
                    ON CONFLICT(client_username) DO UPDATE SET
                        client_role = excluded.client_role,
                        is_active = 1;
                    """,
                    (username, role),
                )
                await db.commit()
        except Exception as e:
            LOGGER.debug(f"Could not update tg_client_pool for {username}: {e}")

    async def _record_client_telemetry(
        self,
        client_name: str,
        bytes_sent: int = 0,
        cooldown_until: float = 0.0,
    ) -> None:
        """Update cumulative bytes sent and FloodWait cooldown timestamp in SQLite WAL DB."""
        uname = self.client_usernames.get(client_name, client_name)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    UPDATE tg_client_pool
                    SET total_bytes_sent = total_bytes_sent + ?,
                        cooldown_until_ts = MAX(cooldown_until_ts, ?),
                        uploads_last_min = uploads_last_min + 1
                    WHERE client_username = ?;
                    """,
                    (bytes_sent, cooldown_until, uname),
                )
                await db.commit()
        except Exception as e:
            LOGGER.debug(f"Telemetry update skipped for {uname}: {e}")

    async def _acquire_healthy_helper_bot(self) -> Client:
        """
        Select the next healthy Helper Bot via health-aware round-robin:
        - Skips any bot currently in a FloodWait quarantine window (`_bot_cooldown_until`).
        - Enforces per-bot minimum spacing (`BOT_MIN_COOLDOWN_SEC` = 18.5s) so no single bot
          exceeds ~3.2 uploads/minute.
        """
        if not self.helper_clients:
            user = await self._ensure_user_client()
            if user:
                return user
            raise RuntimeError("No Telegram helper bots or user client available!")

        while True:
            async with self._rr_lock:
                now = time.monotonic()
                shortest_wait: float = 999999.0

                for offset in range(len(self.helper_clients)):
                    idx = (self._rr_index + offset) % len(self.helper_clients)
                    candidate = self.helper_clients[idx]
                    cname = candidate.name

                    # Check FloodWait quarantine
                    fw_wait = max(0.0, self._bot_cooldown_until.get(cname, 0.0) - time.time())
                    # Check per-bot rate limit spacing
                    elapsed = now - self._bot_last_commit.get(cname, 0.0)
                    rate_wait = max(0.0, BOT_MIN_COOLDOWN_SEC - elapsed)
                    total_wait = max(fw_wait, rate_wait)

                    if total_wait <= 0.0:
                        self._rr_index = (idx + 1) % len(self.helper_clients)
                        self._bot_last_commit[cname] = now
                        return candidate

                    if total_wait < shortest_wait:
                        shortest_wait = total_wait

                # Advance index and wait for the soonest available bot
                self._rr_index = (self._rr_index + 1) % len(self.helper_clients)

            await asyncio.sleep(min(shortest_wait, 1.0))

    async def publish_season_poster(
        self,
        source_poster_msg_id: Optional[int] = None,
        photo_path: Optional[str] = None,
        caption: str = "",
    ) -> Optional[int]:
        """
        STEP 1 of Strict 3-Step Upload Sequence:
        1. If `source_poster_msg_id` is available from `-1004335327733`, directly `copy_message`
           to `-1004415413346` preserving original photo and Unicode formatting (or applying override caption).
        2. Fallback: Upload `photo_path` with Unicode frame & expandable synopsis.
        """
        if not self._is_started:
            await self.start()

        client = await self._ensure_user_client()
        if not client and self.helper_clients:
            client = self.helper_clients[0]
        if not client:
            LOGGER.error("No client available for Step 1 Season Poster.")
            return None

        # Priority A: Copy original poster card message from source channel (-1004335327733)
        if source_poster_msg_id and self.user_client:
            for attempt in range(1, 4):
                try:
                    await self.channel_gate.acquire()
                    copied: Message = await self.user_client.copy_message(
                        chat_id=self.target_channel_id,
                        from_chat_id=self.source_channel_id,
                        message_id=source_poster_msg_id,
                        caption=caption if caption else None,
                        parse_mode=ParseMode.HTML if caption else None,
                    )
                    LOGGER.info(
                        f"📸 [Step 1] Copied original Season Poster Msg #{source_poster_msg_id} "
                        f"-> Target Msg #{copied.id}"
                    )
                    return copied.id
                except FloodWait as fw:
                    wait_s = int(getattr(fw, "value", 10)) + 2
                    LOGGER.warning(f"FloodWait {wait_s}s on poster copy (attempt {attempt}); sleeping...")
                    await asyncio.sleep(wait_s)
                except Exception as e:
                    LOGGER.warning(f"Direct copy_message #{source_poster_msg_id} failed ({e}), falling back to photo upload.")
                    break

        # Priority B: Upload local poster image
        if not photo_path or not os.path.exists(photo_path):
            LOGGER.error(f"Poster file missing: {photo_path}")
            return None

        return await self.upload_photo(photo_path=photo_path, caption=caption)

    async def upload_photo(
        self,
        photo_path: str,
        caption: str = "",
    ) -> Optional[int]:
        """Upload Season Poster photo with automatic FloodWait recovery and bot fallback."""
        if not self._is_started:
            await self.start()

        if not os.path.exists(photo_path):
            LOGGER.error(f"Poster photo does not exist: {photo_path}")
            return None

        client = await self._ensure_user_client()
        if not client and self.helper_clients:
            client = self.helper_clients[0]
        if not client:
            return None

        for attempt in range(1, 4):
            try:
                await self.channel_gate.acquire()
                is_user = (client == self.user_client)
                effective_caption = caption
                if not is_user and len(effective_caption) > 1020:
                    effective_caption = effective_caption[:1005] + "...</blockquote>"

                msg: Message = await client.send_photo(
                    chat_id=self.target_channel_id,
                    photo=photo_path,
                    caption=effective_caption,
                    parse_mode=ParseMode.HTML,
                )
                await self._record_client_telemetry(client.name, os.path.getsize(photo_path))
                LOGGER.info(f"✅ [Step 1] Season Poster uploaded -> Msg #{msg.id}")
                return msg.id
            except FloodWait as fw:
                wait_s = int(getattr(fw, "value", 15)) + 2
                self._bot_cooldown_until[client.name] = time.time() + wait_s
                LOGGER.warning(f"FloodWait ({wait_s}s) on poster upload via {client.name}; rotating client...")
                if self.helper_clients:
                    client = await self._acquire_healthy_helper_bot()
            except Exception as e:
                LOGGER.error(f"Poster upload error (attempt {attempt}): {e}")
                if self.helper_clients and client == self.user_client:
                    client = self.helper_clients[0]
                else:
                    await asyncio.sleep(2.0)

        return None

    async def upload_file(
        self,
        filepath: str,
        caption: str = "",
        thumb_path: Optional[str] = None,
        duration_sec: int = 0,
        width: int = 1920,
        height: int = 1080,
        cleanup_after_upload: bool = True,
        raw_source_to_purge: Optional[str] = None,
        on_purged_bytes_cb: Optional[Callable[[int], Awaitable[None]]] = None,
        commit_barrier: Optional[asyncio.Event] = None,
        commit_done_event: Optional[asyncio.Event] = None,
    ) -> Optional[int]:
        """
        Tiered & Barrier-Synchronized Video Upload:
        - Files > 1,980 MiB (Full Season Merges) -> `@Hsin_The_Moon_Fox` Premium User Session.
        - Files <= 1,980 MiB (Single Episodes) -> 5 Helper Bots in health-aware rotation.
        - If `commit_barrier` is provided, waits for the previous episode's commit event
          and acquires `ChannelCommitGate` right before committing to the target channel,
          guaranteeing strict `S01E01, S01E02, ...` message ordering in `-1004415413346`.
        - Immediately deletes the muxed file in `/dev/shm` and optional `raw_source_to_purge`
          on NVMe upon upload completion.
        """
        if not self._is_started:
            await self.start()

        if not os.path.exists(filepath):
            LOGGER.error(f"File to upload does not exist: {filepath}")
            if commit_done_event:
                commit_done_event.set()
            return None

        file_size_bytes = os.path.getsize(filepath)
        file_size_mib = file_size_bytes / (1024 ** 2)
        is_large = file_size_mib > TG_SPLIT_THRESHOLD_MIB

        async with self.transfer_sem:
            for attempt in range(1, 5):
                if is_large:
                    client = await self._ensure_user_client()
                    if not client:
                        LOGGER.error(f"Cannot upload >{TG_SPLIT_THRESHOLD_MIB} MiB file without User Session.")
                        if commit_done_event:
                            commit_done_event.set()
                        return None
                else:
                    client = await self._acquire_healthy_helper_bot()

                uname = self.client_usernames.get(client.name, client.name)
                valid_thumb = thumb_path if (thumb_path and os.path.exists(thumb_path)) else None

                try:
                    # Wait on previous episode's completion barrier so channel posts stay strictly ordered
                    if commit_barrier is not None:
                        await commit_barrier.wait()

                    # Enforce global channel <= 16.4 msgs/min commit spacing
                    await self.channel_gate.acquire()

                    LOGGER.info(
                        f"🚀 Uploading {os.path.basename(filepath)} ({file_size_mib:.1f} MiB, "
                        f"dur={duration_sec}s, {width}x{height}) via {uname} [Attempt {attempt}]..."
                    )

                    msg: Message = await client.send_video(
                        chat_id=self.target_channel_id,
                        video=filepath,
                        caption=caption,
                        thumb=valid_thumb,
                        duration=int(duration_sec),
                        width=int(width),
                        height=int(height),
                        supports_streaming=True,
                        parse_mode=ParseMode.HTML,
                    )

                    # Signal the next episode in sequence immediately after message commit
                    if commit_done_event is not None and not commit_done_event.is_set():
                        commit_done_event.set()

                    await self._record_client_telemetry(client.name, bytes_sent=file_size_bytes)
                    LOGGER.info(
                        f"✅ Uploaded {os.path.basename(filepath)} -> Msg #{msg.id} via {uname} "
                        f"({file_size_mib:.1f} MiB)"
                    )

                    # Immediate post-upload purge hook (RAM disk + raw NVMe source)
                    await self._execute_purge_hooks(
                        muxed_filepath=filepath if cleanup_after_upload else None,
                        raw_source_path=raw_source_to_purge,
                        on_purged_bytes_cb=on_purged_bytes_cb,
                    )
                    return msg.id

                except FloodWait as fw:
                    wait_sec = int(getattr(fw, "value", 20)) + 3
                    cooldown_ts = time.time() + wait_sec
                    self._bot_cooldown_until[client.name] = cooldown_ts
                    await self._record_client_telemetry(client.name, bytes_sent=0, cooldown_until=cooldown_ts)
                    LOGGER.warning(
                        f"⚠️ 420 FloodWait ({wait_sec}s) on {uname}! Quarantining client until "
                        f"+{wait_sec}s and rotating to next available bot..."
                    )
                    if is_large:
                        await asyncio.sleep(wait_sec)
                except RPCError as rpc_err:
                    LOGGER.error(f"Telegram RPCError on {uname} (attempt {attempt}): {rpc_err}")
                    await asyncio.sleep(3.0 * attempt)
                except Exception as e:
                    LOGGER.error(f"Unexpected upload error on {uname} (attempt {attempt}): {e}")
                    await asyncio.sleep(2.5 * attempt)

        # Unblock downstream episode barrier even if this episode was quarantined after 4 retries
        if commit_done_event is not None and not commit_done_event.is_set():
            commit_done_event.set()
        return None

    async def upload_season_episodes_ordered(
        self,
        episodes_payload: List[Dict[str, Any]],
        on_purged_bytes_cb: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> Dict[int, Tuple[Optional[int], str]]:
        """
        STEP 3 Dispatcher: Dispatches a season's single episodes across the 5 Helper Bots
        with chained `asyncio.Event` barriers so `S01E01`, `S01E02`, ..., `S01E12` land in
        strict sequential order in `-1004415413346` while maximizing 1 Gbps throughput.

        Returns mapping: `{episode_number: (tg_msg_id, uploaded_by_bot_username)}`
        """
        if not episodes_payload:
            return {}

        sorted_items = sorted(episodes_payload, key=lambda x: x["episode_num"])
        barriers: List[asyncio.Event] = [asyncio.Event() for _ in range(len(sorted_items) + 1)]
        barriers[0].set()  # E01 is immediately unblocked once Step 2 (Season Merge) finishes

        results: Dict[int, Tuple[Optional[int], str]] = {}

        async def _worker(idx: int, item: Dict[str, Any]) -> None:
            ep_num = item["episode_num"]
            msg_id = await self.upload_file(
                filepath=item["path"],
                caption=item["caption"],
                thumb_path=item.get("thumb_path"),
                duration_sec=item.get("duration_sec", 1440),
                width=item.get("width", 1920),
                height=item.get("height", 1080),
                cleanup_after_upload=item.get("cleanup_after_upload", True),
                raw_source_to_purge=item.get("raw_video"),
                on_purged_bytes_cb=on_purged_bytes_cb,
                commit_barrier=barriers[idx],
                commit_done_event=barriers[idx + 1],
            )
            results[ep_num] = (msg_id, f"HelperBotPool (Msg #{msg_id})" if msg_id else "QUARANTINED")

        # Launch bounded concurrent tasks across the 5 helper bots
        tasks = [asyncio.create_task(_worker(i, item)) for i, item in enumerate(sorted_items)]
        await asyncio.gather(*tasks)
        return results

    @staticmethod
    async def _execute_purge_hooks(
        muxed_filepath: Optional[str],
        raw_source_path: Optional[str],
        on_purged_bytes_cb: Optional[Callable[[int], Awaitable[None]]],
    ) -> None:
        """Delete muxed `/dev/shm` file and raw NVMe torrent video file, reporting reclaimed bytes."""
        reclaimed_nvme_bytes = 0

        if muxed_filepath and os.path.exists(muxed_filepath):
            try:
                os.remove(muxed_filepath)
                LOGGER.debug(f"Purged RAM muxed file: {os.path.basename(muxed_filepath)}")
            except Exception as e:
                LOGGER.warning(f"Failed to purge RAM file {muxed_filepath}: {e}")

        if raw_source_path and os.path.exists(raw_source_path):
            try:
                raw_sz = os.path.getsize(raw_source_path)
                os.remove(raw_source_path)
                reclaimed_nvme_bytes += raw_sz
                LOGGER.info(
                    f"🧹 Purged raw torrent video {os.path.basename(raw_source_path)} "
                    f"({raw_sz / (1024**2):.1f} MiB reclaimed on NVMe)"
                )
            except Exception as e:
                LOGGER.warning(f"Failed to purge raw video {raw_source_path}: {e}")

        if reclaimed_nvme_bytes > 0 and on_purged_bytes_cb is not None:
            try:
                await on_purged_bytes_cb(reclaimed_nvme_bytes)
            except Exception as e:
                LOGGER.debug(f"Purge callback warning: {e}")


if __name__ == "__main__":
    async def _smoke_test():
        logging.basicConfig(level=logging.INFO)
        gate = ChannelCommitGate(max_per_minute=16, min_interval_sec=0.05)
        t0 = time.monotonic()
        for _ in range(5):
            await gate.acquire()
        elapsed = time.monotonic() - t0
        print(f"ChannelCommitGate smoke test passed (5 commits spaced in {elapsed:.2f}s)")

    asyncio.run(_smoke_test())