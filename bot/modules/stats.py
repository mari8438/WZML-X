from asyncio import gather, sleep, wait_for, TimeoutError
from html import escape
from pathlib import Path
from platform import machine, platform, python_version, release, system, version
from re import search as research
from time import time

from aiofiles.os import path as aiopath
from httpx import AsyncClient
from psutil import (
    AccessDenied,
    NoSuchProcess,
    Process,
    cpu_count,
    cpu_percent,
    disk_usage,
    process_iter,
    virtual_memory,
)
from pyrogram.enums import ButtonStyle

from .. import LOGGER, bot_cache, bot_start_time, non_queued_dl, non_queued_up, queued_dl, queued_up
from ..core.config_manager import BinConfig, Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import cmd_exec, new_task
from ..helper.ext_utils.status_utils import (
    _network_status,
    get_readable_file_size,
    get_readable_time,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.filters import CustomFilters
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)
from ..helper.telegram_helper.tg_ui_formatter import (
    PREMIUM_BOLT,
    PREMIUM_GEAR,
    PREMIUM_PACKAGE,
    PREMIUM_STATS,
    PREMIUM_TIMER,
    PREMIUM_USER,
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
    "mkvmerge": ([BinConfig.MKVMERGE_NAME, "-V"], r"mkvmerge v([\d.]+)"),
}

REPO_ROOT = str(Path(__file__).resolve().parents[2])


def _get_os_name() -> str:
    try:
        os_rel = Path("/etc/os-release")
        if os_rel.is_file():
            for line in os_rel.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("PRETTY_NAME="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    except Exception:
        pass
    sys_name = f"{system()} {machine()}".strip()
    return sys_name or platform()


def _get_pkg_version(pkg_name: str, fallback: str = "N/A") -> str:
    try:
        import importlib.metadata

        ver = importlib.metadata.version(pkg_name)
        if ver:
            return ver
    except Exception:
        pass
    return fallback


async def _remote_repo_version():
    repo = str(getattr(Config, "UPSTREAM_REPO", "") or "").strip()
    if not repo and await aiopath.exists(".git"):
        repo = (await cmd_exec(["git", "remote", "get-url", "origin"]))[0].strip()
    branch = str(getattr(Config, "UPSTREAM_BRANCH", "") or "master").strip()
    if repo.startswith("git@github.com:"):
        repo = "https://github.com/" + repo.split(":", 1)[1]
    repo = repo.removesuffix(".git").rstrip("/")
    if "github.com/" not in repo:
        return None
    slug = repo.split("github.com/", 1)[1]
    url = f"https://raw.githubusercontent.com/{slug}/{branch}/bot/version.py"
    try:
        async with AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        values = {
            key: research(rf'{key}\s*=\s*["\']([^"\']+)', response.text)
            for key in ("MAJOR", "MINOR", "PATCH", "STATE")
        }
        if all(values[key] for key in ("MAJOR", "MINOR", "PATCH")):
            state = values["STATE"].group(1) if values["STATE"] else ""
            suffix = f"-{state}" if state else ""
            return (
                "v"
                + ".".join(values[key].group(1) for key in ("MAJOR", "MINOR", "PATCH"))
                + suffix
            )
        direct = research(r"v\d+(?:\.\d+){2}(?:-[\w.]+)?", response.text)
        return direct.group(0) if direct else None
    except Exception as error:
        LOGGER.warning(f"Repo version lookup failed: {error}")
        return None


def _build_stats_buttons(user_id: int):
    btns = ButtonMaker()
    bot_username = getattr(TgClient, "BNAME", "") or ""
    group_url = (
        f"https://t.me/{bot_username}?startgroup=true"
        if bot_username
        else (getattr(Config, "AUTHOR_URL", "") or "https://t.me/Anime_Starfall")
    )
    dev_url = getattr(Config, "AUTHOR_URL", "") or "https://t.me/Anime_Starfall"
    source_url = (
        getattr(Config, "UPSTREAM_REPO", "") or "https://github.com/mari8438/WZML-X"
    )

    btns.url_button("👥 USE BOT IN GROUP", group_url, style=ButtonStyle.SUCCESS)
    btns.url_button("👤 DEVELOPER ↗", dev_url, style=ButtonStyle.DANGER)
    btns.url_button("📦 SOURCE ↗", source_url, style=ButtonStyle.PRIMARY)
    btns.data_button(
        "🔄 Refresh", f"stats {user_id} home", "footer", style=ButtonStyle.PRIMARY
    )
    btns.data_button(
        "📊 Processes", f"stats {user_id} systasks", "footer", style=ButtonStyle.SUCCESS
    )
    btns.data_button(
        "❌ Close", f"stats {user_id} close", "l_body", style=ButtonStyle.DANGER
    )
    return btns.build_menu(b_cols=3, f_cols=2, lb_cols=1)


async def get_stats(event, key="home"):
    user_id = event.from_user.id
    if key == "systasks":
        btns = ButtonMaker()
        try:
            processes = []
            for proc in process_iter(
                ["pid", "name", "cpu_percent", "memory_percent", "username"]
            ):
                try:
                    info = proc.info
                    if (
                        info.get("cpu_percent", 0) > 1.0
                        or info.get("memory_percent", 0) > 1.0
                    ):
                        processes.append(info)
                except (NoSuchProcess, AccessDenied):
                    continue
            processes.sort(
                key=lambda x: x.get("cpu_percent", 0) + x.get("memory_percent", 0),
                reverse=True,
            )
            processes = processes[:15]
        except Exception:
            processes = []

        t_icon = safe_emoji(PREMIUM_USER, "📊")
        msg = (
            f"<b>{t_icon} StarFallX — Process Monitor</b>\n"
            f"<i>Tap a process number below to terminate it.</i>\n\n"
            f"<blockquote expandable><b>{t_icon} ACTIVE PROCESSES</b> · top CPU & RAM consumers\n"
        )
        if processes:
            for i, proc in enumerate(processes, 1):
                name = escape(str(proc.get("name", "Unknown"))[:20])
                cpu_val = proc.get("cpu_percent", 0)
                mem_val = proc.get("memory_percent", 0)
                user = escape(str(proc.get("username", "Unknown"))[:10])
                msg += (
                    f"╭ <b>{i:2d}.</b> <code>{name}</code> (PID <code>{proc['pid']}</code>)\n"
                    f"╰ <b>CPU:</b> <code>{cpu_val:.1f}%</code> · <b>MEM:</b> <code>{mem_val:.1f}%</code> · <b>User:</b> <code>{user}</code>\n"
                )
                btns.data_button(f"{i}", f"stats {user_id} killproc {proc['pid']}")
            msg += "<i>Owner can tap a serial button below to kill the process.</i></blockquote>"
        else:
            msg += "<i>No high-usage processes detected.</i></blockquote>"

        btns.data_button(
            "🔄 Refresh", f"stats {user_id} systasks", "header", style=ButtonStyle.SUCCESS
        )
        btns.data_button(
            "🔙 Back to Stats", f"stats {user_id} home", "footer", style=ButtonStyle.PRIMARY
        )
        btns.data_button(
            "❌ Close", f"stats {user_id} close", "footer", style=ButtonStyle.DANGER
        )
        return msg, btns.build_menu(8, f_cols=2)

    total, used, free, disk_pct = disk_usage("/")
    memory = virtual_memory()
    uptime = get_readable_time(time() - bot_start_time) or "0s"
    cpu_pct = cpu_percent(interval=0.1)
    cores = cpu_count(logical=True) or 2
    used_ram_percent = memory.percent
    used_ram = get_readable_file_size(memory.used)
    total_ram = get_readable_file_size(memory.total)
    try:
        bot_memory = get_readable_file_size(Process().memory_info().rss)
    except Exception:
        bot_memory = "N/A"
    free_disk = get_readable_file_size(free)
    total_disk = get_readable_file_size(total)

    os_name = escape(_get_os_name())
    kernel_version = escape(release() or version() or "N/A")
    total_net_bytes, sent_rate, recv_rate = _network_status()
    total_net = get_readable_file_size(total_net_bytes)
    rx_speed = get_readable_file_size(recv_rate)
    tx_speed = get_readable_file_size(sent_rate)

    last_commit_hash = bot_cache.get("commit_hash", "N/A")
    last_commit_message = bot_cache.get("commit_msg", "N/A")
    if await aiopath.exists(f"{REPO_ROOT}/.git"):
        out_hash = (
            await cmd_exec(["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"])
        )[0].strip()
        if out_hash:
            last_commit_hash = escape(out_hash)
        out_msg = (
            await cmd_exec(
                ["git", "-C", REPO_ROOT, "log", "-1", "--pretty=format:%s"]
            )
        )[0].strip()
        if out_msg:
            last_commit_message = escape(out_msg)
    bot_version = escape(str(get_version()))

    ver = bot_cache.get("eng_versions", {})
    python_ver = escape(str(ver.get("python") or python_version()))
    aria2_ver = escape(str(ver.get("aria2") or "N/A"))
    qbit_ver = escape(str(ver.get("qBittorrent") or "N/A"))
    ffmpeg_ver = escape(str(ver.get("ffmpeg") or "N/A"))
    mkvmerge_ver = escape(str(ver.get("mkvmerge") or "N/A"))
    wzgram_ver = escape(
        str(
            ver.get("wzgram")
            or ver.get("pyrotgfork")
            or _get_pkg_version("wzgram", _get_pkg_version("pyrotgfork", "N/A"))
        )
    )
    warpcrypto_ver = escape(
        str(ver.get("warpcrypto") or _get_pkg_version("warpcrypto", "N/A"))
    )

    leech_limit = (
        "∞ (Auto-Split)"
        if getattr(Config, "ALLOW_LARGE_LEECH", False)
        or not getattr(Config, "LEECH_LIMIT", 0)
        else f"{Config.LEECH_LIMIT} GB"
    )
    torrent_limit = (
        f"{Config.TORRENT_LIMIT} GB"
        if getattr(Config, "TORRENT_LIMIT", 0)
        else "∞"
    )
    direct_limit = (
        f"{Config.DIRECT_LIMIT} GB"
        if getattr(Config, "DIRECT_LIMIT", 0)
        else "∞"
    )
    active_count = len(non_queued_dl) + len(non_queued_up)
    queued_count = len(queued_dl) + len(queued_up)

    bot_icon = safe_emoji(PREMIUM_STATS, "🤖")
    os_icon = safe_emoji(PREMIUM_BOLT, "🖥️")
    repo_icon = safe_emoji(PREMIUM_PACKAGE, "📦")
    pkgs_icon = safe_emoji(PREMIUM_GEAR, "🧩")
    limits_icon = safe_emoji(PREMIUM_TIMER, "🚦")

    msg = (
        f"<b>StarFallX — Statistics</b>\n"
        f"<i>Tap a group to open it.</i>\n\n"
        f"<blockquote expandable><b>{bot_icon} BOT STATS</b> · uptime, RAM, disk\n"
        f"╭ <b>Uptime:</b> <code>{uptime}</code>\n"
        f"├ <b>CPU:</b> <code>{cpu_pct}%</code> ({cores} vCPU)\n"
        f"├ <b>RAM:</b> <code>{used_ram_percent}%</code> ({used_ram} / {total_ram})\n"
        f"├ <b>Bot RSS:</b> <code>{bot_memory}</code>\n"
        f"╰ <b>Free Disk:</b> <code>{free_disk}</code> / <code>{total_disk}</code> ({100 - disk_pct:.1f}% free)</blockquote>\n\n"
        f"<blockquote expandable><b>{os_icon} OS STATS</b> · system and network\n"
        f"╭ <b>OS:</b> <code>{os_name}</code>\n"
        f"├ <b>Kernel:</b> <code>{kernel_version}</code>\n"
        f"├ <b>Total Net:</b> <code>{total_net}</code>\n"
        f"╰ <b>Current Speed:</b> <code>↓ {rx_speed}/s</code> | <code>↑ {tx_speed}/s</code></blockquote>\n\n"
        f"<blockquote expandable><b>{repo_icon} REPO STATS</b> · commits and branch\n"
        f"╭ <b>Branch:</b> <code>starfallx-v1.2-vps-2core-4gb</code>\n"
        f"├ <b>Commit:</b> <code>{last_commit_hash}</code>\n"
        f"├ <b>Version:</b> <code>{bot_version}</code>\n"
        f"╰ <b>Changelog:</b> <i>{last_commit_message}</i></blockquote>\n\n"
        f"<blockquote expandable><b>{pkgs_icon} PKGS STATS</b> · engine versions\n"
        f"╭ <b>Python:</b> <code>{python_ver}</code>\n"
        f"├ <b>Aria2:</b> <code>{aria2_ver}</code>\n"
        f"├ <b>qBittorrent:</b> <code>{qbit_ver}</code>\n"
        f"├ <b>FFmpeg:</b> <code>{ffmpeg_ver}</code> · <b>MKVMerge:</b> <code>{mkvmerge_ver}</code>\n"
        f"├ <b>WZGram:</b> <code>{wzgram_ver}</code>\n"
        f"╰ <b>WarpCrypto:</b> <code>{warpcrypto_ver}</code></blockquote>\n\n"
        f"<blockquote expandable><b>{limits_icon} TASK LIMITS</b> · per-task caps\n"
        f"╭ <b>Leech Limit:</b> <code>{leech_limit}</code>\n"
        f"├ <b>Torrent Limit:</b> <code>{torrent_limit}</code>\n"
        f"├ <b>Direct Limit:</b> <code>{direct_limit}</code>\n"
        f"╰ <b>Max Parallel:</b> <code>2 DL / 1 UP</code> ({active_count} active, {queued_count} queued)</blockquote>"
    )
    return msg, _build_stats_buttons(user_id)


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
    elif data[2] == "killproc":
        if not await CustomFilters.owner(_, query):
            await query.answer("Sorry! You cannot Kill System Tasks!", show_alert=True)
            return
        pid = int(data[3])
        try:
            process = Process(pid)
            proc_name = process.name()
            process.terminate()
            await sleep(2)
            if process.is_running():
                process.kill()
                status = "🔥 Force killed"
            else:
                status = "✅ Terminated"
            await query.answer(f"{status}: {proc_name} (PID: {pid})", show_alert=True)
        except NoSuchProcess:
            await query.answer(
                "❌ Process not found or already terminated!", show_alert=True
            )
        except AccessDenied:
            await query.answer(
                "❌ Access denied! Cannot kill this process.", show_alert=True
            )
        except Exception as e:
            await query.answer(f"❌ Error: {str(e)}", show_alert=True)

        msg, btns = await get_stats(query, "systasks")
        await edit_message(message, msg, btns)
    else:
        if data[2] == "systasks" and not await CustomFilters.sudo(_, query):
            await query.answer("Sorry! You cannot open System Tasks!", show_alert=True)
            return
        await query.answer("Refreshing statistics...")
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
        import importlib.metadata

        for key, pkg_name in [
            ("yt-dlp", "yt-dlp"),
            ("aiohttp", "aiohttp"),
            ("wzgram", "wzgram"),
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
        commit_hash = (
            await cmd_exec(["git", "rev-parse", "--short", "HEAD"])
        )[0].strip()
        commit_msg = (
            await cmd_exec(["git", "log", "-1", "--pretty=format:%s"])
        )[0].strip()
        if commit_hash:
            bot_cache["commit_hash"] = escape(commit_hash)
        if commit_msg:
            bot_cache["commit_msg"] = escape(commit_msg)
    else:
        last_commit = "No UPSTREAM_REPO"
    bot_cache["commit"] = last_commit

    LOGGER.info("Fetched Package Versions!")
