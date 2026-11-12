#!/usr/bin/env python3
"""
Queue all Phase 1 BluRay Batches in qBittorrent
===============================================
Discovers the highest-ranked clean 1080p BluRay batch for each Phase 1 title
and adds it to qBittorrent with savepath /usr/src/app/downloads/clean_videos/<slug>.
Queueing is configured with max 3 active downloads to optimize multi-gigabit throughput.
"""

import re
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.modules.starfall_pipeline.qbittorrent_bridge import QBittorrentBridge
from bot.modules.starfall_pipeline.nyaa_matcher import NyaaMatcher
from bot.modules.starfall_pipeline.autonomous_agent import PHASE1_COMPLETED_CATALOGUE

qbt = QBittorrentBridge()
nyaa = NyaaMatcher()

print(f"=== Queueing Phase 1 Torrents in qBittorrent ({len(PHASE1_COMPLETED_CATALOGUE)} Titles) ===\n")

# Get existing torrents in qbt
existing = qbt.get_all_torrents()
existing_hashes = {t.get("hash", "").lower() for t in existing}

results = []
for idx, anime in enumerate(PHASE1_COMPLETED_CATALOGUE, 1):
    title = anime["title"]
    season = anime["season"]
    slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_").lower()
    slug = f"{slug}_s{season:02d}"
    container_save_path = f"/usr/src/app/downloads/clean_videos/{slug}"

    print(f"[{idx:02d}/{len(PHASE1_COMPLETED_CATALOGUE)}] Sourcing: {title} S{season:02d}...")
    batches = nyaa.find_multi_source_batches(title, season_number=season)
    if not batches:
        print(f"  ❌ No batches found for {title}")
        continue

    # Filter out bleach or invalid releases
    valid = [b for b in batches if "bleach" not in b["title"].lower()]
    if not valid:
        print(f"  ❌ No valid batch candidates for {title}")
        continue

    best = valid[0]
    info_hash = best.get("info_hash", "").lower()
    magnet = best.get("magnet_uri") or best.get("torrent_url")

    if info_hash in existing_hashes:
        print(f"  ⚡ Already in qBittorrent: [{best['release_group']}] {best['title'][:55]}... ({best['size']})")
        results.append((title, best["release_group"], best["size"], "Already Queued", info_hash))
        continue

    # Add to qBittorrent
    added_hash = qbt.add_torrent(
        magnet_or_url=magnet,
        container_save_path=container_save_path,
        category="starfall",
        paused=False,
    )

    if added_hash:
        print(f"  ✅ Added to qBittorrent: [{best['release_group']}] {best['title'][:55]}... ({best['size']}, {best['seeders']} seeds)")
        existing_hashes.add(added_hash.lower())
        results.append((title, best["release_group"], best["size"], "Added", added_hash))
    else:
        print(f"  ❌ Failed to add to qBittorrent: {best['title']}")

print("\n=== Summary of Phase 1 Ingestion Queue ===")
for title, grp, sz, status, h in results:
    print(f"- {title}: [{grp}] {sz} -> {status} (Hash: {h[:8]}...)")
