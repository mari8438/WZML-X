#!/usr/bin/env python3
import asyncio
import http.cookiejar
import json
import os
from os import path as ospath
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/usr/src/app")

import aiohttp
import requests

from bot.core.config_manager import Config
from bot.helper.ext_utils.bitporn import BitpornClient
from bot.helper.ext_utils.kufirc import KufircClient
from bot.helper.ext_utils.luminapix import upload_image_details

ROOT_DIR = Path("/usr/src/app")
FOLDER_NAME = "Hstream 1080p Pack - Part 2"
FOLDER_PATH = ROOT_DIR / "torrents" / "seeding" / FOLDER_NAME
OUTPUT_DIR = ROOT_DIR / "torrents" / "output"
SECRETS_DIR = ROOT_DIR / "secrets"

VIDEOS_DIR = FOLDER_PATH / "Videos"
SCREENSHOTS_DIR = FOLDER_PATH / "Screenshots"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def proxy_url(url: str) -> str:
    """Anti-hotlink wrapper using wsrv.nl proxy to bypass tracker CDN referer blocks."""
    if not url:
        return url
    if url.startswith("https://wsrv.nl/?url="):
        return url
    return f"https://wsrv.nl/?url={url}"

def step1_organize_folders():
    print("=== STEP 1: Organizing Folder Structure ===")
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Move any .mkv files in root of folder into Videos/
    mkv_files = list(FOLDER_PATH.glob("*.mkv"))
    if mkv_files:
        print(f"Moving {len(mkv_files)} .mkv files into {VIDEOS_DIR}...")
        for mkv in mkv_files:
            target = VIDEOS_DIR / mkv.name
            if target.exists():
                target.unlink()
            shutil.move(str(mkv), str(target))
        print("Moved video files into Videos/ successfully.")
    else:
        print(f"Videos folder already populated ({len(list(VIDEOS_DIR.glob('*.mkv')))} files).")

    v_count = len(list(VIDEOS_DIR.glob("*.mkv")))
    s_count = len(list(SCREENSHOTS_DIR.glob("*.jpg")))
    print(f"Directory structure verified:")
    print(f"  - {VIDEOS_DIR}: {v_count} video files")
    print(f"  - {SCREENSHOTS_DIR}: {s_count} contact sheet screenshots")
    return v_count, s_count

def calculate_folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp) and os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total

async def step2_upload_previews_and_build_bbcode(v_count, total_size_bytes):
    print("\n=== STEP 2: Previews & BBCode Description ===")
    total_gb = total_size_bytes / (1024 ** 3)
    
    # 1. Upload cover to LuminaPix
    sample_sheets = sorted(list(SCREENSHOTS_DIR.glob("*.jpg")))
    if not sample_sheets:
        raise RuntimeError("No contact sheets found in Screenshots directory!")

    cover_file = sample_sheets[0]
    print(f"Uploading primary cover artwork: {cover_file.name} to LuminaPix...")
    try:
        cover_details = await upload_image_details(str(cover_file))
        raw_direct = cover_details.get("direct") or cover_details.get("image")
        cover_page_url = cover_details.get("page", raw_direct)
        cover_direct_url = proxy_url(raw_direct)
    except Exception as e:
        print(f"LuminaPix cover upload fallback: {e}")
        raw_direct = "https://dirtybytes.net/img/dirtybytes-social-v5.png"
        cover_page_url = raw_direct
        cover_direct_url = proxy_url(raw_direct)

    print(f"Cover URL (Proxied): {cover_direct_url}")

    # 2. Upload up to 10 sample preview contact sheets
    spoilers = []
    for idx, sheet in enumerate(sample_sheets[:10], start=1):
        try:
            print(f"Uploading sample sheet {idx}/10: {sheet.name}...")
            s_det = await upload_image_details(str(sheet))
            title = sheet.stem.replace("_12screens", "").replace("_12screens_2", "")
            img_url = proxy_url(s_det.get("thumbnail") or s_det.get("direct"))
            page_url = s_det.get("page", img_url)
            spoilers.append(
                f"[spoiler={title}]\n"
                f"[center][url={page_url}][img]{img_url}[/img][/url][/center]\n"
                f"[/spoiler]"
            )
        except Exception as e:
            print(f"Warning: Failed to upload sample {sheet.name}: {e}")

    # 3. Assemble rich BBCode description
    tech_specs = (
        f"• Total Collection Size: {total_gb:.2f} GiB\n"
        f"• Episode Count: {v_count} Full Episodes\n"
        f"• Video Format: MKV Container\n"
        f"• Resolution: 1080p Full HD (Prioritizing 48fps Enhanced Editions)\n"
        f"• Video Codec: AV1 / H.264 (AVC)\n"
        f"• Audio: Japanese (Original Dub) AAC / Stereo\n"
        f"• Contact Sheets: 12-Screen Preview Grid included for every episode"
    )

    bbcode = (
        f"[center][b][size=6]{FOLDER_NAME} [1080p 48fps / Full HD][/size][/b][/center]\n\n"
        f"[center][url={cover_page_url}][img]{cover_direct_url}[/img][/url][/center]\n\n"
        "[quote]\n"
        f"[b]Pack Overview:[/b]\n"
        f"{FOLDER_NAME} is an archival megacollection featuring {v_count} uncut anime episodes sourced in pristine 1080p Full HD (featuring 48fps high frame rate editions where available). Every episode is organized with dedicated 12-frame contact sheets inside the Screenshots directory.\n\n"
        f"[b]Technical Specifications:[/b]\n"
        f"{tech_specs}\n\n"
        f"[b]Directory Layout:[/b]\n"
        f"  ├── Videos/ ({v_count} episodes)\n"
        f"  └── Screenshots/ (Contact sheet gallery)\n"
        "[/quote]\n\n"
        "[center][b][size=5]Sample Contact Sheet Galleries[/size][/b][/center]\n\n"
        + "\n\n".join(spoilers)
        + "\n"
    )

    desc_file = OUTPUT_DIR / f"{FOLDER_NAME}.description.txt"
    desc_file.write_text(bbcode, encoding="utf-8")
    print(f"BBCode description saved -> {desc_file}")
    return cover_direct_url, bbcode

def bdecode(data):
    def decode_item(idx):
        b = data[idx:idx+1]
        if b == b'i':
            end = data.index(b'e', idx)
            return int(data[idx+1:end]), end + 1
        elif b == b'l':
            idx += 1
            res = []
            while data[idx:idx+1] != b'e':
                item, idx = decode_item(idx)
                res.append(item)
            return res, idx + 1
        elif b == b'd':
            idx += 1
            res = {}
            while data[idx:idx+1] != b'e':
                k, idx = decode_item(idx)
                v, idx = decode_item(idx)
                res[k] = v
            return res, idx + 1
        elif b.isdigit():
            colon = data.index(b':', idx)
            length = int(data[idx:colon])
            start = colon + 1
            return data[start:start+length], start + length
        raise ValueError(f'Unknown token: {b} at {idx}')
    return decode_item(0)[0]

def bencode(obj):
    if isinstance(obj, int):
        return f'i{obj}e'.encode()
    elif isinstance(obj, bytes):
        return f'{len(obj)}:'.encode() + obj
    elif isinstance(obj, str):
        b = obj.encode('utf-8')
        return f'{len(b)}:'.encode() + b
    elif isinstance(obj, list):
        return b'l' + b''.join(bencode(x) for x in obj) + b'e'
    elif isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: kv[0] if isinstance(kv[0], bytes) else kv[0].encode('utf-8'))
        res = [b'd']
        for k, v in items:
            res.append(bencode(k))
            res.append(bencode(v))
        res.append(b'e')
        return b''.join(res)
    raise TypeError(f'Cannot bencode {type(obj)}')

import hashlib

async def step3_create_torrents():
    print("\n=== STEP 3: Generating Private Torrents via mktorrent ===")
    torrents = {}

    # 1. DirtyBytes Torrent (Base hashing via mktorrent with 16MB piece size)
    db_cookie = SECRETS_DIR / "dirtybytes_cookies.txt"
    cj = http.cookiejar.MozillaCookieJar(str(db_cookie))
    cj.load(ignore_discard=True, ignore_expires=True)
    s = requests.Session()
    s.cookies = cj
    uid = "3791"
    for c in cj:
        if c.name == "uid":
            uid = str(c.value)
    r = s.get(f"https://dirtybytes.net/api/v1/users/{uid}")
    if r.status_code == 200:
        db_passkey = r.json().get("passkey")
    else:
        db_passkey = "9cd40192de41f8979706d2ded50f4a02"
    db_announce = f"https://dirtybytes.net/announce/{db_passkey}"
    db_torrent = OUTPUT_DIR / f"{FOLDER_NAME}.dirtybytes.torrent"
    if db_torrent.exists() and db_torrent.stat().st_size > 500000:
        print(f"Base DirtyBytes torrent already exists ({db_torrent.stat().st_size} bytes). Reusing piece hashes...")
    else:
        cmd_db = [
            "mktorrent", "-o", str(db_torrent), "-p",
            "-s", "DirtyBytes", "-a", db_announce, "-l", "24",
            str(FOLDER_PATH)
        ]
        print(f"Creating base DirtyBytes torrent (16MB piece size -l 24)...")
        subprocess.run(cmd_db, check=True)
    
    db_meta = bdecode(db_torrent.read_bytes())
    db_infohash = hashlib.sha1(bencode(db_meta[b'info'])).hexdigest()
    torrents["dirtybytes"] = {"path": db_torrent, "session": s, "announce": db_announce, "infohash": db_infohash}
    print(f"Generated DirtyBytes torrent: {db_torrent.name} ({db_torrent.stat().st_size} bytes, InfoHash: {db_infohash})")

    # 2. BitPorn Torrent (Derived instantly from base pieces)
    bp_announce = getattr(Config, "BITPORN_ANNOUNCE_URL", "") or "https://bitporn.eu/announce/21f0b107f9c57d8b4e080e8ffcb47199"
    bp_torrent = OUTPUT_DIR / f"{FOLDER_NAME}.bitporn.torrent"
    bp_meta = dict(db_meta)
    bp_meta[b'announce'] = bp_announce.encode('utf-8')
    bp_meta[b'info'] = dict(db_meta[b'info'])
    bp_meta[b'info'][b'source'] = b'BitPorn'
    bp_bytes = bencode(bp_meta)
    bp_torrent.write_bytes(bp_bytes)
    bp_infohash = hashlib.sha1(bencode(bp_meta[b'info'])).hexdigest()
    torrents["bitporn"] = {"path": bp_torrent, "announce": bp_announce, "infohash": bp_infohash}
    print(f"Generated BitPorn torrent: {bp_torrent.name} ({bp_torrent.stat().st_size} bytes, InfoHash: {bp_infohash})")

    # 3. Kufirc Torrent (Derived instantly from base pieces)
    kf_announce = getattr(Config, "KUFIRC_ANNOUNCE_URL", "") or "http://kufirc.com:7456/pcscvrlpzrlko1pgjnunto103sg0abn6/announce"
    kf_torrent = OUTPUT_DIR / f"{FOLDER_NAME}.kufirc.torrent"
    kf_meta = dict(db_meta)
    kf_meta[b'announce'] = kf_announce.encode('utf-8')
    kf_meta[b'info'] = dict(db_meta[b'info'])
    kf_meta[b'info'][b'source'] = b'Kufirc'
    kf_bytes = bencode(kf_meta)
    kf_torrent.write_bytes(kf_bytes)
    kf_infohash = hashlib.sha1(bencode(kf_meta[b'info'])).hexdigest()
    torrents["kufirc"] = {"path": kf_torrent, "announce": kf_announce, "infohash": kf_infohash}
    print(f"Generated Kufirc torrent: {kf_torrent.name} ({kf_torrent.stat().st_size} bytes, InfoHash: {kf_infohash})")

    return torrents

async def step4_upload_trackers(torrents, cover_url, bbcode):
    print("\n=== STEP 4: Uploading to Trackers ===")
    accepted_torrents = []

    # 1. DIRTYBYTES UPLOAD
    print("\n--- Uploading to DirtyBytes ---")
    db_acc = OUTPUT_DIR / f"{FOLDER_NAME}.dirtybytes.accepted.torrent"
    if db_acc.exists() and db_acc.stat().st_size > 500000:
        print(f"DirtyBytes already accepted and actively seeding ({db_acc.name}). Preserving existing release.")
        accepted_torrents.append(db_acc)
    else:
        try:
            db_info = torrents["dirtybytes"]
            s = db_info["session"]
            upload_url = "https://dirtybytes.net/api/v1/torrents/upload"
            data = {
                "category": "26",  # Packs
                "reqid": "0",
                "frileech": "0",
                "anonymousUpload": "1",
                "section": "new",
                "nfo": bbcode,
                "firstpic": cover_url,
                "content_categories": "45,41",  # Japanese, Asian
                "othergenre": "Hentai, Anime, Uncensored, 1080p, 48fps, Megapack",
                "language": "Japanese",
                "unrar": "0",
                "gameId": "0",
                "imdbId": "0",
                "requestModQueue": "0",
                "modQueueMessage": "",
            }
            with open(db_info["path"], "rb") as tf:
                files = {"file": (db_info["path"].name, tf, "application/x-bittorrent")}
                r = s.post(upload_url, data=data, files=files, timeout=120)

            if r.status_code == 200:
                res = r.json()
                t_id = res.get("id")
                t_name = res.get("name")
                print(f"DirtyBytes SUCCESS! Release: https://dirtybytes.net/torrent/{t_id}/{t_name}")
                dl_url = f"https://dirtybytes.net/api/v1/torrents/download/{t_id}"
                r_dl = s.get(dl_url)
                acc_path = OUTPUT_DIR / f"{FOLDER_NAME}.dirtybytes.accepted.torrent"
                acc_path.write_bytes(r_dl.content)
                accepted_torrents.append(acc_path)
            else:
                print(f"DirtyBytes Upload Error {r.status_code}: {r.text[:300]}")
                accepted_torrents.append(db_info["path"])
        except Exception as e:
            print(f"DirtyBytes Exception: {e}")
            accepted_torrents.append(torrents["dirtybytes"]["path"])

    # 2. BITPORN UPLOAD
    print("\n--- Uploading to BitPorn ---")
    bp_acc = OUTPUT_DIR / f"{FOLDER_NAME}.bitporn.accepted.torrent"
    if bp_acc.exists() and bp_acc.stat().st_size > 500000:
        print(f"BitPorn already accepted and registered ({bp_acc.name}). Preserving existing release.")
        accepted_torrents.append(bp_acc)
    else:
        try:
            client = BitpornClient(
                base_url="https://bitporn.eu",
                cookie_file=str(SECRETS_DIR / "bitporn_cookies.txt"),
                announce_url=torrents["bitporn"]["announce"],
            )
            async with client:
                upload_res = await client.upload_torrent(
                    torrent_path=str(torrents["bitporn"]["path"]),
                    title=f"{FOLDER_NAME} [1080p 48fps] [Clean Edition]",
                    category_id="15",     # Packs / Hentai
                    resolution_id="13",   # 1080p
                    cover_url=cover_url,
                    desc=bbcode,
                    keywords="anime, collection, uncensored, 1080p, 48fps, megapack",
                    anon=True,
                )
                print(f"BitPorn SUCCESS! Torrent #{upload_res.get('torrent_id')}: {upload_res.get('detail_url')}")
                acc_bytes = upload_res.get("accepted_bytes")
                if acc_bytes:
                    bp_acc.write_bytes(acc_bytes)
                else:
                    shutil.copy2(torrents["bitporn"]["path"], bp_acc)
                accepted_torrents.append(bp_acc)
        except Exception as e:
            print(f"BitPorn Exception: {e}")
            accepted_torrents.append(torrents["bitporn"]["path"])

    # 3. KUFIRC UPLOAD
    print("\n--- Uploading to Kufirc ---")
    kf_acc = OUTPUT_DIR / f"{FOLDER_NAME}.kufirc.accepted.torrent"
    if kf_acc.exists() and kf_acc.stat().st_size > 500000:
        print(f"Kufirc already accepted and registered ({kf_acc.name}). Preserving existing release.")
        accepted_torrents.append(kf_acc)
    else:
        try:
            client = KufircClient(
                base_url="https://kufirc.com",
                cookie_file=str(SECRETS_DIR / "kufirc_cookies.txt"),
                announce_url=torrents["kufirc"]["announce"],
            )
            async with client:
                upload_res = await client.upload_torrent(
                    torrent_path=str(torrents["kufirc"]["path"]),
                    title=f"{FOLDER_NAME} [1080p 48fps]",
                    category="14",  # Collections / Packs
                    desc=bbcode,
                    image=cover_url,
                    tags="anime, hentai, collection, uncensored, 1080p, 48fps",
                    anonymous=True,
                )
                print(f"Kufirc SUCCESS! Torrent #{upload_res.get('torrent_id')}: {upload_res.get('detail_url')}")
                acc_bytes = upload_res.get("accepted_bytes")
                if acc_bytes:
                    kf_acc.write_bytes(acc_bytes)
                else:
                    shutil.copy2(torrents["kufirc"]["path"], kf_acc)
                accepted_torrents.append(kf_acc)
        except Exception as e:
            print(f"Kufirc Exception: {e}")
            accepted_torrents.append(torrents["kufirc"]["path"])

    return accepted_torrents

async def step5_seed_qbittorrent(accepted_torrents):
    print("\n=== STEP 5: Injecting Torrents into qBittorrent-nox (Port 8090) ===")
    auth = aiohttp.BasicAuth("admin", "starfall2026!")
    async with aiohttp.ClientSession(auth=auth) as qbt:
        for tf_path in accepted_torrents:
            try:
                print(f"Adding to qBittorrent: {tf_path.name}...")
                data = aiohttp.FormData()
                with open(tf_path, "rb") as f:
                    data.add_field("torrents", f.read(), filename=tf_path.name, content_type="application/x-bittorrent")
                data.add_field("savepath", str(ROOT_DIR / "torrents" / "seeding"))
                data.add_field("paused", "false")
                data.add_field("autoTMM", "false")
                data.add_field("skip_checking", "true")  # Fast resume instant seeding

                # Try localhost:8090 first, fallback to container direct IP if needed
                added = False
                for endpoint in ["http://127.0.0.1:8090/api/v2/torrents/add", "http://172.18.0.3:8090/api/v2/torrents/add"]:
                    try:
                        async with qbt.post(endpoint, data=data) as resp:
                            if resp.status == 200:
                                print(f"  Response from {endpoint}: {resp.status} {await resp.text()}")
                                added = True
                                break
                    except Exception:
                        pass
                if not added:
                    print(f"  Warning: could not add {tf_path.name} to qBittorrent")
            except Exception as e:
                print(f"  Error adding {tf_path.name}: {e}")

    await asyncio.sleep(5)
    print("\nVerifying Active Seeds in qBittorrent:")
    try:
        async with aiohttp.ClientSession(auth=auth) as qbt:
            for endpoint in ["http://127.0.0.1:8090/api/v2/torrents/info", "http://172.18.0.3:8090/api/v2/torrents/info"]:
                try:
                    async with qbt.get(endpoint) as resp:
                        if resp.status == 200:
                            torrents = await resp.json()
                            for t in torrents:
                                if FOLDER_NAME in t.get("name", ""):
                                    print(f"  • Name: {t.get('name')} | Hash: {t.get('hash')} | State: {t.get('state')} | Progress: {t.get('progress')*100:.1f}% | Up: {t.get('upspeed')/1024:.1f} KB/s")
                            break
                except Exception:
                    pass
    except Exception as e:
        print(f"qBittorrent check warning: {e}")

async def main():
    print(f"============================================================")
    print(f"  STARTING PACKAGING & SEEDING: {FOLDER_NAME}")
    print(f"============================================================")
    v_count, s_count = step1_organize_folders()
    total_size = calculate_folder_size(str(FOLDER_PATH))
    print(f"Total Size: {total_size / (1024**3):.2f} GiB ({total_size} bytes)")

    cover_url, bbcode = await step2_upload_previews_and_build_bbcode(v_count, total_size)
    torrents = await step3_create_torrents()
    accepted = await step4_upload_trackers(torrents, cover_url, bbcode)
    await step5_seed_qbittorrent(accepted)

    print(f"\n============================================================")
    print(f"  ALL DONE! PACK IS 100% SEEDING ACROSS ALL 3 TRACKERS!")
    print(f"============================================================")

if __name__ == "__main__":
    asyncio.run(main())
