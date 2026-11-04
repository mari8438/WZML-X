import importlib.metadata
from asyncio import gather, wait_for, TimeoutError
from pathlib import Path
from platform import platform, version
from re import search as research
from time import time

from aiofiles.os import path as aiopath
from httpx import AsyncClient
from psutil import (
    Process,
    cpu_percent,
    disk_usage,
    net_io_counters,
    virtual_memory,
)
from pyrogram.enums import ButtonStyle

from .. import LOGGER, bot_cache, bot_start_time
from ..core.config_manager import Config, BinConfig
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import cmd_exec, new_task
from ..helper.ext_utils.status_utils import (
    get_readable_file_size,
    get_readable_time,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)
from ..helper.telegram_helper.tg_ui_formatter import (
    PREMIUM_PACKAGE,
    PREMIUM_PUZZLE,
    PREMIUM_SCREEN,
    PREMIUM_STATS,
    PREMIUM_TRAFFIC,
    format_accordion_block,
    safe_emoji,
)
from ..version import get_version

commands = {
    "aria2": ([BinConfig.ARIA2_NAME, "--version"], r"aria2 version ([\d.]+)"),
    "qBittorrent": ([BinConfig.QBIT_NAME, "--version"], r"qBittorrent v([\d.]+)"),
    "SABnzbd+": (
        [BinConfig.SABNZBD_NAME, "--version"],
        rf"{BinConfig.SABNZBD_NAME}-([\d.]+)",
    ),
    "python": (["python3", "--version"], r"Python ([\d.]+)"),
    "rclone": ([BinConfig.RCLONE_NAME, "--version"], r"rclone v([\d.]+)"),
    "yt-dlp": (["yt-dlp", "--version"], r"([\d.]+)"),
    "ffmpeg": (
        [BinConfig.FFMPEG_NAME, "-version"],
        r"ffmpeg version ([\d.]+(-\w+)?).*",
    ),
    "7z": (["7z", "i"], r"7-Zip\s*(?:\[\d+\])?\s*([\d.]+)"),
    "aiohttp": (["uv", "pip", "show", "aiohttp"], r"Version: ([\d.]+)"),
    "wzgram": (["uv", "pip", "show", "wzgram"], r"Version: ([\d.]+)"),
    "pyrotgfork": (["uv", "pip", "show", "pyrotgfork"], r"Version: ([\d.]+)"),
    "warpcrypto": (["uv", "pip", "show", "warpcrypto"], r"Version: ([\d.]+)"),
    "gapi": (["uv", "pip", "show", "google-api-python-client"], r"Version: ([\d.]+)"),
    "mkvmerge": ([BinConfig.MKVMERGE_NAME, "-V"], r"mkvmerge v([\d.]+)"),  # MKVToolNix binary
}

REPO_ROOT = str(Path(__file__).resolve().parents[2])

_last_net_time = 0.0
_last_net_rx = 0
_last_net_tx = 0
_last_rx_spd = "0 B"
_last_tx_spd = "0 B"


async def get_stats(event, key="home"):
    global _last_net_time, _last_net_rx, _last_net_tx, _last_rx_spd, _last_tx_spd
    user_id = event.from_user.id if getattr(event, "from_user", None) else 0

    now = time()
    uptime = get_readable_time(now - bot_start_time)
    cpu = cpu_percent()
    mem = virtual_memory()
    used_ram_pct = mem.percent
    used_ram = get_readable_file_size(mem.used)
    total_ram = get_readable_file_size(mem.total)
    bot_memory = get_readable_file_size(Process().memory_info().rss)
    _, _, free_d, _ = disk_usage("/")
    free_disk = get_readable_file_size(free_d)

    os_name = "Linux" if "linux" in platform().lower() else platform().split("-")[0]
    kernel_ver = version() or platform()
    net = net_io_counters()
    total_net = get_readable_file_size(net.bytes_recv + net.bytes_sent)

    if _last_net_time > 0 and (now - _last_net_time) >= 0.5:
        dt = now - _last_net_time
        _last_rx_spd = get_readable_file_size(max(0, (net.bytes_recv - _last_net_rx) / dt))
        _last_tx_spd = get_readable_file_size(max(0, (net.bytes_sent - _last_net_tx) / dt))
        _last_net_time = now
        _last_net_rx = net.bytes_recv
        _last_net_tx = net.bytes_sent
    elif _last_net_time == 0:
        _last_net_time = now
        _last_net_rx = net.bytes_recv
        _last_net_tx = net.bytes_sent

    rx_speed = _last_rx_spd
    tx_speed = _last_tx_spd

    last_commit_hash = "N/A"
    last_commit_msg = "N/A"
    if await aiopath.exists(f"{REPO_ROOT}/.git"):
        c_hash = await cmd_exec(["git", "-C", REPO_ROOT, "log", "-1", "--pretty=format:%h"])
        if c_hash[0]:
            last_commit_hash = c_hash[0].strip()
        c_msg = await cmd_exec(["git", "-C", REPO_ROOT, "log", "-1", "--pretty=format:%s"])
        if c_msg[0]:
            last_commit_msg = c_msg[0].strip()

    bot_ver = get_version()

    ver = bot_cache.get("eng_versions", {})
    python_ver = ver.get("python") or "3.12.16"
    aria2_ver = ver.get("aria2") or "1.37.0"
    qbit_ver = ver.get("qBittorrent") or "5.1.4"
    ffmpeg_ver = ver.get("ffmpeg") or "7.1"
    wzgram_ver = ver.get("wzgram") or ver.get("pyrotgfork") or "2.2.24.1"
    try:
        warpcrypto_ver = ver.get("warpcrypto") or importlib.metadata.version("warpcrypto")
    except Exception:
        warpcrypto_ver = "2.1.0"

    leech_limit = "∞" if Config.ALLOW_LARGE_LEECH else (f"{Config.LEECH_LIMIT} GB" if Config.LEECH_LIMIT else "∞")
    torrent_limit = f"{Config.TORRENT_LIMIT} GB" if Config.TORRENT_LIMIT else "∞"
    direct_limit = f"{Config.DIRECT_LIMIT} GB" if Config.DIRECT_LIMIT else "∞"

    bot_icon = safe_emoji(PREMIUM_STATS, "🤖")
    os_icon = safe_emoji(PREMIUM_SCREEN, "🖥️")
    repo_icon = safe_emoji(PREMIUM_PACKAGE, "📦")
    pkgs_icon = safe_emoji(PREMIUM_PUZZLE, "🧩")
    limits_icon = safe_emoji(PREMIUM_TRAFFIC, "🚦")

    bot_stats_block = format_accordion_block(
        f"{bot_icon} CORE RUNTIME",
        "hardware & memory matrix",
        [
            ("Runtime", uptime),
            ("CPU Load", f"{cpu:.1f}%"),
            ("RAM Alloc", f"{used_ram_pct}% ({used_ram} / {total_ram})"),
            ("Process RSS", bot_memory),
            ("NVMe Free", free_disk),
        ],
        curved=True,
    )

    os_stats_block = format_accordion_block(
        f"{os_icon} HOST & NETWORK",
        "node infrastructure",
        [
            ("Platform", os_name),
            ("Kernel", kernel_ver),
            ("Total Bandwidth", total_net),
            ("Transfer Rate", f"↓ {rx_speed}/s • ↑ {tx_speed}/s"),
        ],
        curved=True,
    )

    pkgs_stats_block = format_accordion_block(
        f"{pkgs_icon} ENGINE SUITE",
        "acceleration pipeline",
        [
            ("Python Core", f"v{python_ver}"),
            ("Aria2 Turbo", f"v{aria2_ver}"),
            ("qBittorrent", f"v{qbit_ver}"),
            ("FFmpeg Stack", f"v{ffmpeg_ver}"),
            ("WZGram Client", f"v{wzgram_ver}"),
            ("WarpCrypto", f"v{warpcrypto_ver}"),
        ],
        curved=True,
    )

    limits_block = format_accordion_block(
        f"{limits_icon} TASK POLICIES",
        "execution & concurrency caps",
        [
            ("Leech Cap", leech_limit),
            ("Torrent Cap", torrent_limit),
            ("Direct DL Cap", direct_limit),
            ("Concurrency", "2 DL / 1 UP (VPS Safe)"),
        ],
        curved=True,
    )

    repo_stats_block = format_accordion_block(
        f"{repo_icon} FIRMWARE BUILD",
        "git revision & channel",
        [
            ("Active Branch", "starfallx-v1.2-vps-2core-4gb"),
            ("Commit Hash", last_commit_hash),
            ("Engine Version", bot_ver),
            ("Revision Info", last_commit_msg),
        ],
        curved=True,
    )

    msg = (
        "⚡ <b>StarFallX Neo — System Telemetry</b> ⚡\n"
        "<i>◈ Tap any sector to expand partition metrics ◈</i>\n\n"
        f"{bot_stats_block}\n\n"
        f"{os_stats_block}\n\n"
        f"{pkgs_stats_block}\n\n"
        f"{limits_block}\n\n"
        f"{repo_stats_block}"
    )

    bot_username = getattr(TgClient, "BNAME", "")
    if not bot_username and getattr(TgClient, "bot", None) and getattr(TgClient.bot, "me", None):
        bot_username = TgClient.bot.me.username or ""
    if not bot_username:
        bot_username = "Nothing_leech_robot"
    use_group_url = f"https://t.me/{bot_username}?startgroup=true"

    repo_url = getattr(Config, "UPSTREAM_REPO", "") or "https://github.com/mari8438/WZML-X"
    if repo_url.startswith("git@github.com:"):
        repo_url = "https://github.com/" + repo_url.split(":", 1)[1]
    repo_url = repo_url.removesuffix(".git").rstrip("/")

    btns = ButtonMaker()
    btns.url_button("✦ Add to Group", use_group_url)
    btns.url_button("📡 Live Monitor", f"https://t.me/{bot_username}")
    btns.url_button("📦 Source", repo_url)
    btns.data_button("✖ Dismiss Panel", f"stats {user_id} close", position="footer", style=ButtonStyle.DANGER)

    return msg, btns.build_menu(b_cols=3, f_cols=1)


@new_task
async def bot_stats(_, message):
    msg, btns = await get_stats(message)
    await send_message(message, msg, btns, photo="IMAGES")


@new_task
async def stats_pages(_, query):
    data = query.data.split()
    message = query.message
    user_id = query.from_user.id
    if user_id != int(data[1]):
        await query.answer("Not Yours!", show_alert=True)
    elif data[2] == "close":
        await query.answer()
        await delete_message(message, message.reply_to_message)
    else:
        await query.answer()
        msg, btns = await get_stats(query, data[2])
        await edit_message(message, msg, btns)


async def get_version_async(command, regex, timeout=25):
    try:
        out, err, code = await wait_for(cmd_exec(command), timeout=timeout)
        if code != 0:
            return f"Error: {err}"
        match = research(regex, out)
        return match.group(1) if match else "-"
    except TimeoutError:
        return "Timeout"
    except Exception as e:
        return f"Exception: {str(e)}"


@new_task
async def get_packages_version():
    tasks = [get_version_async(command, regex) for command, regex in commands.values()]
    versions = await gather(*tasks)
    bot_cache["eng_versions"] = {}
    for tool, ver in zip(commands.keys(), versions):
        bot_cache["eng_versions"][tool] = ver

    try:
        for key, pkg_name in [
            ("yt-dlp", "yt-dlp"),
            ("aiohttp", "aiohttp"),
            ("pyrotgfork", "pyrotgfork"),
            ("warpcrypto", "warpcrypto"),
            ("gapi", "google-api-python-client"),
        ]:
            try:
                pkg_ver = importlib.metadata.version(pkg_name)
                if pkg_ver:
                    bot_cache["eng_versions"][key] = pkg_ver
            except Exception:
                pass
    except Exception:
        pass
    if await aiopath.exists(".git"):
        last_commit = await cmd_exec(
            "git log -1 --date=short --pretty=format:'%cd <b>From</b> %cr'", True
        )
        last_commit = last_commit[0]
    else:
        last_commit = "No UPSTREAM_REPO"
    bot_cache["commit"] = last_commit

    LOGGER.info("Fetched Package Versions!")
