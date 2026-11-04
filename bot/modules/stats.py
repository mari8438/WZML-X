from asyncio import gather, sleep, wait_for, TimeoutError
from pyrogram.enums import ButtonStyle
from platform import platform, version
from pathlib import Path
from re import search as research
from time import time

from aiofiles.os import path as aiopath
from httpx import AsyncClient
from psutil import (
    Process,
    boot_time,
    cpu_count,
    cpu_freq,
    cpu_percent,
    disk_io_counters,
    disk_usage,
    getloadavg,
    net_io_counters,
    swap_memory,
    virtual_memory,
    process_iter,
    NoSuchProcess,
    AccessDenied,
)

from .. import LOGGER, bot_cache, bot_start_time, bot_loop
from ..core.config_manager import Config, BinConfig
from ..helper.ext_utils.bot_lock import get_system_resources_cached
from ..helper.ext_utils.bot_utils import cmd_exec, compare_versions, new_task
from ..helper.ext_utils.status_utils import (
    get_progress_bar_string,
    get_readable_file_size,
    get_readable_time,
)
from ..helper.telegram_helper.filters import CustomFilters
from ..helper.telegram_helper.button_build import ButtonMaker
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
    "gapi": (["uv", "pip", "show", "google-api-python-client"], r"Version: ([\d.]+)"),
    "mkvmerge": ([BinConfig.MKVMERGE_NAME, "-V"], r"mkvmerge v([\d.]+)"),
}

REPO_ROOT = str(Path(__file__).resolve().parents[2])


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
            return "v" + ".".join(values[key].group(1) for key in ("MAJOR", "MINOR", "PATCH")) + suffix
        direct = research(r'v\d+(?:\.\d+){2}(?:-[\w.]+)?', response.text)
        return direct.group(0) if direct else None
    except Exception as error:
        LOGGER.warning(f"Repo version lookup failed: {error}")
        return None


async def get_stats(event, key="home"):
    user_id = event.from_user.id
    btns = ButtonMaker()
    if key == "home":
        btns = ButtonMaker()
        b_icon = safe_emoji(PREMIUM_STATS, "🤖")
        s_icon = safe_emoji(PREMIUM_BOLT, "💻")
        r_icon = safe_emoji(PREMIUM_PACKAGE, "📦")
        p_icon = safe_emoji(PREMIUM_GEAR, "⚙️")
        l_icon = safe_emoji(PREMIUM_TIMER, "⚡")
        t_icon = safe_emoji(PREMIUM_USER, "📊")

        btns.data_button(f"{b_icon} Bot Stats", f"stats {user_id} stbot")
        btns.data_button(f"{s_icon} OS Stats", f"stats {user_id} stsys")
        btns.data_button(f"{r_icon} Repo Stats", f"stats {user_id} strepo")
        btns.data_button(f"{p_icon} Pkgs Stats", f"stats {user_id} stpkgs")
        btns.data_button(f"{l_icon} Task Limits", f"stats {user_id} tlimits")
        btns.data_button(f"{t_icon} Sys Tasks", f"stats {user_id} systasks")

        total, used, free, disk = disk_usage("/")
        memory = virtual_memory()
        uptime_str = get_readable_time(time() - bot_start_time)

        msg = f"""<b>{safe_emoji(PREMIUM_BOLT, '✦')} 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗦𝗬𝗦𝗧𝗘𝗠 𝗠𝗢𝗡𝗜𝗧𝗢𝗥 {safe_emoji(PREMIUM_BOLT, '✦')}</b>

<blockquote expandable>
<b>System & Host Health Overview:</b>

┌ <b>Bot Uptime</b> : {uptime_str}
├ <b>RAM Usage</b> : {get_progress_bar_string(memory.percent)} {memory.percent}%
├ <b>Disk Space</b> : {get_readable_file_size(free)} free of {get_readable_file_size(total)}
└ <b>OS Info</b> : {platform()}

<i>Select a section below for detailed performance metrics.</i>
</blockquote>"""
    elif key == "stbot":
        total, used, free, disk = disk_usage("/")
        swap = swap_memory()
        memory = virtual_memory()
        disk_io = disk_io_counters()
        res = get_system_resources_cached()
        bot_ram_mb = res["ram_mb"]
        bot_ram_total = bot_ram_mb * 1024 * 1024
        user = Process().username()
        bot_ram_used = sum(
            p.memory_info().rss for p in process_iter() if p.username() == user
        )
        bot_ram_free = max(0, bot_ram_total - bot_ram_used)
        bot_ram_pct = (
            round((bot_ram_used / bot_ram_total * 100), 2) if bot_ram_total > 0 else 0
        )
        instance_cpu = res["cpu_count"]
        sys_cpu = cpu_count(logical=True)
        p_cores = cpu_count(logical=False)
        v_cores = (sys_cpu or 0) - (p_cores or 0)
        msg = f"""<b>{safe_emoji(PREMIUM_STATS, '🤖')} 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗜𝗦𝗧𝗜𝗖𝗦</b>

<blockquote expandable>
<b>Bot Instance & Runtime:</b>
┌ <b>Bot Uptime</b> : {get_readable_time(time() - bot_start_time)}
├ <b>Instance Cores</b> : {instance_cpu} | <b>Usable</b> : {len(Process().cpu_affinity())}
└ <b>Total Cores</b> : {sys_cpu} (P: {p_cores} | V: {v_cores})

<b>Instance Memory (Bot):</b>
┌ <b>Usage</b> : {get_progress_bar_string(bot_ram_pct)} {bot_ram_pct}%
└ <b>U</b>: {get_readable_file_size(bot_ram_used)} | <b>F</b>: {get_readable_file_size(bot_ram_free)} | <b>T</b>: {get_readable_file_size(bot_ram_total)}

<b>Host RAM:</b>
┌ <b>Usage</b> : {get_progress_bar_string(memory.percent)} {memory.percent}%
└ <b>U</b>: {get_readable_file_size(memory.used)} | <b>F</b>: {get_readable_file_size(memory.available)} | <b>T</b>: {get_readable_file_size(memory.total)}

<b>Swap Memory:</b>
┌ <b>Usage</b> : {get_progress_bar_string(swap.percent)} {swap.percent}%
└ <b>U</b>: {get_readable_file_size(swap.used)} | <b>F</b>: {get_readable_file_size(swap.free)} | <b>T</b>: {get_readable_file_size(swap.total)}

<b>Disk Storage (/):</b>
┌ <b>Usage</b> : {get_progress_bar_string(disk)} {disk}%
├ <b>Disk Read</b> : {f"{get_readable_file_size(disk_io.read_bytes)} ({get_readable_time(disk_io.read_time / 1000)})" if disk_io else "Access Denied"}
├ <b>Disk Write</b> : {f"{get_readable_file_size(disk_io.write_bytes)} ({get_readable_time(disk_io.write_time / 1000)})" if disk_io else "Access Denied"}
└ <b>U</b>: {get_readable_file_size(used)} | <b>F</b>: {get_readable_file_size(free)} | <b>T</b>: {get_readable_file_size(total)}
</blockquote>"""
    elif key == "stsys":
        cpu_usage = cpu_percent(interval=0.5)
        sys_cpu = cpu_count(logical=True)
        p_cores = cpu_count(logical=False)
        v_cores = (sys_cpu or 0) - (p_cores or 0)
        msg = f"""<b>{safe_emoji(PREMIUM_BOLT, '💻')} 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗢𝗦 & 𝗡𝗘𝗧𝗪𝗢𝗥𝗞</b>

<blockquote expandable>
<b>Host Operating System:</b>
┌ <b>OS Uptime</b> : {get_readable_time(time() - boot_time())}
├ <b>OS Version</b> : {version()}
└ <b>OS Arch</b> : {platform()}

<b>Network Traffic:</b>
┌ <b>Upload Data</b> : {get_readable_file_size(net_io_counters().bytes_sent)}
├ <b>Download Data</b> : {get_readable_file_size(net_io_counters().bytes_recv)}
├ <b>Packets Sent</b> : {str(net_io_counters().packets_sent)[:-3]}k
├ <b>Packets Recv</b> : {str(net_io_counters().packets_recv)[:-3]}k
└ <b>Total I/O</b> : {get_readable_file_size(net_io_counters().bytes_recv + net_io_counters().bytes_sent)}

<b>Processor Performance:</b>
┌ <b>CPU Load</b> : {get_progress_bar_string(cpu_usage)} {cpu_usage}%
├ <b>Frequency</b> : {f"{cpu_freq().current / 1000:.2f} GHz" if cpu_freq() else "Access Denied"}
├ <b>Avg Load (1m, 5m, 15m)</b> : {"%, ".join(str(round((x / (cpu_count() or 1) * 100), 2)) for x in getloadavg())}%
└ <b>Cores</b> : {sys_cpu} Total ({p_cores} Physical, {v_cores} Virtual)
</blockquote>"""
    elif key == "strepo":
        last_commit, changelog = "No Data", "N/A"
        if await aiopath.exists(f"{REPO_ROOT}/.git"):
            last_commit = (
                await cmd_exec(
                    [
                        "git", "-C", REPO_ROOT, "log", "-1",
                        "--pretty=%cd ( %cr )", "--date=format-local:%d/%m/%Y",
                    ],
                )
            )[0]
            changelog = (
                await cmd_exec(
                    [
                        "git", "-C", REPO_ROOT, "log", "-1",
                        "--pretty=format:<code>%s</code> <b>By</b> %an",
                    ]
                )
            )[0]
        official_v = await _remote_repo_version()
        latest_label = official_v or "Unavailable"
        remarks = (
            compare_versions(get_version(), official_v)
            if official_v
            else "Remote version is unavailable; try again later."
        )
        msg = f"""<b>{safe_emoji(PREMIUM_PACKAGE, '📦')} 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗥𝗘𝗣𝗢 𝗦𝗧𝗔𝗧𝗨𝗦</b>

<blockquote expandable>
<b>Repository & Versioning:</b>
┌ <b>Bot Updated</b> : {last_commit}
├ <b>Installed Version</b> : <code>{get_version()}</code>
├ <b>Latest Version</b> : <code>{latest_label}</code>
└ <b>Last ChangeLog</b> : {changelog}

<b>Status Remarks</b> : <code>{remarks}</code>
</blockquote>"""
    elif key == "stpkgs":
        ver = bot_cache.get("eng_versions", {})
        msg = f"""<b>{safe_emoji(PREMIUM_GEAR, '⚙️')} 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗣𝗔𝗖𝗞𝗔𝗚𝗘 𝗩𝗘𝗥𝗦𝗜𝗢𝗡𝗦</b>

<blockquote expandable>
<b>Core Engines & Binaries:</b>
┌ <b>Python</b> : v{ver.get("python", "N/A")}
├ <b>qBittorrent</b> : v{ver.get("qBittorrent", "N/A")}
├ <b>aria2</b> : v{ver.get("aria2", "N/A")}
├ <b>FFmpeg</b> : v{ver.get("ffmpeg", "N/A")}
├ <b>MKVToolNix</b> : v{ver.get("mkvmerge", "N/A")}
├ <b>SABnzbd+</b> : v{ver.get("SABnzbd+", "N/A")}
├ <b>rclone</b> : v{ver.get("rclone", "N/A")}
├ <b>yt-dlp</b> : v{ver.get("yt-dlp", "N/A")}
├ <b>7-Zip</b> : v{ver.get("7z", "N/A")}
├ <b>WZGram</b> : v{ver.get("wzgram", ver.get("pyrotgfork", "N/A"))}
├ <b>Aiohttp</b> : v{ver.get("aiohttp", "N/A")}
└ <b>Google API</b> : v{ver.get("gapi", "N/A")}
</blockquote>"""
    elif key == "tlimits":
        msg = f"""<b>{safe_emoji(PREMIUM_TIMER, '⚡')} 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗧𝗔𝗦𝗞 𝗟𝗜𝗠𝗜𝗧𝗦</b>

<blockquote expandable>
<b>Traffic & Download Ceilings:</b>
┌ <b>Direct Limit</b> : {Config.DIRECT_LIMIT or "∞"} GB
├ <b>Torrent Limit</b> : {Config.TORRENT_LIMIT or "∞"} GB
├ <b>Leech Limit</b> : {"∞" if Config.ALLOW_LARGE_LEECH else (Config.LEECH_LIMIT or "∞")} GB
├ <b>GDrive DL</b> : {Config.GD_DL_LIMIT or "∞"} GB
├ <b>RClone DL</b> : {Config.RC_DL_LIMIT or "∞"} GB
├ <b>Clone Limit</b> : {Config.CLONE_LIMIT or "∞"} GB
├ <b>JDown Limit</b> : {Config.JD_LIMIT or "∞"} GB
├ <b>NZB Limit</b> : {Config.NZB_LIMIT or "∞"} GB
├ <b>YT-DLP Limit</b> : {Config.YTDLP_LIMIT or "∞"} GB
├ <b>Playlist Limit</b> : {Config.PLAYLIST_LIMIT or "∞"}
├ <b>Archive Limit</b> : {Config.ARCHIVE_LIMIT or "∞"} GB
├ <b>Extract Limit</b> : {Config.EXTRACT_LIMIT or "∞"} GB
└ <b>Threshold Storage</b> : {Config.STORAGE_LIMIT or "∞"} GB

<b>Concurrency & Intervals:</b>
┌ <b>Token Validity</b> : {get_readable_time(Config.VERIFY_TIMEOUT) if Config.VERIFY_TIMEOUT else "Disabled"}
├ <b>User Time Limit</b> : {Config.USER_TIME_INTERVAL or "0"}s / task
├ <b>User Max Tasks</b> : {Config.USER_MAX_TASKS or "∞"}
└ <b>Bot Max Tasks</b> : {Config.BOT_MAX_TASKS or "∞"}
</blockquote>"""

    elif key == "systasks":
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

        msg = f"""<b>{safe_emoji(PREMIUM_USER, '📊')} 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗦𝗬𝗦𝗧𝗘𝗠 𝗧𝗔𝗦𝗞𝗦</b>

<blockquote expandable>
<b>Top Resource Consuming Processes:</b>\n\n"""

        if processes:
            for i, proc in enumerate(processes, 1):
                name = proc.get("name", "Unknown")[:20]
                cpu = proc.get("cpu_percent", 0)
                mem = proc.get("memory_percent", 0)
                user = proc.get("username", "Unknown")[:10]
                msg += f"┌ <b>{i:2d}.</b> <code>{name}</code>\n├ 🔹 <b>CPU:</b> {cpu:.1f}% | <b>MEM:</b> {mem:.1f}%\n└ 👤 <b>User:</b> {user} | <b>PID:</b> {proc['pid']}\n\n"
                btns.data_button(f"{i}", f"stats {user_id} killproc {proc['pid']}")
            msg += "<i>Click serial number to terminate process.</i>\n</blockquote>"
        else:
            msg += "<i>No high usage processes found.</i>\n</blockquote>"

        btns.data_button("🔄 Refresh", f"stats {user_id} systasks", "header")

    if key != "home":
        btns.data_button("🔙 Back", f"stats {user_id} home", "footer")
    btns.data_button(
        "❌ Close", f"stats {user_id} close", "footer", style=ButtonStyle.DANGER
    )
    return msg, btns.build_menu(8 if key == "systasks" else 2)


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
        import importlib.metadata
        for key, pkg_name in [
            ("yt-dlp", "yt-dlp"),
            ("aiohttp", "aiohttp"),
            ("pyrotgfork", "pyrotgfork"),
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
