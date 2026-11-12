#!/usr/bin/env python3
import re
import json

log_path = "/home/administrator/starfall/data/zip_dispatch.log"
with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

seen = set()
failures = []
current_anime = "Unknown"

for line in lines:
    m = re.search(r"\[\d+/\d+\] Checking: '([^']+)'", line)
    if m:
        current_anime = m.group(1)
    
    if "[Backlog]" in line or "[Skip]" in line:
        reason = "Only .mkv individual files (no .zip batch)" if "[Backlog]" in line else "No 720p button found on post"
        key = (current_anime, reason)
        if key not in seen:
            seen.add(key)
            failures.append({"title": current_anime, "reason": reason})

print(f"Total Failed Anime: {len(failures)}")
for idx, item in enumerate(failures, 1):
    print(f"{idx}. {item['title']} -> {item['reason']}")
