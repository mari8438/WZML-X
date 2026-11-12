#!/usr/bin/env python3
import re
import json

with open('/home/administrator/starfall/data/target_clean_fixed.txt', 'r', encoding='utf-8') as f:
    text = f.read()

# Match every '1 (Top Pick)' block
matches = re.finditer(r'1\s*\(Top Pick\)(.{50,500}?)(?=2\s+[A-Za-z0-9]|\Z)', text, re.DOTALL)

items = []
for m in matches:
    block = m.group(1)
    # find nyaa url
    url_m = re.search(r'https://nyaa\.si/view/\d+', block)
    url = url_m.group(0) if url_m else ""
    # find size
    size_m = re.search(r'([0-9\.]+\s+GiB)', block)
    size = size_m.group(1) if size_m else ""
    # find title
    title_m = re.search(r'\[(.*?)\]', block)
    title = title_m.group(1) if title_m else ""
    
    # Check if one of the 3 target anime
    bl = block.lower()
    if any(k in bl for k in ['hero', 'shingeki', 'titan', 'enen', 'fire force']):
        items.append({
            "title": title,
            "size": size,
            "url": url,
            "raw": block[:120]
        })

print(f"Extracted {len(items)} Target Anime Top Picks:")
for it in items:
    print(f"- {it['title']} | {it['size']} | {it['url']}")

with open('/home/administrator/starfall/data/top_picks_clean.json', 'w', encoding='utf-8') as f:
    json.dump(items, f, indent=2)
