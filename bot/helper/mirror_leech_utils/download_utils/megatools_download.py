import asyncio
import re
from contextlib import suppress
from secrets import token_hex
from time import time

from aiofiles.os import makedirs, listdir
from aiofiles.os import path as aiopath

from .... import LOGGER, task_dict, task_dict_lock
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.task_manager import (
    check_running_tasks,
    limit_checker,
    stop_duplicate_check,
)
from ..status_utils.megatools_status import MegaToolsStatus
from ..status_utils.queue_status import QueueStatus
from ...telegram_helper.message_utils import send_status_message

PROGRESS_PATTERN = re.compile(
    r"^(?P<name>.+?):\s+(?P<pct>[\d.]+)%\s+-\s+(?P<p_str>[^\(]+)\s+\((?P<p_bytes>\d+)\s+bytes\)\s+of\s+(?P<t_str>[^\(\n]+?)(?:\s+\((?P<speed>[^\)]+)\))?$"
)


class MegaToolsDownloader:
    def __init__(self, listener, path):
        self.listener = listener
        self.path = path
        self.name = listener.name or ""
        self.percentage = "0%"
        self.processed_bytes = 0
        self.processed_size_str = "0B"
        self.total_size_str = ""
        self.speed = "0B/s"
        self.eta = "-"
        self.gid = token_hex(5)
        self._process = None
        self._is_cancelled = False
        self._start_time = time()

    def progress_raw(self):
        try:
            return float(self.percentage.rstrip("%"))
        except Exception:
            return 0.0

    async def cancel_task(self):
        self._is_cancelled = True
        LOGGER.info(f"Cancelling Mega download: {self.name or self.listener.link}")
        if self._process:
            with suppress(Exception):
                self._process.kill()
        await self.listener.on_download_error("Download cancelled by user!")

    async def download(self):
        link = self.listener.link.strip()
        await makedirs(self.path, exist_ok=True)

        cmd = ["megatools", "dl", "--path", self.path, link]
        LOGGER.info(f"Starting Mega download via megatools: {link}")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            LOGGER.error(f"Failed to start megatools: {e}")
            await self.listener.on_download_error(f"Failed to start megatools: {e}")
            return

        error_lines = []
        while True:
            line_bytes = await self._process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            match = PROGRESS_PATTERN.search(line)
            if match:
                data = match.groupdict()
                file_name = data.get("name", "").strip()
                if file_name and not self.name:
                    self.name = file_name
                    if not self.listener.name:
                        self.listener.name = file_name

                pct_val = float(data.get("pct", 0.0))
                self.percentage = f"{pct_val:.2f}%"
                self.processed_size_str = data.get("p_str", "").strip()
                self.total_size_str = data.get("t_str", "").strip()
                self.processed_bytes = int(data.get("p_bytes", 0) or 0)
                sp = data.get("speed")
                if sp:
                    self.speed = sp.strip()

                # Calculate rough ETA if possible
                try:
                    if pct_val > 0:
                        elapsed = time() - self._start_time
                        total_expected = (elapsed / pct_val) * 100
                        rem = max(0, total_expected - elapsed)
                        mins, secs = divmod(int(rem), 60)
                        hrs, mins = divmod(mins, 60)
                        if hrs > 0:
                            self.eta = f"{hrs}h {mins}m"
                        elif mins > 0:
                            self.eta = f"{mins}m {secs}s"
                        else:
                            self.eta = f"{secs}s"
                except Exception:
                    pass
            elif line.startswith("ERROR:"):
                error_lines.append(line)
                LOGGER.warning(f"megatools: {line}")
            elif line.startswith("Downloaded "):
                LOGGER.info(f"megatools: {line}")

        ret_code = await self._process.wait()
        if self._is_cancelled:
            return

        if ret_code != 0:
            err_msg = "\n".join(error_lines) if error_lines else f"megatools exited with code {ret_code}"
            LOGGER.error(f"Mega download failed: {err_msg}")
            await self.listener.on_download_error(err_msg)
            return

        # Check that downloaded content exists
        if not await aiopath.exists(self.path):
            await self.listener.on_download_error("Download finished but directory does not exist!")
            return

        items = await listdir(self.path)
        if not items:
            await self.listener.on_download_error("Download finished but no files were found!")
            return

        if not self.listener.name:
            self.listener.name = items[0]

        LOGGER.info(f"Mega download completed successfully: {self.path}")
        await self.listener.on_download_complete()


async def add_mega_download(listener, path):
    downloader = MegaToolsDownloader(listener, path)
    gid = downloader.gid

    msg, button = await stop_duplicate_check(listener)
    if msg:
        await listener.on_download_error(msg, button)
        return

    if limit_exceeded := await limit_checker(listener):
        await listener.on_download_error(limit_exceeded, is_limit=True)
        return

    add_to_queue, event = await check_running_tasks(listener)
    if add_to_queue:
        LOGGER.info(f"Added to Queue/Download: {listener.name or listener.link}")
        async with task_dict_lock:
            task_dict[listener.mid] = QueueStatus(listener, gid, "dl")
        await listener.on_download_start()
        if listener.multi <= 1:
            await send_status_message(listener.message)
        await event.wait()
        if listener.is_cancelled:
            return

    async with task_dict_lock:
        task_dict[listener.mid] = MegaToolsStatus(listener, downloader, gid)

    await listener.on_download_start()
    if listener.multi <= 1:
        await send_status_message(listener.message)

    await downloader.download()
