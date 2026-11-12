#!/usr/bin/env python3
"""
Autonomous 720p Tamil Audio Extractor & Audio Bank Filler
- Excludes: Doraemon, Dragon Ball, Kinnikuman, Shinchan
- Strictly accepts batch .zip archives only (rejects individual .mkv episodes)
- Resolves GDShare directly via native generator, with fallback via DD Bypass Bot (/b)
- Unpacks .zip on NVMe, extracts ONLY Tamil audio track (aac/opus/ac3) to VPS Audio Bank
- Deduplicates Audio Bank automatically (removes older duplicates)
- Deletes temporary video files immediately after extraction
- Sends short 1-line update to "Nothing Do" Telegram group
"""

import os
import sys
import json
import time
import re
import shutil
import asyncio
import subprocess
import urllib.request
from html import unescape

# Add starfall root to sys.path for direct_link_generator
sys.path.insert(0, "/home/administrator/starfall")

try:
    from bot.helper.mirror_leech_utils.download_utils.direct_link_generator import gdshare as direct_gdshare
except Exception as e:
    print(f"Warning: Could not import direct_link_generator.gdshare: {e}")
    direct_gdshare = None

from pyrogram import Client
from pyrogram.enums import MessageEntityType

# Configuration
SESSION_PATH = "/home/administrator/starfall/data/test_user_session.txt"
CATALOGUE_PATH = "/home/administrator/starfall/data/hindianime_tamil_catalogue.json"
CACHE_PATH = "/home/administrator/starfall/data/auto_extracted_anime.json"
BANK_DIR = "/home/administrator/starfall/downloads/vt_audio_bank/8814651149"
TEMP_DIR = "/home/administrator/starfall/downloads/temp_tamil_proc"
NOTHING_DO_CHAT_ID = -1004486743997

EXCLUDED_ANIMES = ["doraemon", "dragon ball", "kinnikuman", "shinchan", "shin-chan", "shin chan"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def is_excluded(title):
    t = title.lower()
    return any(ex in t for ex in EXCLUDED_ANIMES)

SOLVER_URL = "http://127.0.0.1:8192/solve"

def resolve_gateway(gateway_url):
    payload = json.dumps({"url": gateway_url}).encode("utf-8")
    req = urllib.request.Request(
        SOLVER_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=50) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "success":
                mirrors = data.get("mirrors") or {}
                gdshare = mirrors.get("gdshare")
                filepress = mirrors.get("filepress") or mirrors.get("filebee")
                gdflix = mirrors.get("gdflix")
                
                # Determine best mirror: gdshare primary, else fallback
                if gdshare:
                    best = gdshare
                    mirror_type = "gdshare"
                elif filepress:
                    best = filepress
                    mirror_type = "fallback"
                elif gdflix:
                    best = gdflix
                    mirror_type = "fallback"
                elif mirrors:
                    best = next(iter(mirrors.values()))
                    mirror_type = "fallback"
                else:
                    best = None
                    mirror_type = None

                title = data.get("title", "")
                return {
                    "url": best,
                    "mirror_type": mirror_type,
                    "title": title,
                    "mirrors": mirrors
                }
    except Exception as e:
        print(f"    [Solver Error] {e}")
    return None

def extract_series_and_ep(filename):
    m = re.search(r'^(.*?)[Ss](\d+)[Ee](\d+)', filename)
    if m:
        raw_prefix = m.group(1)
        clean_title = re.sub(r'\[.*?\]', '', raw_prefix)
        clean_title = re.sub(r'[^a-zA-Z0-9]', '', clean_title).lower()
        return clean_title, int(m.group(2)), int(m.group(3))
    return None, None, None

def deduplicate_bank(bank_dir, new_file):
    try:
        new_base = os.path.basename(new_file)
        new_series, new_s, new_e = extract_series_and_ep(new_base)
        new_sz = os.path.getsize(new_file) if os.path.exists(new_file) else 0

        for f in os.listdir(bank_dir):
            fp = os.path.join(bank_dir, f)
            if fp == new_file or not os.path.isfile(fp):
                continue
            
            f_series, fs, fe = extract_series_and_ep(f)
            # Rule 1: Same series AND same Season + Episode
            if new_series and f_series and new_series == f_series and fs == new_s and fe == new_e:
                try:
                    os.remove(fp)
                    print(f"      [Deduplicate] Removed duplicate of same anime ({f})")
                    continue
                except Exception:
                    pass

            # Rule 2: Identical file size (exact duplicate byte stream)
            if new_sz > 1000:
                try:
                    if os.path.getsize(fp) == new_sz:
                        os.remove(fp)
                        print(f"      [Deduplicate] Removed identical byte-size duplicate: {f}")
                except Exception:
                    pass
    except Exception:
        pass

async def download_file(url, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        try: os.remove(out_path)
        except Exception: pass
    cmd = ["aria2c", "-s", "16", "-x", "16", "-j", "16", "--min-split-size=1M", "-o", os.path.basename(out_path), "--dir", os.path.dirname(out_path), url]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.communicate()
    return os.path.exists(out_path) and os.path.getsize(out_path) > 1024*1024

async def extract_tamil_from_folder(extract_dir, anime_title, season_num):
    os.makedirs(BANK_DIR, exist_ok=True)
    saved_count = 0
    mkv_files = []
    for root, _, files in os.walk(extract_dir):
        for f in files:
            if f.lower().endswith(".mkv"):
                mkv_files.append(os.path.join(root, f))

    mkv_files.sort()
    for mkv in mkv_files:
        try:
            cmd_probe = ["mkvmerge", "-J", mkv]
            res = subprocess.run(cmd_probe, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                continue
            info = json.loads(res.stdout)
            tracks = info.get("tracks", [])

            # Find Tamil audio track
            tam_track_id = None
            tam_codec = "m4a"
            for t in tracks:
                if t.get("type") == "audio":
                    props = t.get("properties", {})
                    lang = (props.get("language") or "").lower()
                    name = (props.get("track_name") or "").lower()
                    codec = (t.get("codec") or "").lower()
                    if any(k in lang or k in name for k in ["tam", "tamil", "ta"]):
                        tam_track_id = t.get("id")
                        if "aac" in codec: tam_codec = "m4a"
                        elif "opus" in codec: tam_codec = "opus"
                        elif "eac3" in codec: tam_codec = "eac3"
                        elif "ac3" in codec: tam_codec = "ac3"
                        elif "flac" in codec: tam_codec = "flac"
                        break

            if tam_track_id is not None:
                base_name = os.path.splitext(os.path.basename(mkv))[0]
                out_name = f"Tamil - {base_name}.{tam_codec}"
                out_path = os.path.join(BANK_DIR, out_name)

                # Extract track
                cmd_ext = ["mkvextract", "tracks", mkv, f"{tam_track_id}:{out_path}"]
                ext_res = subprocess.run(cmd_ext, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if ext_res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                    deduplicate_bank(BANK_DIR, out_path)
                    saved_count += 1
                else:
                    if ext_res.returncode != 0:
                        print(f"      [mkvextract error] {ext_res.stderr.strip()}")
        except Exception as e:
            print(f"      [MKV Extract Error] {mkv}: {e}")

    return saved_count

def extract_urls_from_msg(msg):
    urls = []
    # 1. Text link entities
    if msg.entities:
        for ent in msg.entities:
            if ent.type == MessageEntityType.TEXT_LINK and getattr(ent, "url", None):
                urls.append(ent.url)
            elif ent.type == MessageEntityType.URL and msg.text:
                url_text = msg.text[ent.offset:ent.offset + ent.length]
                urls.append(url_text)
    # 2. Raw regex search in text and caption
    raw_text = (msg.text or "") + " " + (msg.caption or "")
    regex_urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', raw_text)
    for u in regex_urls:
        if u not in urls:
            urls.append(u)
    return urls

async def main():
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(BANK_DIR, exist_ok=True)
    # Ensure permission on bank directory
    try:
        subprocess.run(["chmod", "-R", "777", BANK_DIR], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    with open(SESSION_PATH, "r", encoding="utf-8") as f:
        sess = f.read().strip()

    app = Client("tamil_audio_bot", session_string=sess, in_memory=True)
    catalogue = load_json(CATALOGUE_PATH, [])
    raw_cache = load_json(CACHE_PATH, {})

    # Reset cache entries where saved_audios was 0 (due to prior permission error)
    cache = {}
    for k, v in raw_cache.items():
        if isinstance(v, dict) and v.get("saved_audios", 0) > 0:
            cache[k] = v
    save_json(CACHE_PATH, cache)

    filtered = [item for item in catalogue if not is_excluded(item.get("title", ""))]

    print(f"=== Starting Autonomous Tamil Extraction Pipeline ===")
    print(f"Total series: {len(filtered)}")
    print(f"Already completed: {len(cache)}")

    async with app:
        for idx, item in enumerate(filtered, 1):
            title = item.get("title")
            posts = item.get("posts", [])

            for post in posts:
                post_url = post.get("url")
                season = post.get("season", 1)
                dedupe_key = f"{post_url}_720p_extracted"

                if dedupe_key in cache:
                    continue

                print(f"\n[{idx}/{len(filtered)}] Processing: '{title}' (Season {season})")

                # Fetch HTML
                try:
                    req = urllib.request.Request(post_url, headers=HEADERS)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        html = unescape(resp.read().decode("utf-8", errors="ignore"))
                except Exception as e:
                    print(f"  [Fetch Error]: {e}")
                    continue

                buttons = re.findall(
                    r'<a[^>]+href=[\'"]([^\'"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^\'"]*)[\'"][^>]*>(.*?)</a>',
                    html,
                    re.DOTALL | re.IGNORECASE
                )

                candidates = []
                for href, label in buttons:
                    lbl = re.sub(r'<[^>]+>', '', label).strip()
                    ll = lbl.lower()
                    if any(bad in ll for bad in ['telegram', 'join', 'watch online', 'stream', 'how to']):
                        continue
                    if '720p' in ll:
                        score = 0
                        if 'zip' in ll: score += 50
                        if 'batch' in ll: score += 40
                        if '10bit' in ll: score += 30
                        if 'x265' in ll or 'hevc' in ll: score += 20
                        candidates.append((score, href, lbl))

                candidates.sort(key=lambda x: x[0], reverse=True)
                if not candidates:
                    print(f"  [Skip] No 720p download button found.")
                    continue

                matched_zip = None
                mkv_rejections = 0
                for score, href, lbl in candidates:
                    if score == 0 and mkv_rejections >= 3:
                        break
                    resolved = resolve_gateway(href)
                    if not resolved or not resolved.get("url"):
                        continue
                    ftitle = resolved.get("title", "")
                    if not (ftitle.lower().endswith(".zip") or (".zip" in ftitle.lower() and not ftitle.lower().endswith(".mkv"))):
                        mkv_rejections += 1
                        continue
                    matched_zip = resolved
                    break

                if not matched_zip:
                    print(f"  [Backlog] No genuine batch .zip available.")
                    continue

                zip_mirror_url = matched_zip["url"]
                mirror_type = matched_zip["mirror_type"]
                print(f"  [FOUND ZIP BATCH]: {matched_zip['title']} ({mirror_type})")

                # Step 1: Direct Download URL Resolution
                direct_dl = None
                if mirror_type == "gdshare" and direct_gdshare:
                    try:
                        print("  Resolving GDShare direct link via native resolver...")
                        direct_dl = direct_gdshare(zip_mirror_url)
                    except Exception as ge:
                        print(f"  GDShare native direct resolution error: {ge}")

                # Step 2: Fallback via DD Bypass Bot in Telegram if needed
                if not direct_dl:
                    print("  Routing through DD Bypass Bot in 'Nothing Do'...")
                    cmd_msg = f"/b {zip_mirror_url}"
                    sent_m = await app.send_message(NOTHING_DO_CHAT_ID, cmd_msg)

                    # Listen for DD Bypass Bot response (wait up to 35s)
                    start_t = time.time()
                    while time.time() - start_t < 35:
                        await asyncio.sleep(2)
                        async for msg in app.get_chat_history(NOTHING_DO_CHAT_ID, limit=5):
                            if msg.reply_to_message_id == sent_m.id or (msg.from_user and "bypass" in (msg.from_user.first_name or "").lower()):
                                found_urls = extract_urls_from_msg(msg)
                                # Look for fast direct links
                                for u in found_urls:
                                    if any(c in u for c in ['r2.dev', 'xcloud', 'drive.google', 'mega.nz', 'pixeldrain', 'gofile', 'mediafire', 'userstorage.mega']):
                                        direct_dl = u
                                        break
                                if direct_dl: break
                        if direct_dl: break

                if not direct_dl:
                    print(f"  [Skip] Could not resolve direct link for '{title}'")
                    continue

                # Step 3: Download Zip to VPS
                zip_target = os.path.join(TEMP_DIR, "batch.zip")
                print(f"  Downloading batch zip to NVMe ({direct_dl[:60]}...)...")
                dl_ok = await download_file(direct_dl, zip_target)
                if not dl_ok:
                    print(f"  [Download Failed]")
                    continue

                # Step 4: Extract Zip
                extract_target = os.path.join(TEMP_DIR, "extracted")
                shutil.rmtree(extract_target, ignore_errors=True)
                os.makedirs(extract_target, exist_ok=True)
                print(f"  Unpacking zip...")
                res_7z = subprocess.run(["7z", "x", zip_target, f"-o{extract_target}", "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try: os.remove(zip_target)
                except Exception: pass

                # Step 5: Extract Tamil Audio Streams
                print(f"  Extracting Tamil audio streams to Audio Bank...")
                saved_count = await extract_tamil_from_folder(extract_target, title, season)
                shutil.rmtree(extract_target, ignore_errors=True)

                # Step 6: Mark Done & Notify
                cache[dedupe_key] = {"title": title, "season": season, "saved_audios": saved_count, "time": time.ctime()}
                save_json(CACHE_PATH, cache)

                msg_status = f"💾 <b>Saved {saved_count} audio file(s) for '{title} S{season}' to Audio Bank.</b>"
                try:
                    await app.send_message(NOTHING_DO_CHAT_ID, msg_status)
                except Exception:
                    pass
                print(f"  >>> SUCCESS: {msg_status}\n")

                await asyncio.sleep(2)

    print("\n=== Autonomous Pipeline Finished Successfully ===")

if __name__ == "__main__":
    asyncio.run(main())
