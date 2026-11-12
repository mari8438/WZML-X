#!/usr/bin/env python3
import json
import urllib.request
import re
from html import unescape

def test():
    with open('/home/administrator/starfall/data/hindianime_tamil_catalogue.json', 'r', encoding='utf-8') as f:
        catalogue = json.load(f)

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    # Test first 5 anime series
    for item in catalogue[:5]:
        title = item.get('title')
        posts = item.get('posts', [])
        print(f"\n==================== {title} ({len(posts)} posts) ====================")
        for p in posts:
            p_url = p.get('url')
            p_season = p.get('season')
            print(f"Checking URL: {p_url} (Season {p_season})")
            try:
                req = urllib.request.Request(p_url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode('utf-8', errors='ignore')
                    html = unescape(html)
                
                # Find all download links / buttons
                buttons = re.findall(r'<a[^>]+href=[\'"]([^\'"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^\'"]*)[\'"][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
                
                found_720p = []
                for href, label in buttons:
                    clean_label = re.sub(r'<[^>]+>', '', label).strip()
                    ll = clean_label.lower()
                    hl = href.lower()
                    if any(bad in ll for bad in ['telegram', 'join', 'watch online', 'stream', 'how to']):
                        continue
                    if '720p' in ll or '720p' in hl:
                        found_720p.append((clean_label, href))
                
                print(f"  Found {len(found_720p)} 720p link(s):")
                for lbl, hr in found_720p:
                    print(f"    - [{lbl}] -> {hr}")

            except Exception as e:
                print(f"  Fetch error: {e}")

if __name__ == "__main__":
    test()
