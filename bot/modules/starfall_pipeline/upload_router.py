import asyncio
import logging
import os
import sys
import time
from typing import Dict, Any, List, Optional

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        TELEGRAM_API,
        TELEGRAM_HASH,
        HELPER_TOKENS,
        USER_SESSION_STRING,
        DEFAULT_TAMIL_CHANNEL_ID,
        TG_SPLIT_THRESHOLD_MIB,
    )
else:
    from .config import (
        TELEGRAM_API,
        TELEGRAM_HASH,
        HELPER_TOKENS,
        USER_SESSION_STRING,
        DEFAULT_TAMIL_CHANNEL_ID,
        TG_SPLIT_THRESHOLD_MIB,
    )

from pyrogram import Client, enums
from pyrogram.enums import ParseMode
from pyrogram.types import Message

# Suppress Pyrogram verbose logs
for _name in ("pyrogram", "pyrogram.session", "pyrogram.connection", "pyrogram.dispatcher", "pyrogram.client"):
    logging.getLogger(_name).setLevel(logging.ERROR)

LOGGER = logging.getLogger("StarfallUploadRouter")

class UploadRouter:
    """
    Tiered Telegram Uploader:
    - Files <= 1,980 MiB (Single Episodes): Round-robin across 5 Helper Bots with 3.5s cooldown.
    - Files > 1,980 MiB (Merged Seasons): Uploaded via Premium User Session (@Hsin_The_Moon_Fox).
    - Immediate local file cleanup to maintain storage quota < 450 GB.
    """

    def __init__(self, target_channel_id: int = DEFAULT_TAMIL_CHANNEL_ID):
        self.target_channel_id = target_channel_id
        self.tokens = [t.strip() for t in HELPER_TOKENS.strip().split() if t.strip()]
        self.helper_clients: List[Client] = []
        self.user_client: Optional[Client] = None
        self._current_bot_idx = 0
        self._last_upload_time: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._is_started = False

    async def start(self) -> None:
        """Start all helper bot clients concurrently."""
        if self._is_started:
            return

        async def _start_bot(idx: int, token: str):
            bot = Client(
                f"starfall_helper_{idx}",
                api_id=TELEGRAM_API,
                api_hash=TELEGRAM_HASH,
                bot_token=token,
                in_memory=True,
            )
            try:
                await bot.start()
                me = await bot.get_me()
                self._last_upload_time[bot.name] = 0.0
                LOGGER.info(f"Helper Bot #{idx} started: @{me.username} (ID: {me.id})")
                return bot
            except Exception as e:
                LOGGER.error(f"Failed to start helper bot #{idx}: {e}")
                return None

        # Start all helper bots concurrently
        bot_tasks = [_start_bot(idx, token) for idx, token in enumerate(self.tokens, 1)]
        results = await asyncio.gather(*bot_tasks)
        self.helper_clients = [b for b in results if b is not None]

        self._is_started = True

    async def _ensure_user_client(self) -> Client:
        """Lazy-start premium user client when needed for >1980 MiB files."""
        if self.user_client is None and USER_SESSION_STRING:
            self.user_client = Client(
                "starfall_user_uploader",
                api_id=TELEGRAM_API,
                api_hash=TELEGRAM_HASH,
                session_string=USER_SESSION_STRING,
                in_memory=True,
            )
            try:
                await self.user_client.start()
                async for _ in self.user_client.get_dialogs(limit=30):
                    pass
                me = await self.user_client.get_me()
                LOGGER.info(f"Premium User client started: @{me.username} (Premium: {me.is_premium})")
            except Exception as e:
                LOGGER.error(f"Failed to start user client: {e}")
        return self.user_client

    async def stop(self) -> None:
        """Stop all clients."""
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
        self._is_started = False

    async def _get_next_helper_bot(self) -> Client:
        """Select next helper bot in round-robin and respect 3.5s cooldown."""
        async with self._lock:
            bot = self.helper_clients[self._current_bot_idx]
            self._current_bot_idx = (self._current_bot_idx + 1) % len(self.helper_clients)

            last_time = self._last_upload_time.get(bot.name, 0.0)
            elapsed = time.time() - last_time
            if elapsed < 3.5:
                wait_sec = 3.5 - elapsed
                LOGGER.info(f"Enforcing 3.5s cooldown on {bot.name}: waiting {wait_sec:.2f}s")
                await asyncio.sleep(wait_sec)

            self._last_upload_time[bot.name] = time.time()
            return bot

    async def upload_photo(
        self,
        photo_path: str,
        caption: str = "",
    ) -> Optional[int]:
        """
        Upload season poster photo with Unicode frame caption to target channel.
        """
        if not self._is_started:
            await self.start()

        if not os.path.exists(photo_path):
            LOGGER.error(f"Poster photo does not exist: {photo_path}")
            return None

        # Prefer Premium User client for 2048-char caption support; fallback to helper bot
        client = await self._ensure_user_client()
        if not client and self.helper_clients:
            client = self.helper_clients[0]

        if not client:
            LOGGER.error("No available client to upload poster.")
            return None

        # Bot API hard limit is 1024 chars for captions
        is_bot = not getattr(client, "is_premium", False)
        if is_bot and len(caption) > 1020:
            LOGGER.warning(f"Truncating caption from {len(caption)} to 1020 chars for bot client")
            caption = caption[:1015] + "..."

        uploader_tag = getattr(client, "name", "Uploader")
        LOGGER.info(f"📸 Sending Season Poster via {uploader_tag} to {self.target_channel_id}...")
        try:
            msg: Message = await client.send_photo(
                chat_id=self.target_channel_id,
                photo=photo_path,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            LOGGER.info(f"✅ Season Poster sent! Msg ID: {msg.id}")
            return msg.id
        except Exception as e:
            LOGGER.error(f"Failed to send poster: {e}")
            # If failed, attempt fallback with truncated caption via helper bot
            if client != self.helper_clients[0] if self.helper_clients else False:
                try:
                    bot = self.helper_clients[0]
                    short_caption = caption[:1000] if len(caption) > 1000 else caption
                    LOGGER.info(f"Retrying poster via {bot.name} with truncated caption...")
                    msg = await bot.send_photo(
                        chat_id=self.target_channel_id,
                        photo=photo_path,
                        caption=short_caption,
                        parse_mode=ParseMode.HTML,
                    )
                    LOGGER.info(f"✅ Season Poster fallback sent! Msg ID: {msg.id}")
                    return msg.id
                except Exception as ex2:
                    LOGGER.error(f"Fallback poster upload also failed: {ex2}")
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
    ) -> Optional[int]:
        """
        Upload single file (video or document) with tiered routing:
        - <= 1,980 MiB -> 5 Helper Bots (Round-Robin)
        - > 1,980 MiB -> Premium User Session
        """
        if not self._is_started:
            await self.start()

        if not os.path.exists(filepath):
            LOGGER.error(f"File to upload does not exist: {filepath}")
            return None

        file_size_bytes = os.path.getsize(filepath)
        file_size_mib = file_size_bytes / (1024 ** 2)

        # Tiered client selection
        is_large = file_size_mib > TG_SPLIT_THRESHOLD_MIB
        if is_large:
            client = await self._ensure_user_client()
            if not client:
                LOGGER.error("Cannot upload >1980 MiB file: User premium client is not active")
                return None
            uploader_tag = "Premium User (@Hsin_The_Moon_Fox)"
        else:
            client = await self._get_next_helper_bot()
            uploader_tag = getattr(client, "name", "HelperBot")

        LOGGER.info(
            f"🚀 Uploading {os.path.basename(filepath)} ({file_size_mib:.1f} MiB) "
            f"via {uploader_tag} to channel {self.target_channel_id}..."
        )

        try:
            # Check thumb
            valid_thumb = thumb_path if (thumb_path and os.path.exists(thumb_path)) else None

            # Upload as video with streaming support
            msg: Message = await client.send_video(
                chat_id=self.target_channel_id,
                video=filepath,
                caption=caption,
                thumb=valid_thumb,
                duration=duration_sec,
                width=width,
                height=height,
                supports_streaming=True,
            )

            LOGGER.info(
                f"✅ Upload succeeded! Msg ID: {msg.id} via {uploader_tag} "
                f"({file_size_mib:.1f} MiB in channel {self.target_channel_id})"
            )

            # Quota cleanup: delete local output file immediately
            if cleanup_after_upload and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    LOGGER.info(f"Purged local uploaded file {os.path.basename(filepath)} to preserve quota.")
                except Exception as e:
                    LOGGER.warning(f"Failed to delete {filepath}: {e}")

            return msg.id

        except Exception as e:
            LOGGER.error(f"Upload failed via {uploader_tag} for {filepath}: {e}")
            return None

if __name__ == "__main__":
    async def _test():
        logging.basicConfig(level=logging.INFO)
        router = UploadRouter()
        await router.start()
        print("UploadRouter initialized successfully.")
        await router.stop()

    asyncio.run(_test())
