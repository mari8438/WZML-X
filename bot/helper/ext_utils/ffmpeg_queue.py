from asyncio import Lock
from contextlib import asynccontextmanager
import os
from time import time

from ... import DOWNLOAD_DIR, LOGGER
from ...core.config_manager import Config
from .files_utils import ensure_nvme_headroom

ffmpeg_lock = Lock()


def _queue_enabled():
    return bool(getattr(Config, "FFMPEG_QUEUE_ENABLED", True))


def _queue_logs():
    return bool(getattr(Config, "FFMPEG_QUEUE_LOGS", True))


async def _notify(listener, text):
    if not listener or not _queue_logs():
        return
    try:
        from ..telegram_helper.message_utils import send_message

        await send_message(listener.message, text)
    except Exception as e:
        LOGGER.debug(f"FFmpeg queue notify skipped: {e}")


@asynccontextmanager
async def ffmpeg_task(
    listener=None,
    label="FFmpeg task",
    input_size: int = 0,
    input_path: str = "",
    download_dir: str = "",
    **kwargs,
):
    if not _queue_enabled():
        yield
        return

    req_size = input_size
    if not req_size:
        if input_path and os.path.isfile(input_path):
            try:
                req_size = os.path.getsize(input_path)
            except OSError:
                req_size = 0
        elif listener:
            req_size = getattr(listener, "subsize", 0) or getattr(listener, "size", 0) or 0

    target_dir = download_dir
    if not target_dir:
        if input_path:
            target_dir = os.path.dirname(input_path)
        elif listener:
            target_dir = getattr(listener, "dir", "")
        if not target_dir:
            target_dir = DOWNLOAD_DIR
    if not os.path.exists(target_dir):
        target_dir = "."

    start_wait = time()
    if ffmpeg_lock.locked():
        LOGGER.info(f"Queue: Waiting for FFmpeg slot - {label}")
        await _notify(listener, f"Queue: Waiting for FFmpeg slot - <code>{label}</code>")

    async with ffmpeg_lock:
        waited = time() - start_wait
        if _queue_logs():
            if waited >= 1:
                LOGGER.info(f"FFmpeg task started - {label} after {waited:.1f}s wait")
            else:
                LOGGER.info(f"FFmpeg task started - {label}")

        is_safe, free_gb, purged = await ensure_nvme_headroom(
            target_dir, required_bytes=int(req_size * 1.15) if req_size else 0
        )
        if purged > 0:
            LOGGER.info(
                f"NVMe Headroom check freed {purged / (1024**2):.1f} MB of orphaned files before {label}"
            )
        if not is_safe:
            err_msg = (
                f"Insufficient NVMe disk headroom ({free_gb:.2f} GB free) to safely run {label}"
            )
            LOGGER.error(err_msg)
            await _notify(listener, f"⚠️ <b>Disk Space Warning:</b> {err_msg}")
            raise RuntimeError(err_msg)

        try:
            yield
            if _queue_logs():
                LOGGER.info(f"FFmpeg task finished - {label}")
        except Exception as e:
            LOGGER.error(f"FFmpeg task failed - {label}: {e}")
            raise
