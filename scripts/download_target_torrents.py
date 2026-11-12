#!/usr/bin/env python3
"""
Starfall Target Anime Torrents Downloader
Downloads Judas 1080p HEVC BluRay batches for:
- My Hero Academia (S01-S07, Vigilantes)
- Attack on Titan (S01-S04, OADs)
- Fire Force (S01-S02)

Saves directly to NVMe storage. Muxing is held until user confirmation.
"""
import os
import subprocess
import time
import urllib.request

TORRENTS = [
    # My Hero Academia
    {"id": "1192841", "name": "MHA_S01_Judas", "size": "4.1 GiB"},
    {"id": "1210452", "name": "MHA_S02_Judas", "size": "7.9 GiB"},
    {"id": "1229871", "name": "MHA_S03_Judas", "size": "8.1 GiB"},
    {"id": "1233860", "name": "MHA_S04_Judas", "size": "6.8 GiB"},
    {"id": "1445670", "name": "MHA_S05_Judas", "size": "8.9 GiB"},
    {"id": "1655102", "name": "MHA_S06_Judas", "size": "9.2 GiB"},
    {"id": "1890041", "name": "MHA_S07_Judas", "size": "10.8 GiB"},
    {"id": "1996112", "name": "MHA_Vigilantes_Judas_v2", "size": "5.5 GiB"},
    # Attack on Titan
    {"id": "1187425", "name": "AoT_S01_Judas", "size": "8.2 GiB"},
    {"id": "1186714", "name": "AoT_S02_Judas", "size": "5.1 GiB"},
    {"id": "1182740", "name": "AoT_S03_Judas", "size": "6.8 GiB"},
    {"id": "1739210", "name": "AoT_S04_Final_Judas", "size": "12.9 GiB"},
    {"id": "1188102", "name": "AoT_OADs_Judas", "size": "2.8 GiB"},
    # Fire Force
    {"id": "1241102", "name": "Fire_Force_S01_Judas", "size": "8.2 GiB"},
    {"id": "1399812", "name": "Fire_Force_S02_Judas", "size": "8.4 GiB"},
]

TORRENT_DIR = "/home/administrator/starfall/data/torrents"
DOWNLOAD_DIR = "/home/administrator/starfall/data/torrents/downloads"

def fetch_and_queue():
    os.makedirs(TORRENT_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    headers = {"User-Agent": "Mozilla/5.0"}
    torrent_files = []

    print("=== 1. Fetching .torrent files from Nyaa ===")
    for item in TORRENTS:
        tid = item["id"]
        tname = item["name"]
        tsize = item["size"]
        tpath = os.path.join(TORRENT_DIR, f"{tname}_{tid}.torrent")
        
        if not os.path.exists(tpath):
            url = f"https://nyaa.si/download/{tid}.torrent"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    with open(tpath, "wb") as f:
                        f.write(resp.read())
                print(f"Downloaded: {tname} ({tsize}) -> {tpath}")
            except Exception as e:
                print(f"Failed downloading {tname} ({tid}): {e}")
        else:
            print(f"Cached: {tname} -> {tpath}")
            
        if os.path.exists(tpath):
            torrent_files.append(tpath)

    print(f"\nTotal ready .torrent files: {len(torrent_files)} / {len(TORRENTS)}")
    
    # Check if aria2c daemon is running or launch batch download
    print("\n=== 2. Starting background torrent download ===")
    cmd = [
        "aria2c",
        "--enable-rpc=false",
        f"--dir={DOWNLOAD_DIR}",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=10M",
        "--max-overall-download-limit=0",
        "--seed-time=0",
        "--summary-interval=30",
        "--daemon=true",
        f"--log={TORRENT_DIR}/aria2_downloads.log"
    ] + torrent_files

    try:
        subprocess.run(cmd, check=True)
        print("Aria2c download daemon launched successfully!")
        print(f"Download location: {DOWNLOAD_DIR}")
        print(f"Log output: {TORRENT_DIR}/aria2_downloads.log")
    except Exception as e:
        print("Aria2c launch error:", e)

    print("\n[NOTE] Video files are downloading. MUXING AND UPLOADING ARE HELD.")
    print("Waiting for explicit user confirmation before mux engine startup.")

if __name__ == "__main__":
    fetch_and_queue()
