#!/usr/bin/env python3
"""
HindiAnimeZone 720p Batch Zip Dispatcher for Leech Bot
- Excludes: Doraemon, Dragon Ball, Kinnikuman, Shinchan
- Strictly filters for .ZIP batch files (rejects .mkv single episodes)
- Sends via Test User Session
- Formats:
    GDShare -> /bleech <link1> <link2> ... <up to 20 links>
    Fallback (FilePress / GDFlix) -> /b <link1> <link2> ... <up to 20 links>
"""
import asyncio
import json
import os
import re
import sys
import urllib.request
from html import unescape
from pyrogram import Client

NOTHING_DO_CHAT_ID = -1004486743997
CATALOGUE_PATH = "/home/administrator/starfall/data/hindianime_tamil_catalogue.json"
SENT_CACHE_PATH = "/home/administrator/starfall/data/nothingdo_zip_sent.json"
ERROR_MD_PATH = "/home/administrator/starfall/data/hindianime_zip_errors.md"
SESSION_PATH = "/home/administrator/starfall/data/test_user_session.txt"
SOLVER_URL = "http://127.0.0.1:8192/solve"

EXCLUDED_KEYWORDS = [
    "doraemon",
    "dragon ball",
    "dragonball",
    "kinnikuman",
    "shinchan",
    "shin-chan",
    "crayon shin"
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def is_excluded(title):
    tl = title.lower()
    return any(k in tl for k in EXCLUDED_KEYWORDS)

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

async def send_command_batch(app, cmd_prefix, links):
    """Sends a batch of links (up to 20) with command prefix (/bleech or /b)"""
    if not links:
        return
    text = f"{cmd_prefix} " + " ".join(links)
    print(f"\n[Telegram Send] Sending {len(links)} link(s) with '{cmd_prefix}':")
    try:
        await app.send_message(NOTHING_DO_CHAT_ID, text)
        print(f"  >>> Successfully sent {len(links)} link(s) to 'Nothing Do'!")
    except Exception as e:
        print(f"  [!] Failed to send message: {e}")

async def run_dispatcher():
    if not os.path.exists(CATALOGUE_PATH):
        print(f"Catalogue not found at {CATALOGUE_PATH}")
        return

    with open(SESSION_PATH, "r", encoding="utf-8") as f:
        sess_str = f.read().strip()

    catalogue = load_json(CATALOGUE_PATH, [])
    sent_keys = set(load_json(SENT_CACHE_PATH, []))
    errors = []

    # Filter out excluded anime series
    filtered_cat = [item for item in catalogue if not is_excluded(item.get("title", ""))]

    print(f"=== Starting 720p Batch Zip Dispatcher ===")
    print(f"Total anime in catalogue: {len(catalogue)}")
    print(f"Active anime (after exclusions): {len(filtered_cat)}")
    print(f"Previously sent zip batches: {len(sent_keys)}")

    app = Client("nothingdo_zip_bot", session_string=sess_str, in_memory=True)

    bleech_queue = []
    b_queue = []

    async with app:
        for idx, item in enumerate(filtered_cat, 1):
            title = item.get("title")
            posts = item.get("posts", [])

            for post in posts:
                post_url = post.get("url")
                season = post.get("season", 1)
                dedupe_key = f"{post_url}_720p_zip"

                if dedupe_key in sent_keys:
                    continue

                print(f"\n[{idx}/{len(filtered_cat)}] Checking: '{title}' (Season {season})")

                # 1. Fetch post HTML
                try:
                    req = urllib.request.Request(post_url, headers=HEADERS)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        html = unescape(resp.read().decode("utf-8", errors="ignore"))
                except Exception as e:
                    print(f"  [Fetch Error]: {e}")
                    errors.append({"title": title, "season": season, "url": post_url, "reason": f"Fetch error: {e}"})
                    continue

                # 2. Extract potential download buttons
                buttons = re.findall(
                    r'<a[^>]+href=[\'"]([^\'"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^\'"]*)[\'"][^>]*>(.*?)</a>',
                    html,
                    re.DOTALL | re.IGNORECASE
                )

                # Prioritize explicit 720p batch/zip buttons
                candidates = []
                for href, label in buttons:
                    lbl = re.sub(r'<[^>]+>', '', label).strip()
                    ll = lbl.lower()
                    if any(bad in ll for bad in ['telegram', 'join', 'watch online', 'stream', 'how to']):
                        continue
                    if '720p' in ll:
                        score = 0
                        if 'zip' in ll:
                            score += 50
                        if 'batch' in ll:
                            score += 40
                        if '10bit' in ll:
                            score += 30
                        if 'x265' in ll or 'hevc' in ll:
                            score += 20
                        candidates.append((score, href, lbl))

                candidates.sort(key=lambda x: x[0], reverse=True)

                if not candidates:
                    print(f"  [Skip] No 720p download button found for '{title}'")
                    errors.append({"title": title, "season": season, "url": post_url, "reason": "No 720p button found"})
                    continue

                # 3. Resolve candidates until we find a genuine .ZIP file
                found_zip = False
                for score, href, lbl in candidates:
                    print(f"  Testing candidate: [{lbl}] -> {href}")
                    resolved = resolve_gateway(href)
                    if not resolved or not resolved.get("url"):
                        continue

                    file_title = resolved.get("title", "").strip()
                    file_url = resolved["url"]
                    mirror_type = resolved["mirror_type"]

                    print(f"    Resolved file title: '{file_title}' | URL: {file_url}")

                    # Strict ZIP validation:
                    # Must end with .zip or contain .zip (reject .mkv, .mp4, etc.)
                    is_zip = file_title.lower().endswith(".zip") or (".zip" in file_title.lower() and not file_title.lower().endswith(".mkv"))
                    if not is_zip:
                        print(f"    [Rejected] Not a zip file ('{file_title}'). Skipping.")
                        continue

                    found_zip = True
                    print(f"    [MATCHED ZIP BATCH] Type: {mirror_type} | Link: {file_url}")

                    # Add to appropriate queue
                    if mirror_type == "gdshare":
                        bleech_queue.append(file_url)
                        if len(bleech_queue) >= 20:
                            await send_command_batch(app, "/bleech", bleech_queue)
                            bleech_queue = []
                            await asyncio.sleep(5)
                    else:
                        b_queue.append(file_url)
                        if len(b_queue) >= 20:
                            await send_command_batch(app, "/b", b_queue)
                            b_queue = []
                            await asyncio.sleep(5)

                    sent_keys.add(dedupe_key)
                    save_json(SENT_CACHE_PATH, list(sent_keys))
                    break

                if not found_zip:
                    print(f"  [Backlog] No genuine .zip batch found for '{title}' (all options were .mkv or unresolvable).")
                    errors.append({"title": title, "season": season, "url": post_url, "reason": "Only .mkv individual files found / No batch .zip"})

                await asyncio.sleep(1)

        # Flush remaining queued links
        if bleech_queue:
            await send_command_batch(app, "/bleech", bleech_queue)
            await asyncio.sleep(5)
        if b_queue:
            await send_command_batch(app, "/b", b_queue)

    # Write errors markdown
    with open(ERROR_MD_PATH, "w", encoding="utf-8") as f:
        f.write("# HindiAnimeZone Manual Upload Backlog (Missing 720p Zip Batches)\n\n")
        f.write(f"Total anime requiring manual handling: **{len(errors)}**\n\n")
        f.write("| Anime Title | Season | Reason | URL |\n")
        f.write("| :--- | :--- | :--- | :--- |\n")
        for err in errors:
            f.write(f"| {err.get('title')} | S{err.get('season', 1)} | {err.get('reason')} | [Post Link]({err.get('url')}) |\n")

    print(f"\n[Done] All zip batches dispatched! Error backlog written to {ERROR_MD_PATH}")

if __name__ == "__main__":
    asyncio.run(run_dispatcher())
