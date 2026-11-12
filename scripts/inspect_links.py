#!/usr/bin/env python3
import json
import re
import urllib.request
from html import unescape

with open('/home/administrator/starfall/data/hindianime_tamil_catalogue.json') as f:
    cat = json.load(f)

for item in cat[:10]:
    title = item['title']
    url = item['posts'][0]['url']
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        html = unescape(urllib.request.urlopen(req, timeout=10).read().decode('utf-8', errors='ignore'))
        links = re.findall(r'<a[^>]+href=[\'"]([^\'"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^\'"]*)[\'"][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
        print(f"=== {title} ===")
        for h, l in links:
            lbl = re.sub(r'<[^>]+>', '', l).strip()
            print(f"  [{lbl}] -> {h}")
    except Exception as e:
        print(f"Error {title}: {e}")
