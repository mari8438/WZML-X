import json
from os import path as ospath
from pathlib import Path
from time import time
from asyncio import to_thread

try:
    from ... import LOGGER
except Exception:
    import logging
    LOGGER = logging.getLogger(__name__)

_CONFIGS_DIR = "configs"
_TOPIC_FILE = ospath.join(_CONFIGS_DIR, "missav_topics.json")
_TOPIC_CACHE: dict[str, int] = {}  # key: f"{chat_id}:{topic_name.lower().strip()}" -> thread_id
_INITIALIZED = False


def _cache_key(chat_id: int | str, topic_name: str) -> str:
    cid = str(chat_id).strip()
    name = str(topic_name or "").strip().lower()
    return f"{cid}:{name}"


class MissAVTopicArchive:
    @classmethod
    async def init(cls):
        global _INITIALIZED
        if _INITIALIZED:
            return
        _INITIALIZED = True

        # 1. Load local JSON cache from persistent configs volume
        if ospath.isfile(_TOPIC_FILE):
            try:
                content = Path(_TOPIC_FILE).read_text(encoding="utf-8")
                data = json.loads(content)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (int, str)) and str(v).isdigit():
                            _TOPIC_CACHE[k] = int(v)
                        elif isinstance(v, dict) and "thread_id" in v:
                            _TOPIC_CACHE[k] = int(v["thread_id"])
                LOGGER.info(f"Loaded {len(_TOPIC_CACHE)} MissAV topic records from local file.")
            except Exception as e:
                LOGGER.warning(f"Failed loading MissAV topics local file: {e}")

        # 2. Sync from MongoDB if connected
        try:
            from .db_handler import database
            if database.db is not None:
                cursor = database.db.missav_topics.find({})
                count = 0
                async for doc in cursor:
                    key = doc.get("_id")
                    tid = doc.get("thread_id")
                    if key and tid is not None and key not in _TOPIC_CACHE:
                        _TOPIC_CACHE[key] = int(tid)
                        count += 1
                if count:
                    LOGGER.info(f"Loaded {count} additional MissAV topic records from MongoDB.")
        except Exception as e:
            LOGGER.warning(f"MissAV topics MongoDB sync note: {e}")

    @classmethod
    def _save_local_sync(cls):
        try:
            Path(ospath.dirname(_TOPIC_FILE)).mkdir(parents=True, exist_ok=True)
            tmp_file = f"{_TOPIC_FILE}.tmp"
            Path(tmp_file).write_text(json.dumps(_TOPIC_CACHE, indent=2), encoding="utf-8")
            Path(tmp_file).replace(_TOPIC_FILE)
        except Exception as e:
            LOGGER.warning(f"Failed writing local MissAV topics file: {e}")

    @classmethod
    async def get(cls, chat_id: int | str, topic_name: str) -> int | None:
        if not _INITIALIZED:
            await cls.init()
        key = _cache_key(chat_id, topic_name)
        return _TOPIC_CACHE.get(key)

    @classmethod
    async def set(cls, chat_id: int | str, topic_name: str, thread_id: int, display_name: str = ""):
        if not _INITIALIZED:
            await cls.init()
        if not topic_name or thread_id is None:
            return

        key = _cache_key(chat_id, topic_name)
        thread_id = int(thread_id)
        _TOPIC_CACHE[key] = thread_id

        # Async write to local file
        await to_thread(cls._save_local_sync)

        # Async write to MongoDB
        try:
            from .db_handler import database
            if database.db is not None:
                await database.db.missav_topics.update_one(
                    {"_id": key},
                    {
                        "$set": {
                            "chat_id": str(chat_id),
                            "topic_name": topic_name.strip().lower(),
                            "display_name": display_name or topic_name.strip(),
                            "thread_id": thread_id,
                            "updated_at": time(),
                        }
                    },
                    upsert=True,
                )
        except Exception as e:
            LOGGER.warning(f"Failed saving MissAV topic to MongoDB: {e}")

    @classmethod
    async def get_all_for_chat(cls, chat_id: int | str) -> dict[str, int]:
        if not _INITIALIZED:
            await cls.init()
        prefix = f"{str(chat_id).strip()}:"
        result = {}
        for k, v in _TOPIC_CACHE.items():
            if k.startswith(prefix):
                topic_name = k[len(prefix):]
                result[topic_name] = v
        return result
