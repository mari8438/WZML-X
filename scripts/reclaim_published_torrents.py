#!/usr/bin/env python3
"""
Autonomous Disk Reclamation Utility for Starfall Pipeline.
Safely purges completed raw torrent files from qBittorrent for seasons that have
ALREADY been successfully published and staged into Telegram Main Dump.
"""

import os
import re
import sys
import json
import logging

sys.path.insert(0, "/home/administrator/starfall")
from bot.modules.starfall_pipeline.qbittorrent_bridge import QBittorrentBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s"
)
LOGGER = logging.getLogger("DiskReclaimer")

PUBLISHED_REGISTRY_PATH = "/home/administrator/starfall/data/published_seasons.json"

def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        LOGGER.info("🔍 Running in DRY-RUN mode. No files or torrents will be deleted.")

    if not os.path.exists(PUBLISHED_REGISTRY_PATH):
        LOGGER.error(f"Published registry not found: {PUBLISHED_REGISTRY_PATH}")
        sys.exit(1)

    with open(PUBLISHED_REGISTRY_PATH, "r") as f:
        published_data = json.load(f)

    # Build lookup set of published (norm_title, season)
    published_seasons = set()
    for k in published_data.keys():
        m = re.match(r"^(.*?)_s(\d+)_([a-zA-Z0-9]+)$", k)
        if m:
            t_raw = m.group(1)
            s_num = int(m.group(2))
            norm_t = re.sub(r"[^a-zA-Z0-9]", "", t_raw).lower()
            published_seasons.add((norm_t, s_num))

    LOGGER.info(f"Loaded {len(published_data)} published records ({len(published_seasons)} unique title/season pairs).")

    qb = QBittorrentBridge()
    torrents = qb.get_all_torrents()
    if not torrents:
        LOGGER.warning("No torrents found in qBittorrent.")
        return

    LOGGER.info(f"Found {len(torrents)} torrents in qBittorrent.")

    # Guard keywords: never delete active in-flight or sensitive franchises
    IN_FLIGHT_KEYWORDS = ["appraisal", "gundam", "mercury", "bleach", "sharehouse", "chainsaw", "revengers", "heroacademia"]

    to_purge = []
    skipped_multi = 0
    skipped_incomplete = 0
    skipped_unpublished = 0

    for t in torrents:
        name = t.get("name", "")
        h = t.get("hash")
        sz = t.get("size", 0)
        prog = t.get("progress", 0.0)
        norm_name = re.sub(r"[^a-zA-Z0-9]", "", name).lower()

        # In-flight guard
        if any(k in norm_name for k in IN_FLIGHT_KEYWORDS):
            continue

        # Incomplete guard
        if prog < 1.0:
            skipped_incomplete += 1
            continue

        # Multi-season range guard
        has_multi_pattern = bool(
            re.search(r"\b(s\d{1,2}|season\s*\d{1,2}|seasons\s*\d{1,2})\s*(-|~|to|\+)\s*(s\d{1,2}|\d{1,2}|re)\b", name, re.I)
        ) or any(k in norm_name for k in [
            "s0105", "s0104", "s0103", "s0102", "s12", "s13", "s14", "s1s4", "s1s3", 
            "seasons12", "seasons13", "seasons14", "complete series", "s1a"
        ])
        if has_multi_pattern:
            skipped_multi += 1
            continue

        # Extract season number (default 1)
        m_s = re.search(r"\b(?:s|season)\s*(\d{1,2})\b", name, re.I)
        t_season = int(m_s.group(1)) if m_s else 1

        # Check against published registry
        is_published = False
        matched_pair = None
        for pt, ps in published_seasons:
            if len(pt) >= 5 and pt in norm_name and ps == t_season:
                is_published = True
                matched_pair = (pt, ps)
                break

        if is_published:
            to_purge.append((name, h, sz, matched_pair))
        else:
            skipped_unpublished += 1

    total_bytes = sum(x[2] for x in to_purge)
    LOGGER.info(
        f"Reclamation summary: {len(to_purge)} eligible torrents ({total_bytes / (1024**3):.2f} GB) | "
        f"Skipped: {skipped_multi} multi-season, {skipped_incomplete} incomplete, {skipped_unpublished} unpublished/in-flight"
    )

    if dry_run:
        LOGGER.info("Dry-run eligible list:")
        for name, h, sz, pair in to_purge:
            LOGGER.info(f"  [DRY-RUN] {sz / (1024**3):6.2f} GB | {pair[0]} S{pair[1]:02d} -> {name[:60]}")
        return

    # Execute purge
    purged_bytes = 0
    purged_count = 0
    for name, h, sz, pair in to_purge:
        LOGGER.info(f"🗑️ Purging ({sz / (1024**3):.2f} GB): {name[:65]}...")
        success = qb.delete_torrent(h, delete_files=True)
        if success:
            purged_bytes += sz
            purged_count += 1
        else:
            LOGGER.error(f"Failed to purge {name}")

    LOGGER.info(f"✅ Successfully reclaimed {purged_bytes / (1024**3):.2f} GB across {purged_count} published torrents!")

if __name__ == "__main__":
    main()
