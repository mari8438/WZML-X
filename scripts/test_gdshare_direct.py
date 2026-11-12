#!/usr/bin/env python3
import sys
sys.path.insert(0, '/home/administrator/starfall')
from bot.helper.mirror_leech_utils.download_utils.direct_link_generator import gdshare

test_url = "https://gdshare.top/download/Q172ayB9ETtFH1bxwcy1WQ"
try:
    res = gdshare(test_url)
    print("DIRECT_LINK_SUCCESS:", res)
except Exception as e:
    print("DIRECT_LINK_ERROR:", type(e).__name__, e)
