#!/usr/bin/env python3
import json
import os
import shutil
import time
from pathlib import Path

STATE_PATH = Path("/usr/src/app/torrents/manifests/hstream_part2_state.json")
MANIFEST_PATH = Path("/usr/src/app/torrents/manifests/hstream_part2_manifest.json")
TARGET_DIR = Path("/usr/src/app/torrents/seeding/Hstream 1080p Pack - Part 2")

def main():
    if not MANIFEST_PATH.is_file():
        print("Manifest not found.")
        return

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    total_items = len(manifest)

    state = {}
    if STATE_PATH.is_file():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    completed = [v for v in state.values() if v.get("status") == "COMPLETED"]
    downloading = [v for v in state.values() if v.get("status") == "DOWNLOADING"]
    failed = [v for v in state.values() if v.get("status") == "FAILED"]
    
    total_bytes = sum(v.get("size_bytes", 0) for v in completed)
    total_gb = total_bytes / (1024 ** 3)

    _, _, free = shutil.disk_usage("/")
    free_gb = free / (1024 ** 3)

    pct = (len(completed) / max(total_items, 1)) * 100

    print("=" * 60)
    print(f"  HSTREAM 1080P PART 2 — INGESTION MONITOR")
    print("=" * 60)
    print(f"Progress:       {len(completed)} / {total_items} episodes ({pct:.1f}%)")
    print(f"Active Workers: {len(downloading)} currently downloading")
    print(f"Failed / Retry: {len(failed)}")
    print(f"Downloaded:     {total_gb:.2f} GB")
    print(f"Free Disk:      {free_gb:.1f} GB remaining")
    
    recent = [v for v in completed if time.time() - v.get("completed_at", 0) < 3600]
    if recent:
        recent_bytes = sum(v.get("size_bytes", 0) for v in recent)
        recent_speed_mb = (recent_bytes / (1024 * 1024)) / 3600
        rem_items = total_items - len(completed)
        avg_sz_mb = (total_bytes / max(len(completed), 1)) / (1024 * 1024)
        eta_sec = (rem_items * avg_sz_mb) / max(recent_speed_mb, 0.1)
        eta_h = eta_sec / 3600
        print(f"Recent Speed:   {recent_speed_mb:.2f} MB/s (~{recent_speed_mb*8:.1f} Mbps)")
        print(f"Estimated ETA:  {eta_h:.1f} hours to completion")
    print("=" * 60)

if __name__ == "__main__":
    main()
