#!/usr/bin/env python3
import json
import re
import urllib.request
from html import unescape

with open('/home/administrator/starfall/data/hindianime_tamil_catalogue.json') as f:
    cat = json.load(f)

# Filter out excluded series
excluded = ['doraemon', 'dragon ball', 'dragonball', 'kinnikuman', 'shinchan', 'shin-chan', 'crayon shin']

def is_excluded(title):
    tl = title.lower()
    return any(ex in tl for ex in excluded)

filtered = [item for item in cat if not is_excluded(item['title'])]
print(f"Total after excluding Doraemon, DB, Kinnikuman, Shinchan: {len(filtered)} (out of {len(cat)})")

# Test 10 sample posts for zip detection
for item in filtered[15:25]:
    title = item['title']
    url = item['posts'][0]['url']
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        html = unescape(urllib.request.urlopen(req, timeout=10).read().decode('utf-8', errors='ignore'))
        
        # Look for batch / zip sections
        # Often after "Batch" or "Zip" heading, or at the bottom table
        batch_match = re.findall(r'(?:batch|zip)[^<]*</h3>.*?(<table.*?</table>)', html, re.DOTALL | re.IGNORECASE)
        if not batch_match:
            batch_match = re.findall(r'(?:batch|zip)[^<]*</h2>.*?(<table.*?</table>)', html, re.DOTALL | re.IGNORECASE)
            
        print(f"\n--- {title} ---")
        if batch_match:
            print("  Found explicit Batch Table!")
            links = re.findall(r'<a[^>]+href=[\'"]([^\'"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^\'"]*)[\'"][^>]*>(.*?)</a>', batch_match[-1], re.DOTALL | re.IGNORECASE)
            for h, l in links:
                lbl = re.sub(r'<[^>]+>', '', l).strip()
                print(f"    Batch Button: [{lbl}] -> {h}")
        else:
            # Check all links
            links = re.findall(r'<a[^>]+href=[\'"]([^\'"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^\'"]*)[\'"][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
            zip_links = [(h, re.sub(r'<[^>]+>', '', l).strip()) for h, l in links if 'zip' in l.lower() or 'batch' in l.lower() or '10bit' in l.lower()]
            print(f"  All zip/batch candidate buttons ({len(zip_links)}):")
            for h, lbl in zip_links:
                print(f"    [{lbl}] -> {h}")
    except Exception as e:
        print(f"Error {title}: {e}")
