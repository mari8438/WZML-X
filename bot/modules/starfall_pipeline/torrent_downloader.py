import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from typing import Dict, Any, List, Optional, Tuple

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import DATA_DIR
    from bot.modules.starfall_pipeline.disk_guard import DiskQuotaGuard
else:
    from .config import DATA_DIR
    from .disk_guard import DiskQuotaGuard

LOGGER = logging.getLogger("StarfallTorrentDownloader")

TORRENTS_BASE_DIR = os.path.join(DATA_DIR, "torrents")
os.makedirs(TORRENTS_BASE_DIR, exist_ok=True)

class TorrentDownloader:
    """
    Manages BitTorrent downloads via aria2c with automatic file priority filtering:
    - Filters out NCOP, NCED, Menu, PV, CM, OVA, Scans, Extras
    - Enforces NVMe disk quota reservation (< 450 GB cap)
    - Maps episode numbers to downloaded video files
    """

    def __init__(self, base_download_dir: str = TORRENTS_BASE_DIR):
        self.base_dir = base_download_dir
        self.quota_guard = DiskQuotaGuard.get_instance()
        os.makedirs(self.base_dir, exist_ok=True)

    @staticmethod
    def is_extra_file(filename: str) -> bool:
        """Check if file is an extra, opening/ending theme, menu, or scan."""
        t = filename.upper()
        patterns = [
            r"NCED", r"NCOP", r"MENU", r"\bPV\b", r"\bCM\b",
            r"EXTRA", r"SCANS?", r"CREDITLESS", r"SP\d*", r"TRAILER", r"\bOST\b"
        ]
        return any(re.search(p, t) for p in patterns)

    @staticmethod
    def parse_ep_number(filename: str) -> Optional[int]:
        """Extract episode number from filename."""
        raw = os.path.splitext(os.path.basename(filename))[0]
        # Match SxxExx
        s_e = re.search(r"[Ss]\d{1,2}[Ee](\d{1,3})", raw)
        if s_e:
            return int(s_e.group(1))
        # Match EP xx or Episode xx
        ep = re.search(r"(?:EP|Episode)[.\s_-]*(\d{1,3})", raw, re.IGNORECASE)
        if ep:
            return int(ep.group(1))
        # Match _-_01_ or - 01
        num = re.search(r"[-_]\s*0*(\d{1,3})(?:\s*\(|\s*\[|\s*_|\s*-|\.|$)", raw)
        if num:
            return int(num.group(1))
        return None

    def fetch_torrent_file(self, torrent_url: str, info_hash: str) -> Optional[str]:
        """Download .torrent file to local cache."""
        target_path = os.path.join(self.base_dir, f"{info_hash}.torrent")
        if os.path.exists(target_path) and os.path.getsize(target_path) > 100:
            return target_path

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        req = urllib.request.Request(torrent_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            if len(data) > 100:
                with open(target_path, "wb") as f:
                    f.write(data)
                return target_path
        except Exception as e:
            LOGGER.error(f"Failed to fetch .torrent from {torrent_url}: {e}")
        return None

    def inspect_torrent_files(self, torrent_path: str) -> List[Dict[str, Any]]:
        """
        Run `aria2c --show-files` and parse file index list.
        Returns list of dicts: [{'idx': 1, 'path': '...', 'size_str': '...', 'is_episode': bool, 'ep': int}]
        """
        cmd = ["aria2c", "--show-files", torrent_path]
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except Exception as e:
            LOGGER.error(f"Failed to inspect files in {torrent_path}: {e}")
            return []

        files = []
        # Pattern: " 1|./path/to/file.mkv"
        lines = out.splitlines()
        for idx_line, line in enumerate(lines):
            m = re.match(r"^\s*(\d+)\|(.*)$", line)
            if m:
                file_idx = int(m.group(1))
                file_path = m.group(2).strip()
                # Next line often has size: " |288MiB (302,883,609)"
                size_str = ""
                if idx_line + 1 < len(lines):
                    next_line = lines[idx_line + 1].strip()
                    if next_line.startswith("|"):
                        size_str = next_line[1:].strip()

                is_video = file_path.lower().endswith((".mkv", ".mp4", ".webm"))
                is_extra = self.is_extra_file(file_path)
                ep_num = self.parse_ep_number(file_path) if is_video and not is_extra else None

                files.append({
                    "idx": file_idx,
                    "path": file_path,
                    "size_str": size_str,
                    "is_video": is_video,
                    "is_extra": is_extra,
                    "episode": ep_num,
                })

        return files

    async def download_batch_torrent(
        self,
        torrent_info: Dict[str, Any],
        target_episodes: Optional[List[int]] = None,
        timeout_sec: int = 1800,
    ) -> Optional[Dict[int, str]]:
        """
        Download batch torrent with aria2c, selecting only required episodes
        and skipping non-episode extras.
        Returns mapping: {episode_number: local_video_file_path}
        """
        info_hash = torrent_info.get("info_hash") or "unknown_hash"
        torrent_url = torrent_info.get("torrent_url", "")
        dest_dir = os.path.join(self.base_dir, info_hash)
        os.makedirs(dest_dir, exist_ok=True)

        # 1. Fetch .torrent file
        torrent_path = self.fetch_torrent_file(torrent_url, info_hash)
        if not torrent_path or not os.path.exists(torrent_path):
            LOGGER.error(f"Could not retrieve .torrent file for {info_hash}")
            return None

        # 2. Inspect files and select episode indices
        all_files = self.inspect_torrent_files(torrent_path)
        selected_indices = []
        ep_to_index: Dict[int, int] = {}

        for f in all_files:
            ep = f["episode"]
            if f["is_video"] and not f["is_extra"] and ep is not None:
                if target_episodes is None or ep in target_episodes:
                    selected_indices.append(str(f["idx"]))
                    ep_to_index[ep] = f["idx"]

        if not selected_indices:
            LOGGER.warning(f"No valid episode video files matched in torrent {info_hash}")
            # If episode parsing was ambiguous, select all video files that aren't extras
            for f in all_files:
                if f["is_video"] and not f["is_extra"]:
                    selected_indices.append(str(f["idx"]))

        if not selected_indices:
            LOGGER.error(f"No video files found in torrent {torrent_path}")
            return None

        select_files_arg = ",".join(selected_indices)
        LOGGER.info(
            f"Selected {len(selected_indices)}/{len(all_files)} files to download for {info_hash}: "
            f"indices={select_files_arg[:60]}..."
        )

        # 3. Disk Quota Reservation Check
        estimated_bytes = 10 * 1024 * 1024 * 1024  # default 10 GB estimate
        can_reserve = await self.quota_guard.can_reserve_torrent(estimated_bytes)
        if not can_reserve:
            LOGGER.error(f"Insufficient quota reservation for batch download {info_hash}")
            return None

        # 4. Launch aria2c download
        cmd = [
            "aria2c",
            f"--dir={dest_dir}",
            f"--select-file={select_files_arg}",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--seed-time=0",
            "--bt-stop-timeout=180",
            "--max-connection-per-server=16",
            "--split=16",
            "--summary-interval=15",
            "--file-allocation=none",
            torrent_path,
        ]

        LOGGER.info(f"Starting aria2c download for {info_hash} in {dest_dir}...")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            if proc.returncode != 0:
                LOGGER.error(f"aria2c failed (code {proc.returncode}): {stderr.decode()[:400]}")
                await self.quota_guard.release_reservation(estimated_bytes)
                return None
        except asyncio.TimeoutError:
            LOGGER.error(f"aria2c timed out after {timeout_sec}s for {info_hash}")
            try:
                proc.kill()
            except Exception:
                pass
            await self.quota_guard.release_reservation(estimated_bytes)
            return None

        # 5. Map downloaded files to episode numbers
        downloaded_episodes: Dict[int, str] = {}
        for root, _, filenames in os.walk(dest_dir):
            for fn in filenames:
                if fn.lower().endswith((".mkv", ".mp4", ".webm")) and not self.is_extra_file(fn):
                    full_p = os.path.join(root, fn)
                    ep = self.parse_ep_number(fn)
                    if ep is not None:
                        downloaded_episodes[ep] = full_p

        LOGGER.info(f"🎉 Downloaded and mapped {len(downloaded_episodes)} episodes for {info_hash}")
        return downloaded_episodes

    async def cleanup_batch(self, info_hash: str) -> None:
        """Purge downloaded batch folder to free up NVMe storage immediately."""
        dest_dir = os.path.join(self.base_dir, info_hash)
        if os.path.exists(dest_dir):
            try:
                shutil.rmtree(dest_dir)
                LOGGER.info(f"Cleaned up torrent directory: {dest_dir}")
            except Exception as e:
                LOGGER.error(f"Failed to cleanup {dest_dir}: {e}")

        torrent_file = os.path.join(self.base_dir, f"{info_hash}.torrent")
        if os.path.exists(torrent_file):
            try:
                os.remove(torrent_file)
            except Exception:
                pass

        await self.quota_guard.release_reservation(10 * 1024 * 1024 * 1024)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== TorrentDownloader Module Ready ===")
