#!/usr/bin/env python3
"""
scripts/prebuffer_torrents.py
-----------------------------
Queues high-priority upcoming torrents into qBittorrent to establish
the ~150 GB background download pre-buffer while muxing and uploading.
"""

import sys
import logging
from bot.modules.starfall_pipeline.qbittorrent_bridge import QBittorrentBridge

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] - %(message)s")
LOGGER = logging.getLogger("Prebuffer")

TORRENTS_TO_PREBUFFER = [
    # Attack on Titan Upcoming Batches
    {"name": "AoT S03 Judas", "url": "https://nyaa.si/download/1182740.torrent", "hash": "3e9b72a4c146c8270146e27c1f8222b467ec50ee"},
    {"name": "AoT S04 Judas", "url": "https://nyaa.si/download/1739210.torrent", "hash": "f471e4d0752d5dbd391f165e38682a22be1e7da9"},
    {"name": "AoT OADs Judas", "url": "https://nyaa.si/download/1188102.torrent", "hash": "cbbf6dbe4d5f4ad69cf88bb97db9eefd0859a721"},

    # Demon Slayer Upcoming Batches
    {"name": "DS S01 Judas", "url": "https://nyaa.si/download/1280887.torrent", "hash": "a8dd49c302c17e035541a3499f22e3f6e3529f45"},
    {"name": "DS S02 Yuukaku Judas", "url": "https://nyaa.si/download/1491968.torrent", "hash": "37369c950f47f6d44041c1bda9735c4c7cd8ba2d"},
    {"name": "DS S02 Mugen Judas", "url": "https://nyaa.si/download/1491786.torrent", "hash": "2fe8084a5e1b3cd006dea85f279271deee757a1b"},
    {"name": "DS S03 Judas", "url": "https://nyaa.si/download/1706044.torrent", "hash": "bd573fb3fb07db7a6f90be76859be72ae4c2cb8c"},
    {"name": "DS S01-05 TRiX", "url": "https://nyaa.si/download/2013468.torrent", "hash": "c9f2f5b4c36d9f0a0d3d449fb81050eb4d24f6e6"},
]

def main():
    qb = QBittorrentBridge()
    existing = {t.get("hash", "").lower(): t for t in qb.get_all_torrents()}
    
    LOGGER.info(f"Currently active in qBittorrent: {len(existing)} torrents")
    added_count = 0

    for item in TORRENTS_TO_PREBUFFER:
        ihash = item.get("hash", "").lower()
        if ihash in existing:
            t = existing[ihash]
            LOGGER.info(f"⚡ [Already in qBittorrent] {item['name']}: {t.get('name')} ({t.get('progress', 0.0)*100:.1f}%)")
            continue

        LOGGER.info(f"📥 Adding pre-buffer torrent: {item['name']} ({item['url']})")
        res_hash = qb.add_torrent(
            magnet_or_url=item["url"],
            container_save_path="/usr/src/app/downloads",
            category="starfall_sprint",
        )
        if res_hash:
            LOGGER.info(f"✅ Successfully queued {item['name']} (Hash: {res_hash})")
            added_count += 1
        else:
            LOGGER.warning(f"❌ Failed to queue {item['name']}")

    LOGGER.info(f"🎉 Pre-buffering complete: {added_count} new torrents queued in qBittorrent.")

if __name__ == "__main__":
    main()
