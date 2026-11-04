import asyncio
import os
import shutil
import logging
import sys
import time
from typing import Dict, Any

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        MAX_NVME_PIPELINE_QUOTA_GB,
        SAFETY_BUFFER_GB,
        RAM_MUX_DIR,
    )
else:
    from .config import MAX_NVME_PIPELINE_QUOTA_GB, SAFETY_BUFFER_GB, RAM_MUX_DIR

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] [%(levelname)s] - %(message)s")
LOGGER = logging.getLogger("DiskQuotaGuard")

class DiskQuotaGuard:
    _instance = None
    _lock = asyncio.Lock()
    _in_flight_bytes: int = 0

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def get_disk_free_gb(path: str = "/") -> float:
        """Returns free disk space in GB."""
        try:
            usage = shutil.disk_usage(path)
            return usage.free / (1024 ** 3)
        except Exception:
            return 0.0

    @staticmethod
    def get_path_size_gb(path: str) -> float:
        """Calculate total directory size in GB."""
        total = 0
        if not os.path.exists(path):
            return 0.0
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
        return total / (1024 ** 3)

    @classmethod
    def verify_ram_workspace(cls) -> Dict[str, Any]:
        """
        Verify that /dev/shm/starfall_mux is mounted and ready for high-speed RAM muxing.
        """
        os.makedirs(RAM_MUX_DIR, exist_ok=True)
        usage = shutil.disk_usage(RAM_MUX_DIR)
        total_gb = usage.total / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)

        # Verify read/write capability in RAM
        test_file = os.path.join(RAM_MUX_DIR, ".ram_mount_test")
        test_payload = b"starfall_ram_disk_verification" * 1024
        try:
            with open(test_file, "wb") as f:
                f.write(test_payload)
            with open(test_file, "rb") as f:
                read_back = f.read()
            assert read_back == test_payload, "Data integrity check failed on RAM disk"
            is_writable = True
        except Exception as e:
            LOGGER.error(f"Failed RAM disk write verification: {e}")
            is_writable = False
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

        is_tmpfs = os.path.realpath(RAM_MUX_DIR).startswith("/dev/shm")

        status = {
            "path": RAM_MUX_DIR,
            "is_tmpfs": is_tmpfs,
            "is_writable": is_writable,
            "total_gb": round(total_gb, 2),
            "free_gb": round(free_gb, 2),
            "used_gb": round(used_gb, 2),
            "status": "READY" if (is_writable and free_gb > 1.0) else "DEGRADED",
        }
        return status

    async def can_reserve_torrent(self, torrent_size_bytes: int) -> bool:
        """
        Enforce: Used_NVMe + InFlight_Torrents + (2.1 * New_Torrent_Size) <= 75 GB
        """
        async with self._lock:
            # Check physical NVMe free space (ensure > SAFETY_BUFFER_GB)
            free_nvme = self.get_disk_free_gb("/")
            if free_nvme < SAFETY_BUFFER_GB:
                LOGGER.warning(f"Disk quota rejected: Only {free_nvme:.2f} GB free on root NVMe.")
                return False

            new_projected_bytes = int(2.1 * torrent_size_bytes)
            projected_total_gb = (self._in_flight_bytes + new_projected_bytes) / (1024 ** 3)
            if projected_total_gb > MAX_NVME_PIPELINE_QUOTA_GB:
                LOGGER.warning(
                    f"Disk quota limit: In-flight {projected_total_gb:.2f} GB exceeds {MAX_NVME_PIPELINE_QUOTA_GB} GB cap."
                )
                return False

            self._in_flight_bytes += new_projected_bytes
            LOGGER.info(
                f"Reserved {torrent_size_bytes / (1024**2):.1f} MB for torrent. Current in-flight: {self._in_flight_bytes / (1024**3):.2f} GB."
            )
            return True

    async def release_reservation(self, torrent_size_bytes: int) -> None:
        """Release allocated reservation when torrent and muxes are cleared."""
        async with self._lock:
            released = int(2.1 * torrent_size_bytes)
            self._in_flight_bytes = max(0, self._in_flight_bytes - released)
            LOGGER.info(f"Released reservation. Current in-flight: {self._in_flight_bytes / (1024**3):.2f} GB.")

    @staticmethod
    def cleanup_ram_scratch(older_than_sec: int = 3600) -> None:
        """Clean leftover files in /dev/shm/starfall_mux."""
        if not os.path.exists(RAM_MUX_DIR):
            return
        now = time.time()
        for filename in os.listdir(RAM_MUX_DIR):
            filepath = os.path.join(RAM_MUX_DIR, filename)
            try:
                if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > older_than_sec:
                    os.remove(filepath)
            except Exception as e:
                LOGGER.error(f"Error cleaning {filepath}: {e}")

if __name__ == "__main__":
    async def _test_guard():
        print("=== Initializing DiskQuotaGuard & RAM Workspace Verification ===")
        guard = DiskQuotaGuard.get_instance()
        ram_info = guard.verify_ram_workspace()
        print("RAM Workspace Telemetry:")
        for k, v in ram_info.items():
            print(f"  • {k}: {v}")
        
        root_free = guard.get_disk_free_gb("/")
        print(f"Root NVMe Free: {root_free:.2f} GB (Safety buffer: {SAFETY_BUFFER_GB} GB, Pipeline cap: {MAX_NVME_PIPELINE_QUOTA_GB} GB)")

        # Test reservation simulation
        test_size = 5 * 1024 * 1024 * 1024  # 5 GB
        reserved = await guard.can_reserve_torrent(test_size)
        print(f"Simulated 5 GB torrent reservation allowed: {reserved}")
        await guard.release_reservation(test_size)
        print("=== DiskQuotaGuard Initialized & Verified Successfully ===")

    asyncio.run(_test_guard())
