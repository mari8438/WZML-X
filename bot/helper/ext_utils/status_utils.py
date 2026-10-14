from asyncio import gather, iscoroutinefunction
from html import escape
from re import findall
from time import time

from psutil import cpu_percent, disk_usage, net_io_counters, virtual_memory
from pyrogram.enums import ButtonStyle

from ... import (
    DOWNLOAD_DIR,
    bot_cache,
    bot_start_time,
    non_queued_dl,
    non_queued_up,
    queued_dl,
    queued_up,
    status_dict,
    task_dict,
    task_dict_lock,
)
from ...core.config_manager import Config
from ..telegram_helper.button_build import ButtonMaker

SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]
GREEN_DOT = "\U0001F7E2"
_network_sample = {"time": time(), "sent": 0, "recv": 0}


def _network_status():
    counters = net_io_counters()
    now = time()
    elapsed = max(0.001, now - _network_sample["time"])
    sent_rate = max(0, counters.bytes_sent - _network_sample["sent"]) / elapsed
    recv_rate = max(0, counters.bytes_recv - _network_sample["recv"]) / elapsed
    if not _network_sample["sent"] and not _network_sample["recv"]:
        sent_rate = recv_rate = 0
    _network_sample.update(time=now, sent=counters.bytes_sent, recv=counters.bytes_recv)
    return counters.bytes_sent + counters.bytes_recv, sent_rate, recv_rate


class MirrorStatus:
    STATUS_UPLOAD = "Upload"
    STATUS_DOWNLOAD = "Download"
    STATUS_CLONE = "Clone"
    STATUS_QUEUEDL = "QueueDl"
    STATUS_QUEUEUP = "QueueUp"
    STATUS_PAUSED = "Pause"
    STATUS_ARCHIVE = "Archive"
    STATUS_EXTRACT = "Extract"
    STATUS_SPLIT = "Split"
    STATUS_CHECK = "CheckUp"
    STATUS_SEED = "Seed"
    STATUS_SAMVID = "SamVid"
    STATUS_CONVERT = "Convert"
    STATUS_FFMPEG = "FFmpeg"
    STATUS_AUTOPROCESS = "AutoProcess"
    STATUS_YT = "YouTube"
    STATUS_METADATA = "Metadata"


class EngineStatus:
    def __init__(self):
        ver = bot_cache.get("eng_versions", {})
        self.STATUS_ARIA2 = f"Aria2 v{ver.get('aria2', 'N/A')}"
        self.STATUS_AIOHTTP = f"AioHttp v{ver.get('aiohttp', 'N/A')}"
        self.STATUS_GDAPI = f"Google-API v{ver.get('gapi', 'N/A')}"
        self.STATUS_QBIT = f"qBit v{ver.get('qBittorrent', 'N/A')}"
        self.STATUS_TGRAM = f"{Config.UPLOAD_ENGINE} v{Config.UPLOAD_ENGINE_VERSION}"
        self.STATUS_MEGA = f"MegaCMD v{ver.get('mega', 'N/A')}"
        self.STATUS_YTDLP = f"yt-dlp v{ver.get('yt-dlp', 'N/A')}"
        self.STATUS_FFMPEG = f"ffmpeg v{ver.get('ffmpeg', 'N/A')}"
        self.STATUS_7Z = f"7z v{ver.get('7z', 'N/A')}"
        self.STATUS_RCLONE = f"RClone v{ver.get('rclone', 'N/A')}"
        self.STATUS_SABNZBD = f"SABnzbd+ v{ver.get('SABnzbd+', 'N/A')}"
        self.STATUS_QUEUE = "QSystem v2"
        self.STATUS_JD = "JDownloader v2"
        self.STATUS_YT = "Youtube-Api"
        self.STATUS_METADATA = "Metadata"
        self.STATUS_UPHOSTER = "Uphoster"


STATUSES = {
    "ALL": "All",
    "DL": MirrorStatus.STATUS_DOWNLOAD,
    "UP": MirrorStatus.STATUS_UPLOAD,
    "QD": MirrorStatus.STATUS_QUEUEDL,
    "QU": MirrorStatus.STATUS_QUEUEUP,
    "AR": MirrorStatus.STATUS_ARCHIVE,
    "EX": MirrorStatus.STATUS_EXTRACT,
    "SD": MirrorStatus.STATUS_SEED,
    "CL": MirrorStatus.STATUS_CLONE,
    "CM": MirrorStatus.STATUS_CONVERT,
    "SP": MirrorStatus.STATUS_SPLIT,
    "SV": MirrorStatus.STATUS_SAMVID,
    "FF": MirrorStatus.STATUS_FFMPEG,
    "AP": MirrorStatus.STATUS_AUTOPROCESS,
    "PA": MirrorStatus.STATUS_PAUSED,
    "CK": MirrorStatus.STATUS_CHECK,
}


async def get_task_by_gid(gid: str):
    async with task_dict_lock:
        for tk in task_dict.values():
            if hasattr(tk, "seeding"):
                await tk.update()
            if tk.gid() == gid:
                return tk
        return None


async def get_specific_tasks(status, user_id):
    def visible_task(tk):
        return not getattr(tk.listener, "rss_auto_leech", False)

    if status == "All":
        if user_id:
            return [
                tk
                for tk in task_dict.values()
                if tk.listener.user_id == user_id and visible_task(tk)
            ]
        else:
            return [tk for tk in task_dict.values() if visible_task(tk)]
    tasks_to_check = (
        [
            tk
            for tk in task_dict.values()
            if tk.listener.user_id == user_id and visible_task(tk)
        ]
        if user_id
        else [tk for tk in task_dict.values() if visible_task(tk)]
    )
    coro_tasks = []
    coro_tasks.extend(tk for tk in tasks_to_check if iscoroutinefunction(tk.status))
    coro_statuses = await gather(*[tk.status() for tk in coro_tasks])
    result = []
    coro_index = 0
    for tk in tasks_to_check:
        if tk in coro_tasks:
            st = coro_statuses[coro_index]
            coro_index += 1
        else:
            st = tk.status()
        if (st == status) or (
            status == MirrorStatus.STATUS_DOWNLOAD and st not in STATUSES.values()
        ):
            result.append(tk)
    return result


async def get_all_tasks(req_status: str, user_id):
    async with task_dict_lock:
        return await get_specific_tasks(req_status, user_id)


def get_raw_file_size(size):
    num, unit = size.split()
    return int(float(num) * (1024 ** SIZE_UNITS.index(unit)))


def get_readable_file_size(size_in_bytes):
    if not size_in_bytes:
        return "0B"

    index = 0
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1

    return f"{size_in_bytes:.2f}{SIZE_UNITS[index]}"


def get_readable_time(seconds: int):
    periods = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    result = ""
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result += f"{int(period_value)}{period_name}"
    return result


def get_raw_time(time_str: str) -> int:
    time_units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(
        int(value) * time_units[unit]
        for value, unit in findall(r"(\d+)([dhms])", time_str)
    )


def time_to_seconds(time_duration):
    try:
        parts = time_duration.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = map(float, parts)
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = map(float, parts)
        elif len(parts) == 1:
            hours = 0
            minutes = 0
            seconds = float(parts[0])
        else:
            return 0
        return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return 0


def speed_string_to_bytes(size_text: str):
    if isinstance(size_text, (int, float)):
        return int(size_text)
    if not size_text:
        return 0
    size = 0
    size_text = str(size_text).strip().lower()
    try:
        if "k" in size_text:
            size += float(size_text.split("k")[0]) * 1024
        elif "m" in size_text:
            size += float(size_text.split("m")[0]) * 1048576
        elif "g" in size_text:
            size += float(size_text.split("g")[0]) * 1073741824
        elif "t" in size_text:
            size += float(size_text.split("t")[0]) * 1099511627776
        elif "b" in size_text:
            size += float(size_text.split("b")[0])
        elif size_text:
            size += float(size_text)
    except (TypeError, ValueError):
        return 0
    return int(size)


def _legacy_progress_bar_string(pct):
    pct = float(str(pct).strip("%"))
    p = min(max(pct, 0), 100)
    cFull = int(p // 8)
    p_str = "⬢" * cFull
    p_str += "⬡" * (12 - cFull)
    return f"[{p_str}]"

def _legacy_square_progress_bar_string(pct):
    pct = float(str(pct).strip("%"))
    p = min(max(pct, 0), 100)
    full = int(p // 8)
    return f"[{'■' * full}{'□' * (12 - full)}]"


def get_progress_bar_string(pct):
    try:
        pct = float(str(pct).strip("%"))
    except (TypeError, ValueError):
        pct = 0
    progress = min(max(pct, 0), 100)
    filled = min(12, max(0, int(round((progress / 100) * 12))))
    return "▰" * filled + "▱" * (12 - filled)


def _compact_task_name(name, limit=90):
    name = " ".join(str(name or "Unnamed task").split())
    return name if len(name) <= limit else f"{name[: limit - 1]}…"


def _status_theme():
    theme = str(
        getattr(Config, "BOT_THEME", "")
        or getattr(Config, "STATUS_THEME", "")
        or "starfall"
    ).lower()
    if theme in {"starfall_neo", "neo", "neo_minimal"}:
        return "starfall"
    return theme


def is_starfall_theme():
    return _status_theme() == "starfall"


def get_starfall_system_status():
    from .performance import get_smoothed_telemetry, MAX_PARALLEL_TASKS

    telem = get_smoothed_telemetry(DOWNLOAD_DIR)
    used_ram_gb = telem.get("used_ram_gb", 0)
    total_ram_gb = telem.get("total_ram_gb", 4.0)
    ram_percent = telem.get("ram_percent", 0)
    cpu_percent = telem.get("cpu_percent", 0)
    free_disk_gb = telem.get("free_disk_gb", 0)
    active = len(non_queued_dl) + len(non_queued_up)
    max_parallel = MAX_PARALLEL_TASKS

    return (
        "<b>╭── ⚙ 𝗩𝗣𝗦 𝗧𝗘𝗟𝗘𝗠𝗘𝗧𝗥𝗬 (2vCPU • 4GB) ──╮</b>\n"
        f"├ 🧠 <b>RAM</b> : <code>{used_ram_gb}/{total_ram_gb} GB ({ram_percent}%)</code> • 🖥 <b>CPU</b> : <code>{cpu_percent}%</code>\n"
        f"╰ 💾 <b>SSD</b> : <code>{free_disk_gb} GB Free</code> • 🚦 <b>Tasks</b> : <code>{active}/{max_parallel}</code>"
    )


def get_legacy_system_status():
    disk = disk_usage(DOWNLOAD_DIR)
    uptime = get_readable_time(time() - bot_start_time) or "0s"
    total_net, tx_rate, rx_rate = _network_status()
    return (
        "⌬ <b><u>Bot Stats</u></b>\n"
        f"┟ <b>CPU</b> → {cpu_percent()}% | <b>F</b> → "
        f"{get_readable_file_size(disk.free)}\n"
        f"┠ <b>RAM</b> → {virtual_memory().percent}% | <b>UP</b> → {uptime}\n"
        f"┖ <b>NET</b> → {get_readable_file_size(total_net)} | ↓ {get_readable_file_size(rx_rate)}/s ↑ {get_readable_file_size(tx_rate)}/s"
    )


def _starfall_progress(progress):
    try:
        value = min(max(float(str(progress).strip("%")), 0), 100)
    except (TypeError, ValueError):
        value = 0
    filled = min(12, max(0, int(round((value / 100) * 12))))
    label = f"{value:.1f}%"
    return "▰" * filled + "▱" * (12 - filled), label


def _task_value(task, method, default="-"):
    try:
        value = getattr(task, method)()
        return default if value in (None, "") else value
    except Exception:
        return default


def _eta_total(eta, elapsed_seconds):
    eta_text = str(eta or "-").strip()
    if eta_text in {"", "-", "N/A", "NA", "None", "∞"}:
        return "-", "-"
    eta_seconds = get_raw_time(eta_text)
    if not eta_seconds and (
        ":" in eta_text or eta_text.replace(".", "", 1).isdigit()
    ):
        eta_seconds = time_to_seconds(eta_text)
    if eta_seconds < 0:
        return "-", "-"
    total = get_readable_time(elapsed_seconds + eta_seconds) or "0s"
    return eta_text, total


def _starfall_task_card(number, task, task_status, cancel_command):
    name = escape(_compact_task_name(_task_value(task, "name", "Unnamed task"), 96))
    progress = _task_value(task, "progress", "0%")
    bar, percentage = _starfall_progress(progress)
    engine = escape(str(getattr(task, "engine", "Unknown")))
    status_str = escape(str(task_status))
    gid = escape(str(_task_value(task, "gid", "unknown")))
    cancel_cmd = f"{cancel_command}_{gid}"

    if task_status == MirrorStatus.STATUS_SEED:
        processed = _task_value(task, "uploaded_bytes", "0B")
        total_size = _task_value(task, "size", "0B")
        speed = _task_value(task, "seed_speed", "0B/s")
        eta = _task_value(task, "seeding_time", "-")
    else:
        processed = _task_value(task, "processed_bytes", "0B")
        total_size = _task_value(task, "size", "0B")
        speed = _task_value(task, "speed", "0B/s")
        eta = _task_value(task, "eta", "-")

    return (
        f"🎬 <code>{name}</code>\n"
        f"╭ <b>Stage</b>   : ⚡ <i>{status_str}</i> ({engine})\n"
        f"├ <b>Prog</b>    : <code>{bar}</code> <b>{percentage}</b>\n"
        f"├ <b>Size</b>    : <code>{processed}</code> / <code>{total_size}</code>\n"
        f"├ <b>Speed</b>   : <code>{speed}</code> • <b>ETA:</b> <code>{eta}</code>\n"
        f"╰ <b>Stop</b>    : /{cancel_cmd}"
    )


async def _get_readable_message_legacy(sid, is_user, page_no=1, status="All", page_step=1):
    msg = ""
    button = None

    tasks = await get_specific_tasks(status, sid if is_user else None)

    STATUS_LIMIT = Config.STATUS_LIMIT
    tasks_no = len(tasks)
    pages = (max(tasks_no, 1) + STATUS_LIMIT - 1) // STATUS_LIMIT
    if page_no > pages:
        page_no = (page_no - 1) % pages + 1
        status_dict[sid]["page_no"] = page_no
    elif page_no < 1:
        page_no = pages - (abs(page_no) % pages)
        status_dict[sid]["page_no"] = page_no
    start_position = (page_no - 1) * STATUS_LIMIT

    for index, task in enumerate(
        tasks[start_position : STATUS_LIMIT + start_position], start=1
    ):
        if status != "All":
            tstatus = status
        elif iscoroutinefunction(task.status):
            tstatus = await task.status()
        else:
            tstatus = task.status()
        msg += f"<b>{index + start_position}.</b> "
        task_name = escape(_compact_task_name(task.name()))
        msg += f"<b><i>{task_name}</i></b>"
        if task.listener.subname:
            msg += f"\n┖ <b>Sub Name</b> → <i>{task.listener.subname}</i>"
        elapsed = time() - task.listener.message.date.timestamp()

        msg += f"\n\n<b>Task By {task.listener.message.from_user.mention(style='html')} </b> ( #ID{task.listener.message.from_user.id} )"
        if task.listener.is_super_chat:
            msg += f" <i>[<a href='{task.listener.message.link}'>Link</a>]</i>"

        if (
            tstatus not in [MirrorStatus.STATUS_SEED, MirrorStatus.STATUS_QUEUEUP]
            and task.listener.progress
        ):
            progress = task.progress()
            msg += f"\n┟ {get_progress_bar_string(progress)} <i>{progress}</i>"
            if task.listener.subname:
                subsize = f" / {get_readable_file_size(task.listener.subsize)}"
                ac = len(task.listener.files_to_proceed)
                count = f"( {task.listener.proceed_count} / {ac or '?'} )"
            else:
                subsize = ""
                count = ""
            msg += f"\n┠ <b>Processed</b> → <i>{task.processed_bytes()}{subsize} of {task.size()}</i>"
            if count:
                msg += f"\n┠ <b>Count:</b> → <b>{count}</b>"
            msg += f"\n┠ <b>Status</b> → <b>{tstatus}</b>"
            msg += f"\n┠ <b>Speed</b> → <i>{task.speed()}</i>"
            msg += f"\n┠ <b>Time</b> → <i>{task.eta()} of {get_readable_time(elapsed + get_raw_time(task.eta()))} ( {get_readable_time(elapsed)} )</i>"
            if tstatus == MirrorStatus.STATUS_DOWNLOAD and (
                task.listener.is_torrent or task.listener.is_qbit
            ):
                try:
                    msg += f"\n┠ <b>Seeders</b> → {task.seeders_num()} | <b>Leechers</b> → {task.leechers_num()}"
                except Exception:
                    pass
            # TODO: Add Connected Peers
        elif tstatus == MirrorStatus.STATUS_SEED:
            msg += f"\n┠ <b>Size</b> → <i>{task.size()}</i> | <b>Uploaded</b>  → <i>{task.uploaded_bytes()}</i>"
            msg += f"\n┠ <b>Status</b> → <b>{tstatus}</b>"
            msg += f"\n┠ <b>Speed</b> → <i>{task.seed_speed()}</i>"
            msg += f"\n┠ <b>Ratio</b> → <i>{task.ratio()}</i>"
            msg += f"\n┠ <b>Time</b> → <i>{task.seeding_time()}</i> | <b>Elapsed</b> → <i>{get_readable_time(elapsed)}</i>"
        else:
            msg += f"\n┠ <b>Size</b> → <i>{task.size()}</i>"
        msg += f"\n┠ <b>Engine</b> → <i>{task.engine}</i>"
        upload_engine = getattr(task.listener, "upload_engine", "")
        upload_client = getattr(task.listener, "upload_client", "")
        if upload_engine:
            msg += f"\n┠ <b>Upload Engine</b> → <i>{upload_engine}</i>"
        if upload_client:
            msg += f"\n┠ <b>Upload Client</b> → <i>{upload_client}</i>"
        msg += f"\n┠ <b>In Mode</b> → <i>{task.listener.mode[0]}</i>"
        msg += f"\n┠ <b>Out Mode</b> → <i>{task.listener.mode[1]}</i>"
        # TODO: Add Bt Sel
        from ..telegram_helper.bot_commands import BotCommands

        msg += f"\n<b>┖ Stop</b> → <i>/{BotCommands.CancelTaskCommand[1]}_{task.gid()}</i>\n\n"

    if len(msg) == 0:
        if status == "All":
            return None, None
        else:
            msg = f"No Active {status} Tasks!\n\n"

    msg += "⌬ <b><u>Bot Stats</u></b>"
    buttons = ButtonMaker()
    if not is_user:
        buttons.data_button("\U0001F4DC TStats", f"status {sid} ov", position="header", style=ButtonStyle.PRIMARY)
    if len(tasks) > STATUS_LIMIT:
        msg += f"<b>Page:</b> {page_no}/{pages} | <b>Tasks:</b> {tasks_no} | <b>Step:</b> {page_step}\n"
        buttons.data_button("<<", f"status {sid} pre", position="header")
        buttons.data_button(">>", f"status {sid} nex", position="header")
        if tasks_no > 30:
            for i in [1, 2, 4, 6, 8, 10, 15]:
                buttons.data_button(i, f"status {sid} ps {i}", position="footer")
    if status != "All" or tasks_no > 20:
        for label, status_value in list(STATUSES.items()):
            if status_value != status:
                buttons.data_button(label, f"status {sid} st {status_value}")
    buttons.data_button(f"{GREEN_DOT} Refresh", f"status {sid} ref", position="header", style=ButtonStyle.SUCCESS)
    button = buttons.build_menu(8)
    msg += f"\n┟ <b>CPU</b> → {cpu_percent()}% | <b>F</b> → {get_readable_file_size(disk_usage(DOWNLOAD_DIR).free)} [{round(100 - disk_usage(DOWNLOAD_DIR).percent, 1)}%]"
    msg += f"\n┖ <b>RAM</b> → {virtual_memory().percent}% | <b>UP</b> → {get_readable_time(time() - bot_start_time)}"
    active_count = len(non_queued_dl) + len(non_queued_up)
    queue_count = len(queued_dl) + len(queued_up)
    msg += f"\n<b>Active/Queued</b> -> {active_count}/{queue_count}"
    try:
        from .starfallx_upload import starfallx_upload

        sfx = starfallx_upload.status_summary()
        msg += (
            f"\n<b>{sfx['engine']}</b> -> Helpers {sfx['helper_active']}"
            f" | Ready {sfx.get('ready', 0)} | Main {sfx['main_active']}"
            f" | Cooling {sfx['cooling']}"
        )
    except Exception:
        pass
    cleanup = {
        "\u00e2\u201d\u2013": "-",
        "\u00e2\u201d\u0178": "-",
        "\u00e2\u201d\u00a0": "-",
        "\u00e2\u2020\u2019": ":",
        "\u00e2\u0152\u00ac": "",
        "\u00e2\u00ac\u00a2": "■",
        "\u00e2\u00ac\u00a1": "□",
        "\u00f0\u0178\u201d\u00b4": "🔴",
        "\u00f0\u0178\u201c\u0153": "",
    }
    for bad, good in cleanup.items():
        msg = msg.replace(bad, good)
    return msg, button


async def get_readable_message(sid, is_user, page_no=1, status="All", page_step=1):
    if _status_theme() != "starfall":
        return await _get_readable_message_legacy(
            sid, is_user, page_no, status, page_step
        )

    from ..telegram_helper.bot_commands import BotCommands

    tasks = await get_specific_tasks(status, sid if is_user else None)
    if not tasks and status == "All":
        return None, None

    limit = max(1, int(Config.STATUS_LIMIT or 10))
    task_count = len(tasks)
    cards = []
    for number, task in enumerate(tasks, start=1):
        task_status = (
            await task.status()
            if iscoroutinefunction(task.status)
            else task.status()
        )
        cards.append(
            _starfall_task_card(
                number,
                task,
                task_status,
                BotCommands.CancelTaskCommand[1],
            )
        )

    header = "<b>✦ 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗧𝗔𝗦𝗞 𝗠𝗢𝗡𝗜𝗧𝗢𝗥 ✦</b>\n\n"
    footer = get_starfall_system_status()
    if not cards:
        cards.append(f"<b>No Active {escape(str(status))} Tasks!</b>")

    card_pages = []
    current_page = []
    for card in cards:
        candidate = header + "\n\n".join((*current_page, card, footer))
        if current_page and (len(current_page) >= limit or len(candidate) > 3600):
            card_pages.append(current_page)
            current_page = []
        current_page.append(card)
    if current_page:
        card_pages.append(current_page)

    pages = len(card_pages)
    page_no = ((page_no - 1) % pages) + 1
    if sid in status_dict:
        status_dict[sid]["page_no"] = page_no
    chunks = card_pages[page_no - 1]
    page_line = (
        f"📄 <b>Page:</b> {page_no}/{pages} | <b>Tasks:</b> {task_count}"
        if pages > 1
        else ""
    )

    buttons = ButtonMaker()
    if not is_user:
        buttons.data_button(
            "📊 TStats",
            f"status {sid} ov",
            position="header",
            style=ButtonStyle.PRIMARY,
        )
    if pages > 1:
        buttons.data_button("◀️", f"status {sid} pre", position="header")
        buttons.data_button("▶️", f"status {sid} nex", position="header")
    buttons.data_button(
        "🟢 Refresh",
        f"status {sid} ref",
        position="header",
        style=ButtonStyle.SUCCESS,
    )
    if status != "All" or task_count > 20:
        for label, value in STATUSES.items():
            if value != status:
                buttons.data_button(label, f"status {sid} st {value}")
    message_parts = [f"{header}{chunks[0]}"] if chunks else []
    if len(chunks) > 1:
        message_parts.extend(chunks[1:])
    if page_line:
        message_parts.append(page_line)
    message_parts.append(footer)
    return "\n\n".join(message_parts), buttons.build_menu(8)
