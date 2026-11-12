#!/usr/bin/env python3
import re

with open('/home/administrator/starfall/data/target_anime_pages.txt', 'r', encoding='utf-8') as f:
    content = f.read()

pages = content.split('==============================')
for p in pages:
    if 'PAGE' in p:
        # Get page header
        first_line = p.strip().split('\n')[0]
        cleaned_page = re.sub(r'\s+', ' ', p)
        print(f"\n{'#'*60}\n{first_line}\n{'#'*60}")
        print(cleaned_page[:1500])
