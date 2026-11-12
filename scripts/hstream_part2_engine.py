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
import traceback
from pathlib import Path

sys.path.insert(0, "/usr/src/app")

from PIL import Image

# Setup logging
LOG_FILE = "/usr/src/app/logs/hstream_part2_download.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("hstream_part2")

ROOT_DIR = Path("/usr/src/app")
MANIFEST_PATH = ROOT_DIR / "torrents" / "manifests" / "hstream_part2_manifest.json"
STATE_PATH = ROOT_DIR / "torrents" / "manifests" / "hstream_part2_state.json"
TARGET_DIR = ROOT_DIR / "torrents" / "seeding" / "Hstream 1080p Pack - Part 2"
SCREENSHOTS_DIR = TARGET_DIR / "Screenshots"
TEMP_DIR = ROOT_DIR / "torrents" / "temp_part2"

TARGET_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MIN_FREE_DISK_GB = 45
MAX_CONCURRENT_WORKERS = 6
YT_DLP_BIN = "/wzvenv/bin/yt-dlp" if Path("/wzvenv/bin/yt-dlp").is_file() else (shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp")

def check_disk_safety():
    total, used, free = shutil.disk_usage("/")
    free_gb = free / (1024 ** 3)
    if free_gb < MIN_FREE_DISK_GB:
        raise RuntimeError(f"CIRCUIT BREAKER: Free disk {free_gb:.1f} GB is below safety threshold ({MIN_FREE_DISK_GB} GB). Stopping!")
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

def choose_target_stream(streams):
    """
    STRICT USER RULE:
    1. If 1080p 48fps is available, ONLY download 1080p 48fps.
    2. If not available, fallback to standard 1080p.
    3. If neither, fallback to highest available resolution.
    """
    if not streams:
        return None
    # 1. 1080p 48fps
    for s in streams:
        is_1080 = "1080" in s.resolution
        is_48 = "48fps" in s.label or s.fps >= 40
        if is_1080 and is_48:
            return s
    # 2. Standard 1080p
    for s in streams:
        if "1080" in s.resolution:
            return s
    # 3. Best fallback
    def _rank(s):
        try:
            res_num = int(str(s.resolution).rstrip("p"))
        except (ValueError, TypeError):
            res_num = 0
        fps_boost = 1 if ("48fps" in s.label or s.fps >= 40) else 0
        return (res_num, fps_boost)
    return max(streams, key=_rank)

def generate_contact_sheet(video_path: Path, output_image_path: Path, count: int = 12) -> bool:
    if output_image_path.is_file() and output_image_path.stat().st_size > 5000:
        return True
    
    probe_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)
    ]
    try:
        res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
        duration = float(res.stdout.strip())
    except Exception:
        duration = 1200.0

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
            logger.warning(f"No frames captured for {video_path.name}")
            return False

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

async def download_worker(worker_id: int, queue: asyncio.Queue, state: dict):
    from bot.helper.ext_utils.hstream_resolver import HstreamCatalogItem, HstreamResolver

    # Each worker maintains its own isolated resolver to prevent CSRF / session race conditions
    async with HstreamResolver() as resolver:
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

            logger.info(f"[Worker {worker_id}] Processing: {series_title} - Ep {ep_num} ({slug})")
            state[slug] = {"status": "DOWNLOADING", "start_time": time.time()}

            try:
                check_disk_safety()
                
                catalog_item = HstreamCatalogItem(series_title=series_title, episode=ep_num, slug=slug)
                episode = await resolver.resolve(catalog_item)
                
                # Apply priority rule: 1080p 48fps > 1080p > fallback
                stream = choose_target_stream(episode.streams)
                if not stream or not stream.urls:
                    raise RuntimeError(f"No valid stream for {slug} (found {len(episode.streams)} streams)")

                mirror_idx = worker_id % len(stream.urls)
                stream_url = stream.urls[mirror_idx]
                mirror_host = stream_url.split("/")[2] if "/" in stream_url else "mirror"
                label = stream.label or stream.resolution
                logger.info(f"[Worker {worker_id}] Selected stream: {label} ({stream.codec}) via {mirror_host} for {slug}")

                clean_series = clean_filename(series_title)
                final_filename = f"{clean_series} - Ep {ep_num:02d} [{label}].mkv"
                final_video_path = TARGET_DIR / final_filename
                temp_output = TEMP_DIR / f"{slug}_temp.mkv"
                
                dl_cmd = [
                    YT_DLP_BIN,
                    "--no-playlist",
                    "--retries", "5",
                    "--concurrent-fragments", "8",
                    "--buffer-size", "16M",
                    "--http-chunk-size", "10M",
                    "--socket-timeout", "25",
                    "--no-check-certificates",
                    "--referer", catalog_item.url,
                    "-f", "bv*+ba/b",
                    "--merge-output-format", "mkv",
                    "-o", str(temp_output),
                    stream_url
                ]

                proc_res = await asyncio.to_thread(
                    subprocess.run,
                    dl_cmd,
                    capture_output=True,
                    timeout=300,
                )

                if proc_res.returncode != 0:
                    err_text = proc_res.stderr.decode(errors="ignore")[-400:]
                    raise RuntimeError(f"yt-dlp exited with {proc_res.returncode}: {err_text}")

                if not temp_output.is_file() or temp_output.stat().st_size < 1024 * 1024:
                    raise RuntimeError(f"Downloaded file {temp_output} missing or too small (<1MB)")

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
                err_msg = str(e) or type(e).__name__
                logger.error(f"[Worker {worker_id}] FAILED: {slug}: {err_msg}\n{traceback.format_exc()}")
                state[slug] = {"status": "FAILED", "error": err_msg, "failed_at": time.time()}
                save_state(state)
            finally:
                queue.task_done()

async def main():
    logger.info("=== HSTREAM 1080P PART 2 HIGH-SPEED ENGINE STARTING ===")
    free_gb = check_disk_safety()
    logger.info(f"Available disk space: {free_gb:.1f} GB")

    if not MANIFEST_PATH.is_file():
        logger.error(f"Manifest not found: {MANIFEST_PATH}")
        sys.exit(1)

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        episodes = json.load(f)

    state = load_state()
    # Filter out failed states to give them a fresh retry with isolated sessions
    for k in list(state.keys()):
        if state[k].get("status") in ("FAILED", "DOWNLOADING"):
            del state[k]
    save_state(state)

    completed_count = sum(1 for v in state.values() if v.get("status") == "COMPLETED")
    logger.info(f"Total Part 2 episodes: {len(episodes)} | Already completed: {completed_count}")

    queue = asyncio.Queue()
    for ep in episodes:
        if state.get(ep["episode_slug"], {}).get("status") != "COMPLETED":
            queue.put_nowait(ep)

    logger.info(f"Queued items remaining: {queue.qsize()}")

    workers = [
        asyncio.create_task(download_worker(i, queue, state))
        for i in range(MAX_CONCURRENT_WORKERS)
    ]
    await queue.join()
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    final_completed = sum(1 for v in state.values() if v.get("status") == "COMPLETED")
    logger.info(f"=== PART 2 RUN COMPLETE: {final_completed}/{len(episodes)} finished ===")

if __name__ == "__main__":
    asyncio.run(main())
