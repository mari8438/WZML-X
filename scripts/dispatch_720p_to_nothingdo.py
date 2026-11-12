#!/usr/bin/env python3
"""
HindiAnimeZone 720p 10-bit HEVC Zip Dispatcher
Extracts completed anime 720p 10bit HEVC zip links, resolves mirrors (gdshare -> filepress -> gdflix),
and dispatches them directly to the 'Nothing Do' Telegram group.
Failed items are logged to hindianime_errors.json for manual user upload.
"""
import asyncio
import json
import os
import re
import sys
import urllib.request
from html import unescape

sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

from bot.core.config_manager import Config
Config.load()
from pyrogram import Client

NOTHING_DO_CHAT_ID = -1004486743997
CATALOGUE_PATH = "/home/administrator/starfall/data/hindianime_tamil_catalogue.json"
SENT_CACHE_PATH = "/home/administrator/starfall/data/nothingdo_sent.json"
ERROR_LOG_PATH = "/home/administrator/starfall/data/hindianime_errors.json"
ERROR_MD_PATH = "/home/administrator/starfall/data/hindianime_errors.md"
SOLVER_URL = "http://127.0.0.1:8192/solve"

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
                # Prioritize: gdshare -> filepress -> gdflix
                best = (
                    mirrors.get("gdshare")
                    or mirrors.get("filepress")
                    or mirrors.get("gdflix")
                )
                if not best and mirrors:
                    best = next(iter(mirrors.values()))
                
                mirror_type = "GDShare" if "gdshare" in str(best).lower() else ("FilePress" if "file" in str(best).lower() else "GDFlix")
                return {
                    "url": best,
                    "mirror_type": mirror_type,
                    "all_mirrors": mirrors,
                    "title": data.get("title", "")
                }
    except Exception as e:
        print(f"    [Solver Error] {e}")
    return None

async def dispatch_all(limit=None):
    if not os.path.exists(CATALOGUE_PATH):
        print(f"Catalogue not found at {CATALOGUE_PATH}")
        return

    catalogue = load_json(CATALOGUE_PATH, [])
    sent = set(load_json(SENT_CACHE_PATH, []))
    errors = load_json(ERROR_LOG_PATH, [])

    print(f"=== Starting HindiAnimeZone 720p Dispatcher ===")
    print(f"Total anime in catalogue: {len(catalogue)}")
    print(f"Already sent: {len(sent)}")
    print(f"Target group: Nothing Do ({NOTHING_DO_CHAT_ID})")

    app = Client('nothing_do_dispatcher', api_id=Config.TELEGRAM_API, api_hash=Config.TELEGRAM_HASH, session_string=Config.USER_SESSION_STRING, in_memory=True)
    
    async with app:
        processed = 0
        for item in catalogue:
            title = item.get("title")
            posts = item.get("posts", [])
            
            for post in posts:
                post_url = post.get("url")
                season = post.get("season", 1)
                dedupe_key = f"{post_url}_720p"
                
                if dedupe_key in sent:
                    continue

                print(f"\nProcessing: '{title}' (Season {season})")
                print(f"  URL: {post_url}")

                # Fetch post HTML
                try:
                    req = urllib.request.Request(post_url, headers=HEADERS)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        html = unescape(resp.read().decode("utf-8", errors="ignore"))
                except Exception as e:
                    print(f"  [Fetch Error]: {e}")
                    errors.append({"title": title, "season": season, "url": post_url, "reason": f"Fetch error: {e}"})
                    save_json(ERROR_LOG_PATH, errors)
                    continue

                # Find 720p batch zip link
                buttons = re.findall(r'<a[^>]+href=[\'"]([^\'"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^\'"]*)[\'"][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
                
                target_gateway = None
                target_label = ""
                for href, label in buttons:
                    lbl = re.sub(r'<[^>]+>', '', label).strip()
                    ll = lbl.lower()
                    hl = href.lower()
                    if any(bad in ll for bad in ['telegram', 'join', 'watch online', 'stream', 'how to']):
                        continue
                    if '720p' in ll and any(k in ll for k in ['x265', '10bit', 'zip', 'batch']):
                        target_gateway = href
                        target_label = lbl
                        break
                    elif '720p' in ll and not target_gateway:
                        target_gateway = href
                        target_label = lbl

                if not target_gateway:
                    print(f"  [Skip] No 720p zip link found for '{title}'")
                    errors.append({"title": title, "season": season, "url": post_url, "reason": "No 720p zip link found on post"})
                    save_json(ERROR_LOG_PATH, errors)
                    continue

                print(f"  Found 720p button: [{target_label}] -> {target_gateway}")
                
                # Resolve via solver
                resolved = resolve_gateway(target_gateway)
                if not resolved or not resolved.get("url"):
                    print(f"  [Error] Failed to resolve gateway for '{title}'")
                    errors.append({"title": title, "season": season, "url": post_url, "gateway": target_gateway, "reason": "Turnstile solver failed to return mirrors"})
                    save_json(ERROR_LOG_PATH, errors)
                    continue

                best_url = resolved["url"]
                mirror_type = resolved["mirror_type"]
                file_name = resolved.get("title") or f"{title} Season {season}"

                # Send to Nothing Do
                msg_text = (
                    f"🎌 <b>{title}</b> (Season {season})\n"
                    f"📦 <b>Quality:</b> 720p 10-bit HEVC [Batch Zip]\n"
                    f"🎧 <b>Audio:</b> Tamil Multi-Audio\n"
                    f"📁 <b>File:</b> <code>{file_name}</code>\n"
                    f"🔗 <b>Link ({mirror_type}):</b> {best_url}"
                )

                try:
                    await app.send_message(NOTHING_DO_CHAT_ID, msg_text)
                    print(f"  >>> SENT to 'Nothing Do' via {mirror_type}: {best_url}")
                    sent.add(dedupe_key)
                    save_json(SENT_CACHE_PATH, list(sent))
                    processed += 1
                except Exception as e:
                    print(f"  [Telegram Error]: {e}")
                    errors.append({"title": title, "season": season, "url": post_url, "reason": f"Telegram send error: {e}"})
                    save_json(ERROR_LOG_PATH, errors)

                await asyncio.sleep(2)

                if limit and processed >= limit:
                    print(f"Reached batch limit of {limit}. Stopping.")
                    break
            if limit and processed >= limit:
                break

    # Write Markdown error summary
    with open(ERROR_MD_PATH, "w", encoding="utf-8") as f:
        f.write("# HindiAnimeZone Manual Upload Backlog\n\n")
        f.write(f"Total series requiring manual upload: **{len(errors)}**\n\n")
        f.write("| Anime Title | Season | Reason | URL |\n")
        f.write("| :--- | :--- | :--- | :--- |\n")
        for err in errors:
            f.write(f"| {err.get('title')} | S{err.get('season', 1)} | {err.get('reason')} | [Post Link]({err.get('url')}) |\n")

    print(f"\nFinished run! Sent: {processed} series. Errors logged to {ERROR_MD_PATH}")

if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    asyncio.run(dispatch_all(limit=limit_arg))
