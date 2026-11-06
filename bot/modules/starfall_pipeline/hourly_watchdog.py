import os
import sys
import time
import shutil
import sqlite3
import logging
from typing import Dict, Any, List

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.qbittorrent_bridge import QBittorrentBridge
    from bot.modules.starfall_pipeline.config import DB_PATH, BASE_DIR
else:
    from .qbittorrent_bridge import QBittorrentBridge
    from .config import DB_PATH, BASE_DIR

LOGGER = logging.getLogger("StarfallWatchdog")
WATCHDOG_LOG_PATH = os.path.join(BASE_DIR, "logs", "starfall_hourly_watchdog.log")


class HourlyWatchdog:
    """
    Recurring 1-Hour Autonomous Checkup & Self-Healing Watchdog:
    1. qBittorrent Health: checks active torrents; reannounces stalled/zero-peer torrents >30 min.
    2. Pipeline Lock Clear: purges temporary files >1 hour old in temp_pipeline and temp_tamil_proc.
    3. Database Consistency: resets stuck 'PROCESSING' worker states in SQLite back to 'PENDING'.
    4. Disk Safety Audit: enforces 80-100 GB safety thresholds and clears cache dumps.
    5. Hourly Status Pulse: writes structured diagnostic report to logs and console.
    """

    def __init__(self, qbt_bridge: QBittorrentBridge = None, db_path: str = DB_PATH):
        self.qbt = qbt_bridge or QBittorrentBridge()
        self.db_path = db_path
        self.temp_dirs = [
            os.path.join(BASE_DIR, "downloads", "temp_pipeline"),
            os.path.join(BASE_DIR, "downloads", "temp_tamil_proc"),
        ]
        os.makedirs(os.path.dirname(WATCHDOG_LOG_PATH), exist_ok=True)

    def check_qbittorrent_health(self) -> Dict[str, Any]:
        """Check active torrents and reannounce or note stalled torrents."""
        torrents = self.qbt.get_all_torrents()
        stalled_count = 0
        reannounced = []
        active_downloads = []

        now = time.time()
        for t in torrents:
            state = t.get("state", "")
            seeds = t.get("num_seeds", 0)
            added_on = t.get("added_on", now)
            last_activity = t.get("last_activity", added_on)
            dlspeed = t.get("dlspeed", 0)

            active_downloads.append({
                "name": t.get("name", "Unknown"),
                "state": state,
                "progress": round(t.get("progress", 0.0) * 100, 1),
                "dlspeed_mb": round(dlspeed / (1024 * 1024), 2),
                "seeds": seeds,
                "size_gib": round(t.get("size", 0) / (1024 ** 3), 2),
            })

            # Check if stalled or 0 peers for > 30 mins
            idle_sec = max(now - last_activity, now - added_on)
            if state in ("stalledDL", "metaDL", "queuedDL") or (seeds == 0 and dlspeed == 0):
                if idle_sec > 1800:
                    stalled_count += 1
                    info_hash = t.get("hash", "")
                    if info_hash:
                        ok = self.qbt.reannounce(info_hash)
                        if ok:
                            reannounced.append(t.get("name", info_hash[:8]))
                            LOGGER.info(f"Reannounced stalled torrent: {t.get('name')}")

        return {
            "total_torrents": len(torrents),
            "stalled_count": stalled_count,
            "reannounced": reannounced,
            "active_downloads": active_downloads,
        }

    def clear_pipeline_locks(self) -> int:
        """Purge temporary files older than 1 hour in staging folders."""
        purged = 0
        now = time.time()
        for d in self.temp_dirs:
            if not os.path.exists(d):
                continue
            for root, _, files in os.walk(d):
                for f in files:
                    filepath = os.path.join(root, f)
                    try:
                        mtime = os.path.getmtime(filepath)
                        if now - mtime > 3600:
                            os.remove(filepath)
                            purged += 1
                    except Exception as e:
                        LOGGER.warning(f"Failed to remove stale temp file {filepath}: {e}")
        return purged

    def fix_database_consistency(self) -> Dict[str, int]:
        """Reset stuck PROCESSING or DOWNLOADING tasks back to PENDING/QUEUED."""
        if not os.path.exists(self.db_path):
            return {"unlocked_seasons": 0, "unlocked_jobs": 0}

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Unlock seasons stuck in PROCESSING, DOWNLOADING, HARVESTING older than 1 hour
            cursor.execute(
                """
                UPDATE anime_seasons
                SET season_state = 'PENDING', updated_at = CURRENT_TIMESTAMP
                WHERE season_state IN ('PROCESSING', 'DOWNLOADING', 'HARVESTING')
                  AND (strftime('%s', 'now') - strftime('%s', updated_at)) > 3600
                """
            )
            unlocked_seasons = cursor.rowcount

            # Unlock episode jobs stuck in SYNCING, MUXING, UPLOADING older than 1 hour
            cursor.execute(
                """
                UPDATE episode_jobs
                SET job_state = 'QUEUED', updated_at = CURRENT_TIMESTAMP
                WHERE job_state IN ('SYNCING', 'MUXING', 'UPLOADING')
                  AND (strftime('%s', 'now') - strftime('%s', updated_at)) > 3600
                """
            )
            unlocked_jobs = cursor.rowcount

            conn.commit()
            conn.close()
            return {"unlocked_seasons": unlocked_seasons, "unlocked_jobs": unlocked_jobs}
        except Exception as e:
            LOGGER.error(f"Database consistency check failed: {e}")
            return {"unlocked_seasons": 0, "unlocked_jobs": 0}

    def audit_disk_safety(self) -> Dict[str, Any]:
        """Check disk space and clear logs or dumps if < 100 GB free."""
        usage = shutil.disk_usage("/")
        free_gb = round(usage.free / (1024 ** 3), 2)
        total_gb = round(usage.total / (1024 ** 3), 2)
        used_gb = round(usage.used / (1024 ** 3), 2)
        is_safe = free_gb >= 80.0
        cleaned_mb = 0.0

        if free_gb < 100.0:
            LOGGER.warning(f"Free disk space low ({free_gb:.1f} GB < 100 GB threshold). Cleaning cached logs...")
            log_dir = os.path.join(BASE_DIR, "logs")
            if os.path.exists(log_dir):
                for f in os.listdir(log_dir):
                    fp = os.path.join(log_dir, f)
                    try:
                        sz = os.path.getsize(fp)
                        if sz > 50 * 1024 * 1024:  # truncate logs > 50 MB
                            with open(fp, "w") as lf:
                                lf.truncate(0)
                            cleaned_mb += sz / (1024 * 1024)
                    except Exception:
                        pass

        return {
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "is_safe": is_safe,
            "cleaned_mb": round(cleaned_mb, 2),
        }

    def run_checkup(self, completed_count: int = 0, current_phase: str = "Phase 1", next_anime: str = "Solo Leveling") -> str:
        """Run complete 1-hour diagnostic and remediation cycle."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        qbt_res = self.check_qbittorrent_health()
        purged_files = self.clear_pipeline_locks()
        db_res = self.fix_database_consistency()
        disk_res = self.audit_disk_safety()

        active_dl_lines = []
        for d in qbt_res["active_downloads"]:
            active_dl_lines.append(
                f"   - {d['name']} [{d['state']}] {d['progress']}% ({d['dlspeed_mb']} MB/s, {d['seeds']} seeds)"
            )
        dl_summary = "\n".join(active_dl_lines) if active_dl_lines else "   - None currently active"

        report = (
            f"==========================================================\n"
            f"🕒 STARFALL 1-HOUR AUTONOMOUS WATCHDOG PULSE ({ts})\n"
            f"==========================================================\n"
            f"📊 Progress: {current_phase} | Completed Titles: {completed_count}\n"
            f"⏭️ Next Queued Anime: {next_anime}\n"
            f"💾 NVMe SSD: {disk_res['free_gb']} GB Free / {disk_res['total_gb']} GB Total ({'✅ SAFE' if disk_res['is_safe'] else '⚠️ CRITICAL'})\n"
            f"📥 Active Torrents ({qbt_res['total_torrents']}):\n{dl_summary}\n"
            f"🔄 Remediations:\n"
            f"   - Reannounced Stalled: {len(qbt_res['reannounced'])}\n"
            f"   - Purged Temp Files: {purged_files}\n"
            f"   - Unlocked DB Records: Seasons={db_res['unlocked_seasons']}, Jobs={db_res['unlocked_jobs']}\n"
            f"=========================================================="
        )

        LOGGER.info(f"\n{report}")
        try:
            with open(WATCHDOG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(report + "\n\n")
        except Exception as e:
            LOGGER.error(f"Failed to append to watchdog log: {e}")

        return report


if __name__ == "__main__":
    watchdog = HourlyWatchdog()
    report = watchdog.run_checkup()
    print(report)
