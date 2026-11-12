#!/usr/bin/env python3
import json
import subprocess
import os

url = "https://pub-13118a2affb747cb9bd64b9fa4257e32.r2.dev/uploads/411694/HindiAnimeZone.com_365_Days_to_the_Wedding_S01_720p_x265_10bit_WEB-DL_Multi_Audio_ESub.zip"
temp_zip = "/home/administrator/starfall/downloads/temp_pipeline/test.zip"
extract_dir = "/home/administrator/starfall/downloads/temp_pipeline/test_ext"

# Download first 50MB using curl to check structure or zip header
print("Testing 7z l on remote or partial download...")
cmd = f"curl -s -L -r 0-50000000 '{url}' -o {temp_zip}"
os.system(cmd)
print("File size:", os.path.getsize(temp_zip) if os.path.exists(temp_zip) else 0)

# Check 7z l
res = subprocess.run(["7z", "l", temp_zip], stdout=subprocess.PIPE, text=True)
print("7z list output:")
print(res.stdout[:1000])

if os.path.exists(temp_zip): os.remove(temp_zip)
