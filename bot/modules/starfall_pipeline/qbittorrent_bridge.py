import logging
import os
import re
import time
import requests
from typing import Dict, Any, List, Optional

LOGGER = logging.getLogger("StarfallQBitBridge")

DEFAULT_QBT_URL = os.environ.get("QBIT_URL", "http://172.18.0.3:8090/api/v2")


class QBittorrentBridge:
    """
    High-performance, zero-dependency HTTP client for the local Docker qBittorrent Web API.
    Operates over the internal docker subnet (172.18.0.3:8090) with bypass whitelist authentication.
    """

    def __init__(self, base_url: str = DEFAULT_QBT_URL, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.timeout = timeout

    def get_version(self) -> str:
        """Fetch qBittorrent version string."""
        try:
            r = self.session.get(f"{self.base_url}/app/version", timeout=self.timeout)
            if r.status_code == 200:
                return r.text.strip()
        except Exception as e:
            LOGGER.error(f"Failed to get qBittorrent version: {e}")
        return ""

    def get_all_torrents(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """List active or all torrents in qBittorrent."""
        url = f"{self.base_url}/torrents/info"
        params = {}
        if category:
            params["category"] = category
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            LOGGER.error(f"Failed to list torrents: {e}")
        return []

    def get_torrent(self, info_hash: str) -> Optional[Dict[str, Any]]:
        """Fetch detailed info for a single torrent hash."""
        url = f"{self.base_url}/torrents/info"
        params = {"hashes": info_hash.lower()}
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
            if r.status_code == 200:
                items = r.json()
                if items:
                    return items[0]
        except Exception as e:
            LOGGER.error(f"Failed to get torrent {info_hash}: {e}")
        return None

    def add_torrent(
        self,
        magnet_or_url: str,
        container_save_path: str,
        category: str = "starfall",
        paused: bool = False,
    ) -> Optional[str]:
        """
        Add a torrent via magnet link or .torrent URL.
        Returns the 40-char info_hash if successful.
        """
        url = f"{self.base_url}/torrents/add"
        data = {
            "urls": magnet_or_url,
            "savepath": container_save_path,
            "category": category,
            "paused": "true" if paused else "false",
            "autoTMM": "false",
        }
        try:
            r = self.session.post(url, data=data, timeout=self.timeout)
            if r.status_code == 200:
                # Extract info_hash from JSON or magnet link
                try:
                    resp_json = r.json()
                    added = resp_json.get("added_torrent_ids", [])
                    if added:
                        return added[0].lower()
                except Exception:
                    pass

                # Fallback: extract hash from magnet
                m = re.search(r"urn:btih:([a-fA-F0-9]{40})", magnet_or_url)
                if m:
                    return m.group(1).lower()

                # Fallback: check recent torrent in qbt
                time.sleep(1.0)
                all_t = self.get_all_torrents(category=category)
                if all_t:
                    all_t.sort(key=lambda x: x.get("added_on", 0), reverse=True)
                    return all_t[0].get("hash", "").lower()

            LOGGER.error(f"Failed to add torrent: HTTP {r.status_code} - {r.text}")
        except Exception as e:
            LOGGER.error(f"Exception adding torrent: {e}")
        return None

    def reannounce(self, info_hash: str) -> bool:
        """Reannounce torrent to all trackers."""
        url = f"{self.base_url}/torrents/reannounce"
        try:
            r = self.session.post(url, data={"hashes": info_hash.lower()}, timeout=self.timeout)
            return r.status_code == 200
        except Exception as e:
            LOGGER.error(f"Failed to reannounce {info_hash}: {e}")
            return False

    def resume_torrent(self, info_hash: str) -> bool:
        """Resume a paused torrent."""
        url = f"{self.base_url}/torrents/resume"
        try:
            r = self.session.post(url, data={"hashes": info_hash.lower()}, timeout=self.timeout)
            return r.status_code == 200
        except Exception as e:
            LOGGER.error(f"Failed to resume {info_hash}: {e}")
            return False

    def pause_torrent(self, info_hash: str) -> bool:
        """Pause a running torrent."""
        url = f"{self.base_url}/torrents/pause"
        try:
            r = self.session.post(url, data={"hashes": info_hash.lower()}, timeout=self.timeout)
            return r.status_code == 200
        except Exception as e:
            LOGGER.error(f"Failed to pause {info_hash}: {e}")
            return False

    def delete_torrent(self, info_hash: str, delete_files: bool = False) -> bool:
        """Remove torrent from qBittorrent, optionally deleting downloaded data."""
        url = f"{self.base_url}/torrents/delete"
        data = {
            "hashes": info_hash.lower(),
            "deleteFiles": "true" if delete_files else "false",
        }
        try:
            r = self.session.post(url, data=data, timeout=self.timeout)
            return r.status_code == 200
        except Exception as e:
            LOGGER.error(f"Failed to delete {info_hash}: {e}")
            return False

    def get_torrent_files(self, info_hash: str) -> List[Dict[str, Any]]:
        """List all files inside a torrent."""
        url = f"{self.base_url}/torrents/files"
        try:
            r = self.session.get(url, params={"hash": info_hash.lower()}, timeout=self.timeout)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            LOGGER.error(f"Failed to get files for {info_hash}: {e}")
        return []

    def set_file_priorities(self, info_hash: str, file_ids: List[int], priority: int) -> bool:
        """
        Set file download priority (0 = do not download, 1 = normal, 6 = high, 7 = maximal).
        """
        url = f"{self.base_url}/torrents/filePrio"
        data = {
            "hash": info_hash.lower(),
            "id": "|".join(str(i) for i in file_ids),
            "priority": str(priority),
        }
        try:
            r = self.session.post(url, data=data, timeout=self.timeout)
            return r.status_code == 200
        except Exception as e:
            LOGGER.error(f"Failed to set file priorities for {info_hash}: {e}")
            return False
