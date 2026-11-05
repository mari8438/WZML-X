#!/usr/bin/env python3
"""
Nyaa.si Deep Source Research Engine:
Scans the 149 Tamil Anime Catalogue on nyaa.si for 1080p BluRay (HEVC & AV1) batches.
Selects up to 4 candidate releases per anime ranked by encoder quality.
Outputs:
1. /home/administrator/starfall/data/nyaa_sources_report.json
2. /home/administrator/starfall/data/nyaa_sources_report.md
"""

import asyncio
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Any

CATALOGUE_PATH = "/home/administrator/starfall/data/hindianime_tamil_catalogue.json"
OUT_JSON = "/home/administrator/starfall/data/nyaa_sources_report.json"
OUT_MD = "/home/administrator/starfall/data/nyaa_sources_report.md"

HEVC_PRIORITY = {
    "YURASUKA": 1, "DB": 2, "ANIMENCODES": 2, "JUDAS": 3, "DKB": 4,
    "NEOHEVC": 5, "SAM": 6, "SEADEX": 6, "ASW": 7, "EMBER": 8,
    "TENRAI-SENSEI": 9, "TENRAI": 9, "ANIME TIME": 10, "ANIMETIME": 10,
    "CLEO": 11, "CTR": 12, "BEATRICE-RAWS": 13, "KAWAII": 14
}

AV1_PRIORITY = {
    "BREEZE": 1, "LASTBREEZE": 1, "IRONCLAD": 2, "SOKUDO": 3, "TRIX": 4,
    "NEKOTRIX": 4, "NEODESU": 5, "LUMEN": 6, "ANIME-AV1": 7, "OMNIPOTENT500": 7,
    "SAVI0R": 8, "SAVI0R-PRO": 8
}

TRACKERS = [
    "http://nyaa.tracker.wf:7777/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://exodus.desync.com:6969/announce",
]

def parse_size_bytes(size_str: str) -> int:
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]i?B)\s*$", size_str.strip(), re.I)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {
        "KIB": 1024, "KB": 1000,
        "MIB": 1024 ** 2, "MB": 1000 ** 2,
        "GIB": 1024 ** 3, "GB": 1000 ** 3,
        "TIB": 1024 ** 4, "TB": 1000 ** 4,
    }.get(unit, 1024 ** 3)
    return int(val * mult)

def is_episode_size_safe(size_str: str, ep_count: int = 12) -> bool:
    """
    Strict Telegram rule: Single episode must NEVER exceed 2.0 GB (2,048 MB).
    Average per-episode size must be <= 2,000 MB.
    """
    total_bytes = parse_size_bytes(size_str)
    if total_bytes == 0:
        return True
    ep_count = max(1, ep_count)
    per_ep_gb = (total_bytes / (1024 ** 3)) / ep_count
    return per_ep_gb <= 2.0
    known = list(HEVC_PRIORITY.keys()) + list(AV1_PRIORITY.keys())
    for grp in known:
        if re.search(r"[\W_]" + re.escape(grp) + r"([\W_]|$)", title, re.IGNORECASE) or title.startswith(f"[{grp}]"):
            return grp.title()
    m = re.match(r"^\[([A-Za-z0-9_.\-]+)\]", title.strip())
    return m.group(1).strip() if m else "Unknown"

def detect_codec(title: str) -> str:
    t = title.upper()
    if "AV1" in t:
        return "AV1"
    if any(k in t for k in ["HEVC", "X265", "H.265", "H265", "265"]):
        return "HEVC"
    return "UNKNOWN"

def is_bluray(title: str) -> bool:
    t = title.upper()
    if "REMUX" in t:
        return False
    return bool(re.search(r"\b(BD|BLURAY|BD-RIP|BDRIP|BLU-RAY)\b", t))

def search_nyaa(query: str) -> List[Dict[str, Any]]:
    url = f"https://nyaa.si/?page=rss&f=0&c=1_2&q={urllib.parse.quote_plus(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    req = urllib.request.Request(url, headers=headers)
    results = []
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
        channel = root.find("channel")
        if channel is None:
            return results
        for item in channel.findall("item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            guid = item.findtext("guid", "")
            info_hash = ""
            seeders = 0
            size = ""
            for child in item:
                if child.tag.endswith("infoHash"):
                    info_hash = child.text or ""
                elif child.tag.endswith("seeders"):
                    try: seeders = int(child.text or "0")
                    except ValueError: seeders = 0
                elif child.tag.endswith("size"):
                    size = child.text or ""

            if not is_bluray(title):
                continue
            if not is_episode_size_safe(size, 12):
                continue
            codec = detect_codec(title)
            if codec not in ("HEVC", "AV1"):
                continue

            grp = extract_group(title)
            rank = 99
            if codec == "HEVC":
                rank = HEVC_PRIORITY.get(grp.upper(), 50)
            elif codec == "AV1":
                rank = AV1_PRIORITY.get(grp.upper(), 50)

            tr_str = "&tr=".join([urllib.parse.quote_plus(tr) for tr in TRACKERS])
            magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote_plus(title)}&tr={tr_str}" if info_hash else ""

            results.append({
                "title": title,
                "group": grp,
                "codec": codec,
                "quality": "1080p BluRay",
                "size": size,
                "seeders": seeders,
                "rank": rank,
                "torrent_url": link,
                "page_url": guid,
                "info_hash": info_hash,
                "magnet": magnet,
            })
    except Exception as e:
        print(f"Error querying Nyaa for '{query}': {e}", file=sys.stderr)
    return results

def main():
    if not os.path.exists(CATALOGUE_PATH):
        print(f"Catalogue not found at {CATALOGUE_PATH}")
        return

    with open(CATALOGUE_PATH, "r", encoding="utf-8") as f:
        catalogue = json.load(f)

    print(f"Loaded {len(catalogue)} anime entries for Nyaa BluRay source research.")
    report_data = []

    for idx, item in enumerate(catalogue, 1):
        title = item.get("title", "")
        seasons = item.get("seasons", [1])
        print(f"[{idx:03d}/{len(catalogue):03d}] Researching: {title} (Seasons: {seasons}) ...")

        anime_sources = {"title": title, "seasons_data": []}
        for s in seasons:
            q = f"{title} Season {s} 1080p" if s > 1 else f"{title} 1080p"
            raw_results = search_nyaa(q)
            # Sort by rank ascending, then seeders descending
            raw_results.sort(key=lambda x: (x["rank"], -x["seeders"]))
            # Select top 4 candidate releases
            top_4 = raw_results[:4]
            anime_sources["seasons_data"].append({
                "season": s,
                "query": q,
                "candidates_found": len(raw_results),
                "top_sources": top_4
            })
        report_data.append(anime_sources)

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("# Nyaa.si 1080p BluRay Source Video Research Report\n\n")
        f.write(f"Total Anime Researched: {len(report_data)}\n\n")
        for item in report_data:
            f.write(f"## {item['title']}\n\n")
            for sd in item["seasons_data"]:
                f.write(f"### Season {sd['season']} (Query: `{sd['query']}`)\n")
                if not sd["top_sources"]:
                    f.write("*No direct 1080p BluRay HEVC/AV1 batch found on Nyaa.*\n\n")
                    continue
                f.write("| # | Release Group | Codec | Size | Seeders | Nyaa Release Title |\n")
                f.write("|---|---|---|---|---|---|\n")
                for i, src in enumerate(sd["top_sources"], 1):
                    f.write(f"| {i} | **{src['group']}** | `{src['codec']}` | {src['size']} | {src['seeders']} | [{src['title']}]({src['page_url']}) |\n")
                f.write("\n")

    print(f"\n✅ Research Complete! Output saved to:\n  - {OUT_JSON}\n  - {OUT_MD}")

if __name__ == "__main__":
    main()
