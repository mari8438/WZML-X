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
                        elif etype == "cancel_task":
                            target_gid = str(evt.get("gid") or evt.get("task_id") or "")
                            for mid, st_obj in list(task_dict.items()):
                                g_val = str(st_obj.gid()) if callable(getattr(st_obj, "gid", None)) else str(mid)
                                if g_val == target_gid or str(mid) == target_gid:
                                    t_obj = st_obj.task() if callable(getattr(st_obj, "task", None)) else None
                                    if t_obj and hasattr(t_obj, "cancel_task"):
                                        await t_obj.cancel_task()
                        elif etype == "mux_video":
                            await execute_lossless_mux_ipc(evt)
                        elif etype == "update_user_settings":
                            uid = int(evt["user_id"])
                            s_dict = evt.get("settings") or {}
                            for k, v in s_dict.items():
                                update_user_ldata(uid, k, v)
                        elif etype == "admin_config_action":
                            cfg_dict = evt.get("config") or {}
                            if cfg_dict:
                                Config.load_dict(cfg_dict)
                            if evt.get("action") == "reload_db":
                                Config.load()
                        elif etype == "admin_restart":
                            LOGGER.info("[web_bridge] Owner triggered graceful restart from Web Studio")
                            os._exit(0)
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


async def execute_lossless_mux_ipc(evt: Dict[str, Any]) -> None:
    """
    Execute a strictly lossless mkvmerge (or fmpeg -threads 1 -c copy) container remux
    with audio delay offsets (--sync), track reordering, and language/title tags.
    Never transcodes video streams.
    """
    from asyncio import create_subprocess_exec
    from asyncio.subprocess import PIPE
    from bot import LOGGER

    task_id = str(evt.get("task_id", ""))
    base_dir = f"/usr/src/app/downloads/{task_id}"
    if not os.path.exists(base_dir):
        LOGGER.warning(f"[web_bridge] mux_video directory not found: {base_dir}")
        return

    input_file = ""
    for root, _, files in os.walk(base_dir):
        for fn in files:
            if fn.lower().endswith((".mkv", ".mp4", ".webm", ".avi", ".mov", ".ts")):
                input_file = os.path.join(root, fn)
                break
        if input_file:
            break

    if not input_file:
        return

    output_file = f"{os.path.splitext(input_file)[0]}.starfall_mux.mkv"
    audio_tracks = evt.get("audio_tracks") or []
    subtitle_tracks = evt.get("subtitle_tracks") or []

    kept_audios = [a for a in audio_tracks if a.get("keep", True)]
    kept_subs = [s for s in subtitle_tracks if s.get("keep", True)]

    cmd = ["mkvmerge", "-o", output_file, "--video-tracks", "0"]

    if kept_audios:
        cmd.extend(["--audio-tracks", ",".join(str(a["id"]) for a in kept_audios)])
        for a in kept_audios:
            aid = str(a["id"])
            lang = str(a.get("lang", "jpn"))
            title = str(a.get("title", ""))
            delay_ms = int(a.get("delayMs", 0) or 0)
            cmd.extend(["--language", f"{aid}:{lang}"])
            if title:
                cmd.extend(["--track-name", f"{aid}:{title}"])
            if delay_ms != 0:
                cmd.extend(["--sync", f"{aid}:{delay_ms}"])
            cmd.extend(["--default-track-flag", f"{aid}:{'1' if a.get('isDefault') else '0'}"])
            if a.get("isForced"):
                cmd.extend(["--forced-display-flag", f"{aid}:1"])
    else:
        cmd.append("--no-audio")

    if kept_subs:
        cmd.extend(["--subtitle-tracks", ",".join(str(s["id"]) for s in kept_subs)])
        for s in kept_subs:
            sid = str(s["id"])
            lang = str(s.get("lang", "eng"))
            title = str(s.get("title", ""))
            cmd.extend(["--language", f"{sid}:{lang}"])
            if title:
                cmd.extend(["--track-name", f"{sid}:{title}"])
            cmd.extend(["--default-track-flag", f"{sid}:{'1' if s.get('isDefault') else '0'}"])
            if s.get("isForced"):
                cmd.extend(["--forced-display-flag", f"{sid}:1"])
    else:
        cmd.append("--no-subtitles")

    track_order = ["0:0"] + [f"0:{a['id']}" for a in kept_audios] + [f"0:{s['id']}" for s in kept_subs]
    cmd.extend(["--track-order", ",".join(track_order), input_file])

    proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode in (0, 1) and os.path.exists(output_file):
        os.replace(output_file, input_file)
        LOGGER.info(f"[web_bridge] Lossless mkvmerge completed for {input_file}")
    else:
        LOGGER.error(f"[web_bridge] mkvmerge error: {stderr.decode('utf-8', errors='replace')[:300]}")

