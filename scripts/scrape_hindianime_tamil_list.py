#!/usr/bin/env python3
"""
Scrapes all Tamil Dubbed Anime series from HindiAnimeZone (hindianimeszone.com)
Using standard library only (urllib.request + json + re).
Zero external dependencies!
Outputs:
1. /home/administrator/starfall/data/hindianime_tamil_catalogue.json
2. /home/administrator/starfall/data/hindianime_tamil_catalogue.md
"""

import json
import os
import re
import urllib.request
from typing import Dict, List, Any

BASE_URL = "https://hindianimeszone.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

def clean_anime_title(raw_title: str) -> str:
    # unescape html entities
    raw_title = raw_title.replace("&#8211;", "-").replace("&#8217;", "'").replace("&amp;", "&").replace("&#038;", "&")
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", raw_title)
    qual_pattern = r"\b(1080p|720p|480p|10bit|8bit|BluRay|BDRip|WEB-?DL|WEBRip|HDTV|[x×]26[45]|HEVC|AV1|Tamil|Telugu|Hindi|Malayalam|English|Multi-?Audio|Dual-?Audio|ESub|DDP\d\.\d|Season\s*\d+|S\d+)\b"
    t = re.sub(qual_pattern, " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(19|20)\d{2}\b", " ", t)
    t = re.sub(r"[^\w\s-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def scrape_tamil_list():
    out_json = "/home/administrator/starfall/data/hindianime_tamil_catalogue.json"
    out_md = "/home/administrator/starfall/data/hindianime_tamil_catalogue.md"
    os.makedirs(os.path.dirname(out_json), exist_ok=True)

    print("🔎 Starting crawl of Tamil dubbed anime from hindianimeszone.com API (Category 11 = Tamil)...")
    
    anime_items: List[Dict[str, Any]] = []
    seen_urls = set()

    for page in range(1, 10):
        api_url = f"{BASE_URL}/wp-json/wp/v2/posts?categories=11&per_page=50&page={page}"
        try:
            req = urllib.request.Request(api_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as response:
                if response.status != 200:
                    break
                posts = json.loads(response.read().decode("utf-8"))
                if not posts:
                    break
                for p in posts:
                    title_raw = p.get("title", {}).get("rendered", "")
                    link = p.get("link", "")
                    if link in seen_urls:
                        continue
                    seen_urls.add(link)
                    
                    s_match = re.search(r"Season\s*(\d+)|S(\d+)", title_raw, re.IGNORECASE)
                    s_num = int(s_match.group(1) or s_match.group(2)) if s_match else 1
                    
                    anime_items.append({
                        "title_raw": title_raw,
                        "clean_title": clean_anime_title(title_raw),
                        "season": s_num,
                        "url": link,
                    })
                print(f"  • Scraped API page {page}: {len(posts)} posts (Total: {len(anime_items)})")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                print(f"  Reached end of category posts at page {page}.")
                break
            print(f"  HTTP error on page {page}: {e}")
            break
        except Exception as e:
            print(f"  Crawl stopped on page {page}: {e}")
            break

    # Group by clean title
    grouped = {}
    for it in anime_items:
        ct = it["clean_title"]
        if not ct:
            continue
        if ct not in grouped:
            grouped[ct] = {
                "anime": ct,
                "seasons": set(),
                "posts": []
            }
        grouped[ct]["seasons"].add(it["season"])
        grouped[ct]["posts"].append(it)

    catalogue = []
    for k, v in sorted(grouped.items()):
        catalogue.append({
            "title": k,
            "seasons": sorted(list(v["seasons"])),
            "posts_count": len(v["posts"]),
            "posts": v["posts"],
        })

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(catalogue, f, indent=2)

    with open(out_md, "w", encoding="utf-8") as f:
        f.write("# HindiAnimeZone — Tamil Dubbed Anime Catalogue\n\n")
        f.write(f"Total Unique Tamil Dubbed Series: {len(catalogue)}\n\n")
        f.write("| # | Anime Title | Seasons Found | Releases |\n")
        f.write("|---|---|---|---|\n")
        for i, entry in enumerate(catalogue, 1):
            s_str = ", ".join([f"S{s}" for s in entry["seasons"]])
            f.write(f"| {i} | **{entry['title']}** | {s_str} | {entry['posts_count']} release(s) |\n")

    print(f"\n✅ Finished! Found {len(catalogue)} unique Tamil anime series across {len(anime_items)} releases.")
    print(f"💾 JSON Catalogue saved to: {out_json}")
    print(f"📄 Markdown Catalogue saved to: {out_md}")

if __name__ == "__main__":
    scrape_tamil_list()
