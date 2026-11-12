#!/usr/bin/env python3
import re
import json

with open('/home/administrator/starfall/data/target_candidates_clean.txt', 'r', encoding='utf-8') as f:
    text = f.read()

# Normalize broken nyaa links: 'https://nyaa .si/view/178 9220'
# 1. remove space before .si
text = text.replace('nyaa .si', 'nyaa.si')
# 2. remove space after /view/
text = re.sub(r'nyaa\.si/view/(\d+)\s+(\d+)', r'nyaa.si/view/\1\2', text)
text = re.sub(r'nyaa\.si/view/(\d+)', r'nyaa.si/view/\1', text)

with open('/home/administrator/starfall/data/target_clean_fixed.txt', 'w', encoding='utf-8') as f:
    f.write(text)

urls = set(re.findall(r'https://nyaa\.si/view/\d+', text))
print(f"Total unique Nyaa URLs found: {len(urls)}")
for u in sorted(list(urls)):
    print("  ", u)
