# ruff: noqa: E402
"""
Anime Starfall • Cross-Process Web Studio Bridge (`bot/helper/ext_utils/web_bridge.py`)
Synchronizes live `task_dict`, `user_data["TASK_MODE"]`, and `/api/leech/start`
commands between the Pyrogram `bot` process and the Gunicorn/Uvicorn `web.wserver` worker.
"""

import json
import os
from asyncio import sleep
from time import time
from typing import Any, Dict, List

TASKS_SNAPSHOT_PATH = "/tmp/starfall_active_tasks.json"
USERS_SNAPSHOT_PATH = "/tmp/starfall_users_snapshot.json"
IPC_QUEUE_PATH = "/tmp/starfall_web_ipc.jsonl"


def export_users_snapshot(user_data: Dict[int, Dict[str, Any]], default_dump: Any = None) -> None:
    """Write a lightweight snapshot of user modes and dump channels for fast session lookups."""
    try:
        snapshot = {}
        for uid, udict in list(user_data.items()):
            if not isinstance(udict, dict):
                continue
            snapshot[str(uid)] = {
                "mode": "web" if str(udict.get("TASK_MODE", "telegram")).lower() == "web" else "telegram",
                "dump_channel": udict.get("LEECH_DUMP_CHAT") or default_dump,
                "username": udict.get("USERNAME") or f"user_{uid}",
            }
        tmp_path = f"{USERS_SNAPSHOT_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(snapshot, fp)
        os.replace(tmp_path, USERS_SNAPSHOT_PATH)
    except Exception:
        pass


def read_user_snapshot(user_id: int) -> Dict[str, Any]:
    """Read cached user session metadata from the shared snapshot file."""
    try:
        if os.path.exists(USERS_SNAPSHOT_PATH):
            with open(USERS_SNAPSHOT_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data.get(str(user_id), {})
    except Exception:
        pass
    return {}


def update_user_snapshot_mode(user_id: int, mode: str) -> None:
    """Update a user's TASK_MODE in the shared snapshot file and queue an IPC sync event."""
    normalized = "web" if str(mode).lower() == "web" else "telegram"
    try:
        data = {}
        if os.path.exists(USERS_SNAPSHOT_PATH):
            with open(USERS_SNAPSHOT_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        entry = data.get(str(user_id), {})
        entry["mode"] = normalized
        data[str(user_id)] = entry
        tmp_path = f"{USERS_SNAPSHOT_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp)
        os.replace(tmp_path, USERS_SNAPSHOT_PATH)
    except Exception:
        pass

    enqueue_ipc_event({"type": "set_mode", "user_id": int(user_id), "mode": normalized})


def export_active_tasks_snapshot(task_dict: Dict[Any, Any]) -> None:
    """Export all active tasks in `bot.task_dict` to `/tmp/starfall_active_tasks.json`."""
    try:
        tasks_by_user: Dict[str, List[Dict[str, Any]]] = {}
        for mid, status_obj in list(task_dict.items()):
            listener = getattr(status_obj, "listener", None)
            uid = getattr(listener, "user_id", 0) if listener else 0
            if not uid:
                continue

            def _call_or_attr(obj, attr, fallback=""):
                val = getattr(obj, attr, None)
                try:
                    return str(val() if callable(val) else (val or fallback))
                except Exception:
                    return str(fallback)

            gid = _call_or_attr(status_obj, "gid", str(mid))
            name = _call_or_attr(status_obj, "name", getattr(listener, "name", "Task"))
            status_str = _call_or_attr(status_obj, "status", "Processing")
            progress_str = _call_or_attr(status_obj, "progress", "0%")
            processed_str = _call_or_attr(status_obj, "processed_bytes", "0 B")
            size_str = _call_or_attr(status_obj, "size", "0 B")
            speed_str = _call_or_attr(status_obj, "speed", "0 B/s")
            eta_str = _call_or_attr(status_obj, "eta", "-")
            engine_str = str(getattr(status_obj, "engine", status_obj.__class__.__name__.replace("Status", "")))

            tasks_by_user.setdefault(str(uid), []).append(
                {
                    "task_id": mid,
                    "gid": gid,
                    "name": name,
                    "status": status_str,
                    "progress": progress_str,
                    "processed_bytes": processed_str,
                    "size": size_str,
                    "speed": speed_str,
                    "eta": eta_str,
                    "engine": engine_str,
                    "updated_at": int(time()),
                }
            )

        tmp_path = f"{TASKS_SNAPSHOT_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(tasks_by_user, fp)
        os.replace(tmp_path, TASKS_SNAPSHOT_PATH)
    except Exception:
        pass


def read_active_tasks_for_user(user_id: int) -> List[Dict[str, Any]]:
    """Read live tasks for `user_id` from the shared snapshot file."""
    try:
        if os.path.exists(TASKS_SNAPSHOT_PATH):
            with open(TASKS_SNAPSHOT_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data.get(str(user_id), [])
    except Exception:
        pass
    return []


def enqueue_ipc_event(event: Dict[str, Any]) -> None:
    """Append an IPC event from `web/wserver.py` for the `bot` process to execute."""
    try:
        with open(IPC_QUEUE_PATH, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception:
        pass


async def start_web_bridge_daemon() -> None:
    """
    Background loop running inside the `bot` process that:
    1. Exports `task_dict` and `user_data` snapshots every 1.5s.
    2. Consumes IPC events from `web/wserver.py` (`set_mode` and `start_leech`).
    """
    from bot import LOGGER, task_dict, user_data
    from bot.core.config_manager import Config
    from bot.helper.ext_utils.bot_utils import update_user_ldata

    while True:
        try:
            export_active_tasks_snapshot(task_dict)
            export_users_snapshot(user_data, getattr(Config, "LEECH_DUMP_CHAT", None))

            if os.path.exists(IPC_QUEUE_PATH):
                processing_path = f"{IPC_QUEUE_PATH}.proc"
                try:
                    os.replace(IPC_QUEUE_PATH, processing_path)
                    with open(processing_path, "r", encoding="utf-8") as fp:
                        lines = fp.readlines()
                    os.remove(processing_path)
                except Exception:
                    lines = []

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        etype = evt.get("type")
                        if etype == "set_mode":
                            uid = int(evt["user_id"])
                            mode = "web" if str(evt.get("mode")).lower() == "web" else "telegram"
                            update_user_ldata(uid, "TASK_MODE", mode)
                        elif etype == "start_leech":
                            from bot.helper.mirror_leech import start_web_leech_task

                            await start_web_leech_task(
                                user_id=int(evt["user_id"]),
                                url=str(evt["url"]),
                                mode=str(evt.get("mode", "leech")),
                                engine=str(evt.get("engine", "auto")),
                                custom_name=str(evt.get("name", "")),
                                select=bool(evt.get("select", False)),
                                extract=bool(evt.get("extract", False)),
                                compress=bool(evt.get("compress", False)),
                                up_dest=str(evt.get("up_dest", "")),
                            )
                    except Exception as inner_exc:
                        LOGGER.warning(f"Web bridge IPC event error: {inner_exc}")
        except Exception as exc:
            LOGGER.warning(f"Web bridge daemon cycle error: {exc}")

        await sleep(1.5)
