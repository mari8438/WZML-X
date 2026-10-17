from importlib import import_module

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError
from pymongo.server_api import ServerApi

from ... import LOGGER, qbit_options, rss_dict, user_data
from ...core.config_manager import Config
from ...core.tg_client import TgClient, db_partition_id


class DbManager:
    def __init__(self):
        self._return = True
        self._conn = None
        self.db = None

    def _partition(self):
        if TgClient.PARTITION:
            return str(TgClient.PARTITION)
        bot_id = TgClient.ID or (Config.BOT_TOKEN or "").split(":", 1)[0]
        if not bot_id:
            return "0"
        TgClient.PARTITION = db_partition_id(bot_id)
        return str(TgClient.PARTITION)

    async def connect(self):
        try:
            if self._conn is not None:
                await self._conn.close()
            self._conn = AsyncMongoClient(
                Config.DATABASE_URL, server_api=ServerApi("1")
            )
            self.db = self._conn.wzmlx
            self._return = False
        except PyMongoError as e:
            LOGGER.error(f"Error in DB connection: {e}")
            self.db = None
            self._return = True
            self._conn = None

    async def disconnect(self):
        self._return = True
        if self._conn is not None:
            await self._conn.close()
        self._conn = None

    async def update_deploy_config(self):
        if self._return:
            return
        settings = import_module("config")
        config_file = {
            key: value.strip() if isinstance(value, str) else value
            for key, value in vars(settings).items()
            if not key.startswith("__")
        }
        await self.db.settings.deployConfig.replace_one(
            {"_id": self._partition()}, config_file, upsert=True
        )

    async def update_config(self, dict_):
        if self._return:
            return
        await self.db.settings.config.update_one(
            {"_id": self._partition()}, {"$set": dict_}, upsert=True
        )

    async def update_aria2(self, key, value):
        if self._return:
            return
        await self.db.settings.aria2c.update_one(
            {"_id": self._partition()}, {"$set": {key: value}}, upsert=True
        )

    async def update_qbittorrent(self, key, value):
        if self._return:
            return
        await self.db.settings.qbittorrent.update_one(
            {"_id": self._partition()}, {"$set": {key: value}}, upsert=True
        )

    async def save_qbit_settings(self):
        if self._return:
            return
        await self.db.settings.qbittorrent.update_one(
            {"_id": self._partition()}, {"$set": qbit_options}, upsert=True
        )

    async def update_private_file(self, path):
        if self._return:
            return
        db_path = path.replace(".", "__")
        if await aiopath.exists(path):
            async with aiopen(path, "rb+") as pf:
                pf_bin = await pf.read()
            await self.db.settings.files.update_one(
                {"_id": self._partition()}, {"$set": {db_path: pf_bin}}, upsert=True
            )
            if path == "config.py":
                await self.update_deploy_config()
        else:
            await self.db.settings.files.update_one(
                {"_id": self._partition()}, {"$unset": {db_path: ""}}, upsert=True
            )

    async def update_nzb_config(self):
        if self._return:
            return
        async with aiopen("configs/sabnzbd/SABnzbd.ini", "rb+") as pf:
            nzb_conf = await pf.read()
        await self.db.settings.nzb.replace_one(
            {"_id": self._partition()}, {"SABnzbd__ini": nzb_conf}, upsert=True
        )

    async def update_user_data(self, user_id):
        if self._return:
            return
        data = user_data.get(user_id, {})
        data = data.copy()
        file_keys = (
            "THUMBNAIL",
            "THUMBNAIL_LANDSCAPE",
            "THUMBNAIL_POSTER",
            "RCLONE_CONFIG",
            "TOKEN_PICKLE",
            "USER_COOKIE_FILE",
        )
        for key in file_keys:
            data.pop(key, None)
        pipeline = [
            {
                "$replaceRoot": {
                    "newRoot": {
                        "$mergeObjects": [
                            data,
                            {
                                "$arrayToObject": {
                                    "$filter": {
                                        "input": {"$objectToArray": "$$ROOT"},
                                        "as": "field",
                                        "cond": {
                                            "$in": [
                                                "$$field.k",
                                                list(file_keys),
                                            ]
                                        },
                                    }
                                }
                            },
                        ]
                    }
                }
            }
        ]
        await self.db.users[self._partition()].update_one(
            {"_id": user_id}, pipeline, upsert=True
        )

    async def update_user_doc(self, user_id, key, path=""):
        if self._return:
            return
        if path:
            async with aiopen(path, "rb+") as doc:
                doc_bin = await doc.read()
            await self.db.users[self._partition()].update_one(
                {"_id": user_id}, {"$set": {key: doc_bin}}, upsert=True
            )
        else:
            await self.db.users[self._partition()].update_one(
                {"_id": user_id}, {"$unset": {key: ""}}, upsert=True
            )

    async def rss_update_all(self):
        if self._return:
            return
        for user_id in list(rss_dict.keys()):
            await self.db.rss[self._partition()].replace_one(
                {"_id": user_id}, rss_dict[user_id], upsert=True
            )

    async def rss_update(self, user_id):
        if self._return:
            return
        await self.db.rss[self._partition()].replace_one(
            {"_id": user_id}, rss_dict[user_id], upsert=True
        )

    async def rss_delete(self, user_id):
        if self._return:
            return
        await self.db.rss[self._partition()].delete_one({"_id": user_id})

    async def add_incomplete_task(self, cid, link, tag):
        if self._return:
            return
        await self.db.tasks[self._partition()].insert_one(
            {"_id": link, "cid": cid, "tag": tag}
        )

    async def get_pm_uids(self):
        if self._return:
            return
        return [doc["_id"] async for doc in self.db.pm_users[self._partition()].find({})]

    async def set_pm_users(self, user_id):
        if self._return:
            return
        pm_users = self.db.pm_users[self._partition()]
        if not bool(await pm_users.find_one({"_id": user_id})):
            await pm_users.insert_one({"_id": user_id})
            LOGGER.info(f"New PM User Added : {user_id}")

    async def rm_pm_user(self, user_id):
        if self._return:
            return
        await self.db.pm_users[self._partition()].delete_one({"_id": user_id})

    async def rm_complete_task(self, link):
        if self._return:
            return
        await self.db.tasks[self._partition()].delete_one({"_id": link})

    async def get_incomplete_tasks(self):
        notifier_dict = {}
        if self._return:
            return notifier_dict
        tasks = self.db.tasks[self._partition()]
        if await tasks.find_one():
            rows = tasks.find({})
            async for row in rows:
                if row["cid"] in list(notifier_dict.keys()):
                    if row["tag"] in list(notifier_dict[row["cid"]]):
                        notifier_dict[row["cid"]][row["tag"]].append(row["_id"])
                    else:
                        notifier_dict[row["cid"]][row["tag"]] = [row["_id"]]
                else:
                    notifier_dict[row["cid"]] = {row["tag"]: [row["_id"]]}
        await tasks.drop()
        return notifier_dict

    async def trunc_table(self, name):
        if self._return:
            return
        await self.db[name][self._partition()].drop()

    async def get_next_hstream_pack_number(self):
        if self._return or self.db is None:
            return 1
        try:
            cursor = self.db.happyfappy_hstream_packs.find(
                {"partition": self._partition()},
                {"pack_number": 1}
            ).sort("pack_number", -1).limit(1)
            docs = [d async for d in cursor]
            if docs and "pack_number" in docs[0]:
                return int(docs[0]["pack_number"]) + 1
        except Exception as e:
            LOGGER.warning(f"Failed to get next Hstream pack number from DB: {e}")
        return 1

    async def get_packaged_hstream_slugs(self):
        if self._return or self.db is None:
            return set()
        slugs = set()
        try:
            cursor = self.db.happyfappy_hstream_packs.find(
                {"partition": self._partition(), "status": "COMPLETED"},
                {"episodes.slug": 1}
            )
            async for doc in cursor:
                for ep in doc.get("episodes", []):
                    if ep.get("slug"):
                        slugs.add(ep["slug"])
        except Exception as e:
            LOGGER.warning(f"Failed to query packaged Hstream slugs: {e}")
        return slugs

    async def save_hstream_pack(self, pack_data):
        if self._return or self.db is None:
            return
        try:
            pack_data["partition"] = self._partition()
            pack_id = pack_data.get("_id") or f"pack_{pack_data.get('pack_number')}_{self._partition()}"
            pack_data["_id"] = pack_id
            await self.db.happyfappy_hstream_packs.replace_one(
                {"_id": pack_id}, pack_data, upsert=True
            )
        except Exception as e:
            LOGGER.warning(f"Failed to save Hstream pack to DB: {e}")

    async def get_all_completed_hstream_packs(self):
        if self._return or self.db is None:
            return []
        try:
            cursor = self.db.happyfappy_hstream_packs.find(
                {"partition": self._partition(), "status": "COMPLETED"}
            ).sort("pack_number", 1)
            return [doc async for doc in cursor]
        except Exception as e:
            LOGGER.warning(f"Failed to query completed Hstream packs: {e}")
            return []

    async def get_hstream_pinned_index_id(self):
        if self._return or self.db is None:
            return None
        try:
            doc = await self.db.settings.pinned_index.find_one({"_id": self._partition()})
            return doc.get("message_id") if doc else None
        except Exception:
            return None

    async def set_hstream_pinned_index_id(self, message_id):
        if self._return or self.db is None:
            return
        try:
            await self.db.settings.pinned_index.update_one(
                {"_id": self._partition()},
                {"$set": {"message_id": message_id}},
                upsert=True,
            )
        except Exception as e:
            LOGGER.warning(f"Failed to update pinned index id in DB: {e}")


database = DbManager()
