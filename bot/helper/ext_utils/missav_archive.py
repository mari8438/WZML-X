import json
from os import path as ospath
from pathlib import Path
from re import IGNORECASE, search, sub
from time import time

from asyncio import to_thread

from ... import DOWNLOAD_DIR, LOGGER
from ...core.config_manager import Config

_DOWNLOAD_DIR = DOWNLOAD_DIR if isinstance(DOWNLOAD_DIR, str) else "downloads"
_ARCHIVE_FILE = ospath.join(_DOWNLOAD_DIR, "missav_archive.json")
_ARCHIVE_CACHE: dict[str, dict] = {}
_INITIALIZED = False


def _extract_video_code(item_or_url) -> str:
    if not item_or_url:
        return ""
    if hasattr(item_or_url, "url"):
        url = str(item_or_url.url or "").strip()
        title = str(getattr(item_or_url, "title", "") or "").strip()
    else:
        url = str(item_or_url).strip()
        title = ""

    # 1. Try slug from URL
    slug = url.rstrip("/").split("/")[-1].lower()
    # Check if slug looks like a release code (e.g. ebwh-373, fc2-ppv-4993751, stars-999)
    m = search(r"([a-z0-9]+(?:-[a-z0-9]+)+)", slug)
    if m:
        return m.group(1).lower()

    # 2. Try title match
    if title:
        m_title = search(r"([a-z0-9]+(?:-[a-z0-9]+)+)", title.lower())
        if m_title:
            return m_title.group(1).lower()

    return slug or url.lower()


def _normalize_url(url: str) -> str:
    clean = str(url or "").strip().lower()
    clean = sub(r"https?://(?:www\.)?missav\.[a-z]+", "https://missav.live", clean)
    return clean.rstrip("/")


class MissAVArchive:
    @classmethod
    async def init(cls):
        global _INITIALIZED
        if _INITIALIZED:
            return
        _INITIALIZED = True
        # 1. Load local JSON cache
        if ospath.isfile(_ARCHIVE_FILE):
            try:
                content = Path(_ARCHIVE_FILE).read_text(encoding="utf-8")
                data = json.loads(content)
                if isinstance(data, dict):
                    _ARCHIVE_CACHE.update(data)
                LOGGER.info(f"Loaded {len(_ARCHIVE_CACHE)} MissAV archive records from local file.")
            except Exception as e:
                LOGGER.warning(f"Failed loading MissAV archive local file: {e}")

        # 2. Sync from MongoDB if connected
        try:
            from .db_handler import database
            if database.db is not None:
                cursor = database.db.missav_archive.find({})
                count = 0
                async for doc in cursor:
                    code = doc.get("_id")
                    if code and code not in _ARCHIVE_CACHE:
                        _ARCHIVE_CACHE[code] = {
                            "code": code,
                            "url": doc.get("url", ""),
                            "title": doc.get("title", ""),
                            "chat_id": doc.get("chat_id", 0),
                            "message_ids": doc.get("message_ids", []),
                            "quality": doc.get("quality", ""),
                            "time": doc.get("time", 0),
                        }
                        count += 1
                if count:
                    LOGGER.info(f"Loaded {count} additional MissAV archive records from MongoDB.")
        except Exception as e:
            LOGGER.warning(f"MissAV archive MongoDB sync note: {e}")

    @classmethod
    def _save_local_sync(cls):
        try:
            Path(ospath.dirname(_ARCHIVE_FILE)).mkdir(parents=True, exist_ok=True)
            tmp_file = f"{_ARCHIVE_FILE}.tmp"
            Path(tmp_file).write_text(json.dumps(_ARCHIVE_CACHE, indent=2), encoding="utf-8")
            Path(tmp_file).replace(_ARCHIVE_FILE)
        except Exception as e:
            LOGGER.warning(f"Failed writing local MissAV archive file: {e}")

    @classmethod
    async def get(cls, item_or_url) -> dict | None:
        if not _INITIALIZED:
            await cls.init()
        code = _extract_video_code(item_or_url)
        if not code:
            return None

        # Check in-memory cache by code
        if code in _ARCHIVE_CACHE:
            return _ARCHIVE_CACHE[code]

        # Check in-memory cache by normalized url
        norm_url = _normalize_url(getattr(item_or_url, "url", item_or_url) if item_or_url else "")
        for record in _ARCHIVE_CACHE.values():
            if _normalize_url(record.get("url", "")) == norm_url:
                return record

        # Check MongoDB
        try:
            from .db_handler import database
            if database.db is not None:
                doc = await database.db.missav_archive.find_one({"_id": code})
                if not doc and norm_url:
                    doc = await database.db.missav_archive.find_one({"norm_url": norm_url})
                if doc:
                    rec = {
                        "code": code,
                        "url": doc.get("url", ""),
                        "title": doc.get("title", ""),
                        "chat_id": doc.get("chat_id", 0),
                        "message_ids": doc.get("message_ids", []),
                        "quality": doc.get("quality", ""),
                        "time": doc.get("time", 0),
                    }
                    _ARCHIVE_CACHE[code] = rec
                    return rec
        except Exception as e:
            LOGGER.debug(f"MissAV archive MongoDB lookup error: {e}")

        return None

    @classmethod
    async def save(cls, item, chat_id: int | str, message_ids: list[int], quality: str = ""):
        if not _INITIALIZED:
            await cls.init()
        code = _extract_video_code(item)
        if not code or not message_ids:
            return

        url = getattr(item, "url", str(item))
        title = getattr(item, "title", code)
        norm_url = _normalize_url(url)

        record = {
            "code": code,
            "url": url,
            "norm_url": norm_url,
            "title": title,
            "chat_id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
            "message_ids": [int(mid) for mid in message_ids if mid],
            "quality": quality,
            "time": time(),
        }

        # Update in-memory cache
        _ARCHIVE_CACHE[code] = record

        # Save to local file asynchronously
        await to_thread(cls._save_local_sync)

        # Save to MongoDB
        try:
            from .db_handler import database
            if database.db is not None:
                await database.db.missav_archive.update_one(
                    {"_id": code},
                    {"$set": record},
                    upsert=True,
                )
                LOGGER.info(f"Recorded MissAV title '{title}' ({code}) in MongoDB archive with {len(message_ids)} message IDs.")
        except Exception as e:
            LOGGER.warning(f"MissAV archive MongoDB save failed: {e}")

    @classmethod
    async def remove(cls, item_or_url):
        code = _extract_video_code(item_or_url)
        if not code:
            return
        _ARCHIVE_CACHE.pop(code, None)
        await to_thread(cls._save_local_sync)
        try:
            from .db_handler import database
            if database.db is not None:
                await database.db.missav_archive.delete_one({"_id": code})
        except Exception:
            pass
