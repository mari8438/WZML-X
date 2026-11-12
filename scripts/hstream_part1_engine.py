#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image, ImageDraw, ImageFont, ImageOps

# Setup logging
LOG_FILE = "/usr/src/app/logs/hstream_part1_download.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("hstream_part1")

# Paths
ROOT_DIR = Path("/usr/src/app")
MANIFEST_PATH = ROOT_DIR / "torrents" / "manifests" / "hstream_part1_manifest.json"
STATE_PATH = ROOT_DIR / "torrents" / "manifests" / "hstream_part1_state.json"
TARGET_DIR = ROOT_DIR / "torrents" / "seeding" / "Hstream 1080p Pack - Part 1"
SCREENSHOTS_DIR = TARGET_DIR / "Screenshots"
TEMP_DIR = ROOT_DIR / "torrents" / "temp_part1"

TARGET_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MIN_FREE_DISK_GB = 45  # Safety circuit breaker
MAX_CONCURRENT_WORKERS = 6
YT_DLP_BIN = "/wzvenv/bin/yt-dlp"

def check_disk_safety():
    total, used, free = shutil.disk_usage("/")
    free_gb = free / (1024 ** 3)
    if free_gb < MIN_FREE_DISK_GB:
        raise RuntimeError(f"CIRCUIT BREAKER: Free disk space {free_gb:.1f} GB is below safety threshold ({MIN_FREE_DISK_GB} GB). Halting!")
    return free_gb

def clean_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:150]

def load_state() -> dict:
    if STATE_PATH.is_file():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read state, starting fresh: {e}")
    return {}

def save_state(state: dict):
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_PATH)

def generate_contact_sheet(video_path: Path, output_image_path: Path, count: int = 12) -> bool:
    if output_image_path.is_file() and output_image_path.stat().st_size > 5000:
        return True
    
    # 1. Probe duration
    probe_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)
    ]
    try:
        res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
        duration = float(res.stdout.strip())
    except Exception:
        duration = 1200.0  # fallback 20 mins

    if duration < 30:
        duration = 30

    interval = duration / (count + 1)
    timestamps = [interval * (i + 1) for i in range(count)]
    
    temp_frames = []
    tmp_prefix = TEMP_DIR / f"frame_{video_path.stem}"
    
    try:
        for idx, ts in enumerate(timestamps):
            frame_path = f"{tmp_prefix}_{idx}.jpg"
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(ts),
                "-i", str(video_path),
                "-frames:v", "1",
                "-q:v", "3",
                frame_path
            ]
            subprocess.run(cmd, capture_output=True, timeout=15)
            if os.path.isfile(frame_path) and os.path.getsize(frame_path) > 1000:
                temp_frames.append(frame_path)

        if not temp_frames:
            logger.warning(f"Could not extract frames for {video_path.name}")
            return False

        # Assemble into grid (3 columns x 4 rows)
        cols = 3
        rows = (len(temp_frames) + cols - 1) // cols
        thumb_w, thumb_h = 426, 240
        grid_w = cols * thumb_w
        grid_h = rows * thumb_h

        sheet = Image.new("RGB", (grid_w, grid_h), "black")
        for i, fp in enumerate(temp_frames):
            try:
                with Image.open(fp) as im:
                    thumb = im.resize((thumb_w, thumb_h), Image.Resampling.BILINEAR)
                    c = i % cols
                    r = i // cols
                    sheet.paste(thumb, (c * thumb_w, r * thumb_h))
            except Exception:
                pass

        sheet.save(str(output_image_path), "JPEG", quality=88, optimize=True)
        return True
    finally:
        for fp in temp_frames:
            try:
                os.remove(fp)
            except OSError:
                pass

async def download_worker(worker_id: int, queue: asyncio.Queue, state: dict, resolver):
    from bot.helper.ext_utils.hstream_resolver import HstreamCatalogItem, select_hstream_pack_stream

    while True:
        try:
            item_data = await queue.get()
        except asyncio.CancelledError:
            break

        slug = item_data["episode_slug"]
        series_title = item_data["series_title"]
        ep_num = item_data.get("episode_num", 1)

        if state.get(slug, {}).get("status") == "COMPLETED":
            queue.task_done()
            continue

        logger.info(f"[Worker {worker_id}] Starting: {series_title} - Ep {ep_num} ({slug})")
        state[slug] = {"status": "DOWNLOADING", "start_time": time.time()}

        try:
            check_disk_safety()
            
            # Resolve episode streams
            catalog_item = HstreamCatalogItem(series_title=series_title, episode=ep_num, slug=slug)
            episode = await resolver.resolve(catalog_item)
            
            # Stream selector: STRICTLY 1080p 48fps first, then fallback to 1080p
            stream = select_hstream_pack_stream(episode.streams)
            if not stream or not stream.urls:
                raise RuntimeError("No downloadable 1080p stream found")

            stream_url = stream.urls[0]
            label = stream.label or stream.resolution
            logger.info(f"[Worker {worker_id}] Selected stream: {label} ({stream.codec}) for {slug}")

            # Safe filenames
            clean_series = clean_filename(series_title)
            final_filename = f"{clean_series} - Ep {ep_num:02d} [{label}].mkv"
            final_video_path = TARGET_DIR / final_filename
            temp_output = TEMP_DIR / f"{slug}_temp.mkv"
            
            # Download with yt-dlp multi-fragment
            dl_cmd = [
                YT_DLP_BIN,
                "--no-playlist",
                "--retries", "4",
                "--concurrent-fragments", "6",
                "--socket-timeout", "25",
                "--no-check-certificates",
                "--referer", catalog_item.url,
                "-f", "bv*+ba/b",
                "--merge-output-format", "mkv",
                "-o", str(temp_output),
                stream_url
            ]

            proc = await asyncio.create_subprocess_exec(
                *dl_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                err_text = stderr.decode(errors="ignore")[-400:]
                raise RuntimeError(f"yt-dlp failed (code {proc.returncode}): {err_text}")

            # Verify downloaded file
            if not temp_output.is_file() or temp_output.stat().st_size < 1024 * 1024:
                raise RuntimeError(f"Downloaded file {temp_output} missing or too small")

            # Move to target dir
            if final_video_path.exists():
                final_video_path.unlink()
            shutil.move(str(temp_output), str(final_video_path))
            file_size_mb = final_video_path.stat().st_size / (1024 * 1024)

            # Generate contact sheet
            cs_filename = f"{clean_series} - Ep {ep_num:02d}_12screens.jpg"
            cs_path = SCREENSHOTS_DIR / cs_filename
            generate_contact_sheet(final_video_path, cs_path, count=12)

            state[slug] = {
                "status": "COMPLETED",
                "filename": final_filename,
                "size_bytes": final_video_path.stat().st_size,
                "resolution": label,
                "completed_at": time.time()
            }
            save_state(state)
            logger.info(f"[Worker {worker_id}] SUCCESS: {final_filename} ({file_size_mb:.1f} MB)")

        except Exception as e:
            logger.error(f"[Worker {worker_id}] FAILED: {slug}: {e}")
            state[slug] = {"status": "FAILED", "error": str(e), "failed_at": time.time()}
            save_state(state)
        finally:
            queue.task_done()

async def main():
    logger.info("=== HSTREAM 1080P PART 1 HIGH-SPEED ENGINE STARTING ===")
    free_gb = check_disk_safety()
    logger.info(f"Available disk space: {free_gb:.1f} GB")

    if not MANIFEST_PATH.is_file():
        logger.error(f"Manifest not found: {MANIFEST_PATH}")
        sys.exit(1)

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        episodes = json.load(f)

    state = load_state()
    completed_count = sum(1 for v in state.values() if v.get("status") == "COMPLETED")
    logger.info(f"Total Part 1 episodes: {len(episodes)} | Already completed: {completed_count}")

    from bot.helper.ext_utils.hstream_resolver import HstreamResolver

    queue = asyncio.Queue()
    for ep in episodes:
        if state.get(ep["episode_slug"], {}).get("status") != "COMPLETED":
            queue.put_nowait(ep)

    logger.info(f"Queued items remaining: {queue.qsize()}")

    async with HstreamResolver() as resolver:
        workers = [
            asyncio.create_task(download_worker(i, queue, state, resolver))
            for i in range(MAX_CONCURRENT_WORKERS)
        ]
        await queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    final_completed = sum(1 for v in state.values() if v.get("status") == "COMPLETED")
    logger.info(f"=== PART 1 RUN COMPLETE: {final_completed}/{len(episodes)} finished ===")

if __name__ == "__main__":
    asyncio.run(main())
