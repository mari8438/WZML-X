import asyncio
import os
from os import path as ospath
import re
import sys
import time
from html import escape
from shutil import copy2

import aiohttp

from bot import LOGGER
from bot.core.config_manager import Config
from bot.core.tg_client import TgClient
from bot.helper.ext_utils.bot_utils import cmd_exec
from bot.helper.ext_utils.bitporn import BitpornClient, BitpornError
from bot.helper.ext_utils.luminapix import upload_image_details, LuminapixError
from bot.helper.ext_utils.media_utils import (
    format_clean_poster_title,
    get_readable_file_size,
)
from bot.helper.telegram_helper.button_build import ButtonMaker


def calculate_folder_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = ospath.join(root, f)
            if not ospath.islink(fp) and ospath.isfile(fp):
                total += ospath.getsize(fp)
    return total


async def main():
    folder_name = sys.argv[1] if len(sys.argv) > 1 else "Hanime Pack - 1"
    folder_path = f"/usr/src/app/torrents/seeding/{folder_name}"
    output_dir = "/usr/src/app/torrents/output"
    os.makedirs(output_dir, exist_ok=True)

    print(f"=== Starting BitPorn Megapack Publish for '{folder_name}' ===")
    if not ospath.isdir(folder_path):
        print(f"ERROR: folder not found: {folder_path}")
        return

    # 1. Count video files and calculate total size synchronously
    video_exts = {".mkv", ".mp4", ".avi", ".ts", ".webm"}
    videos = [
        ospath.join(folder_path, f)
        for f in sorted(os.listdir(folder_path))
        if ospath.splitext(f)[1].lower() in video_exts
    ]
    total_size = calculate_folder_size(folder_path)
    print(f"Found {len(videos)} videos in {folder_name}, total size: {get_readable_file_size(total_size)} ({total_size} bytes)")

    # 2. Upload cover to LuminaPix
    preview_webp = ospath.join(output_dir, f"{folder_name}.preview.webp")
    if not ospath.isfile(preview_webp):
        print(f"WARNING: {preview_webp} not found, checking for cover jpg")
        covers = [ospath.join(folder_path, f) for f in os.listdir(folder_path) if f.endswith(".jpg")]
        cover_to_upload = covers[0] if covers else None
    else:
        cover_to_upload = preview_webp

    if not cover_to_upload or not ospath.isfile(cover_to_upload):
        raise FileNotFoundError(f"No cover file found to upload for {folder_name}")

    print(f"Uploading cover to LuminaPix: {cover_to_upload}...")
    cover_details = await upload_image_details(cover_to_upload)
    cover_direct_url = cover_details["direct"]
    cover_page_url = cover_details["page"]
    print(f"Cover uploaded successfully: direct={cover_direct_url}, page={cover_page_url}")

    # 3. Find and upload preview contact sheets to LuminaPix
    screenshots_dir = ospath.join(folder_path, "Screenshots")
    if not ospath.isdir(screenshots_dir):
        screenshots_dir = f"/usr/src/app/torrents/artwork/{folder_name}/Screenshots"
    sheet_files = []
    if ospath.isdir(screenshots_dir):
        sheet_files = [
            ospath.join(screenshots_dir, f)
            for f in sorted(os.listdir(screenshots_dir))
            if f.endswith("_12screens.jpg") or f.endswith("_12screens_2.jpg")
        ]
    print(f"Found {len(sheet_files)} generated contact sheets in {screenshots_dir}")

    preview_sheets = sheet_files[:15]
    spoilers_list = []
    for idx, s_path in enumerate(preview_sheets, 1):
        try:
            print(f"Uploading preview sheet {idx}/{len(preview_sheets)}: {ospath.basename(s_path)}...")
            s_details = await upload_image_details(s_path)
            clean_title = ospath.basename(s_path).replace("_12screens_2.jpg", "").replace("_12screens.jpg", "")
            spoilers_list.append(
                f"[spoiler={clean_title}]\n"
                f"[center][url={s_details['page']}][img]{s_details['thumbnail']}[/img][/url][/center]\n"
                f"[/spoiler]"
            )
        except Exception as up_err:
            print(f"Warning: Failed to upload contact sheet {s_path}: {up_err}")

    # 4. Read metadata descriptions & genres from .hstream.json files
    genres_set = set()
    descriptions = []
    episodes_meta = []
    artwork_dir = f"/usr/src/app/torrents/artwork/{folder_name}"
    import json
    for v in videos:
        meta_file = f"{v}.hstream.json"
        if not ospath.isfile(meta_file):
            meta_file = ospath.join(artwork_dir, f"{ospath.basename(v)}.hstream.json")
        if ospath.isfile(meta_file):
            try:
                with open(meta_file, "r", encoding="utf-8") as mf:
                    m = json.load(mf)
                    for g in m.get("genres", []):
                        if "rape" not in g.lower():
                            genres_set.add(g.title())
                    if m.get("description"):
                        desc_snip = f"{m.get('title')}: {m.get('description')[:300]}..."
                        if desc_snip not in descriptions:
                            descriptions.append(desc_snip)
                    episodes_meta.append({
                        "slug": m.get("slug") or ospath.basename(v),
                        "title": m.get("title") or ospath.basename(v),
                        "size_bytes": ospath.getsize(v),
                    })
            except Exception:
                pass

    clean_genres = sorted(list(genres_set))
    pack_summary = (
        f"{folder_name} is a comprehensive animation release pack containing {len(videos)} scenes in 1080p high definition.\n\n"
        + ("\n\n".join(descriptions[:4]) if descriptions else "")
    )
    tech_specs = f"Total Size: {get_readable_file_size(total_size)}\nScene Count: {len(videos)} videos\nResolution: 1080p HD\nVideo Codec: H.264 / AVC\nAudio: AAC / 2.0 Channels"

    # 5. Build BBCode description for BitPorn (UNIT3D)
    bbcode = (
        f"[center][b][size=6]{folder_name} [1080p][/size][/b][/center]\n\n"
        f"[center][url={cover_page_url}][img]{cover_direct_url}[/img][/url][/center]\n\n"
        "[quote]\n"
        f"[b]Description:[/b]\n{pack_summary}\n\n"
        f"[b]Release Info:[/b]\n"
        f"• Files: {len(videos)} videos\n"
        f"• Size: {get_readable_file_size(total_size)}\n"
        f"• Tags: {', '.join(clean_genres[:10]) if clean_genres else 'Anime, Animation, 1080p'}\n\n"
        f"[b]Technical Specifications:[/b]\n"
        f"{tech_specs}\n"
        "[/quote]\n\n"
        "[center][b][size=5]Scene Previews & Contact Sheets[/size][/b][/center]\n\n"
        + "\n\n".join(spoilers_list)
        + "\n"
    )

    desc_file_path = ospath.join(output_dir, f"{folder_name}.bitporn.description.txt")
    with open(desc_file_path, "w", encoding="utf-8") as df:
        df.write(bbcode)
    print(f"Generated BitPorn BBCode description ({len(bbcode)} chars) -> {desc_file_path}")

    # 6. Create private .torrent file for BitPorn using mktorrent
    announce_url = getattr(Config, "BITPORN_ANNOUNCE_URL", "") or "https://bitporn.eu/announce/21f0b107f9c57d8b4e080e8ffcb47199"
    torrent_path = ospath.join(output_dir, f"{folder_name}.bitporn.torrent")
    if ospath.isfile(torrent_path):
        os.remove(torrent_path)
    print(f"Creating private torrent pointing to announce: {announce_url}...")
    cmd = [
        "mktorrent",
        "-o", torrent_path,
        "-p",
        "-s", "BitPorn",
        "-a", announce_url,
        "-l", "23",
        folder_path,
    ]
    stdout, stderr, code = await cmd_exec(cmd)
    if code != 0 or not ospath.isfile(torrent_path):
        raise RuntimeError(f"mktorrent failed: {stderr or stdout}")
    print(f"Torrent file generated: {torrent_path} ({ospath.getsize(torrent_path)} bytes)")

    # 7. Upload torrent to BitPorn
    client = BitpornClient(
        base_url=getattr(Config, "BITPORN_URL", "https://bitporn.eu"),
        cookie_file=getattr(Config, "BITPORN_COOKIE_FILE", "/usr/src/app/secrets/bitporn_cookies.txt"),
        announce_url=announce_url,
    )
    tags_str = "anime, collection, uncensored, 1080p, 48fps"
    if clean_genres:
        top_genres = [g.lower().replace(" ", "") for g in clean_genres[:8]]
        tags_str = ", ".join(top_genres)

    print("Submitting to BitPorn tracker...")
    async with client:
        upload_res = await client.upload_torrent(
            torrent_path=torrent_path,
            title=f"{folder_name} [1080p] [Clean Edition]",
            category_id=getattr(Config, "BITPORN_CATEGORY", "15"),
            resolution_id=getattr(Config, "BITPORN_RESOLUTION", "13"),
            cover_url=cover_direct_url,
            desc=bbcode,
            keywords=tags_str,
            anon=bool(getattr(Config, "BITPORN_ANONYMOUS", False)),
        )

    print("Upload Response:", upload_res)
    upload_url = upload_res["detail_url"]
    torrent_id = upload_res["torrent_id"]
    accepted_bytes = upload_res.get("accepted_bytes")

    # 8. Save accepted torrent file
    accepted_torrent_path = ospath.join(output_dir, f"{folder_name}.bitporn.accepted.torrent")
    if accepted_bytes:
        with open(accepted_torrent_path, "wb") as af:
            af.write(accepted_bytes)
        print(f"Saved accepted BitPorn torrent ({len(accepted_bytes)} bytes) -> {accepted_torrent_path}")
    else:
        copy2(torrent_path, accepted_torrent_path)
        print(f"Copied local torrent to accepted path -> {accepted_torrent_path}")

    # 9. Add to qBittorrent via Web API and seed
    print("Adding to qBittorrent...")
    torrent_hash = "N/A"
    try:
        async with aiohttp.ClientSession() as qbt_session:
            existing_hashes = set()
            try:
                async with qbt_session.get("http://127.0.0.1:8090/api/v2/torrents/info") as resp:
                    if resp.status == 200:
                        existing_hashes = {t.get("hash") for t in await resp.json()}
            except Exception:
                pass

            data = aiohttp.FormData()
            with open(accepted_torrent_path, "rb") as tf:
                data.add_field("torrents", tf.read(), filename=ospath.basename(accepted_torrent_path), content_type="application/x-bittorrent")
            data.add_field("savepath", "/usr/src/app/torrents/seeding")
            data.add_field("paused", "false")
            data.add_field("autoTMM", "false")
            async with qbt_session.post("http://127.0.0.1:8090/api/v2/torrents/add", data=data) as resp:
                print(f"qBittorrent add response: {resp.status} {await resp.text()}")

            print("Torrent added to qBittorrent! Waiting for torrent to register...")
            await asyncio.sleep(2)
            for _ in range(15):
                async with qbt_session.get("http://127.0.0.1:8090/api/v2/torrents/info") as resp:
                    if resp.status == 200:
                        torrents_list = await resp.json()
                        for item in torrents_list:
                            h = item.get("hash", "")
                            if h and h not in existing_hashes:
                                torrent_hash = h
                                break
                            name = item.get("name", "")
                            tr = item.get("tracker", "")
                            if (name == folder_name or "Hanime" in name) and "bitporn" in tr.lower():
                                torrent_hash = h
                                break
                    if torrent_hash != "N/A":
                        break
                await asyncio.sleep(1)

            print(f"qBittorrent BitPorn Seeding Hash: {torrent_hash}")
            if torrent_hash != "N/A":
                async with qbt_session.post("http://127.0.0.1:8090/api/v2/torrents/recheck", data={"hashes": torrent_hash}) as resp:
                    print(f"Triggered recheck: {resp.status}")
                await asyncio.sleep(1)
                async with qbt_session.post("http://127.0.0.1:8090/api/v2/torrents/resume", data={"hashes": torrent_hash}) as resp:
                    print(f"Triggered resume: {resp.status}")
    except Exception as qb_err:
        print(f"Warning: qBittorrent integration error: {qb_err}")

    # 10. Record in MongoDB
    try:
        from bot.helper.ext_utils.db_handler import database
        if database._conn is None and getattr(Config, "DATABASE_URL", ""):
            await database.connect()
        pack_doc = {
            "_id": f"bitporn_{folder_name}",
            "pack_number": 1,
            "pack_name": folder_name,
            "tracker": "bitporn",
            "status": "COMPLETED",
            "total_size_bytes": total_size,
            "readable_size": get_readable_file_size(total_size),
            "episodes_count": len(videos),
            "episodes": episodes_meta or [{"slug": ospath.basename(v), "title": ospath.basename(v), "size_bytes": 0} for v in videos],
            "torrent_hash": torrent_hash,
            "upload_url": upload_url,
            "completed_at": int(time.time()),
        }
        await database.save_hstream_pack(pack_doc)
        if database.db is not None:
            await database.db.bitporn_hstream_packs.replace_one(
                {"_id": pack_doc["_id"]}, pack_doc, upsert=True
            )
        print("Saved pack record to MongoDB successfully!")
    except Exception as db_err:
        print(f"MongoDB save note: {db_err}")

    # 11. Send Telegram notification if configured
    seeding_chan = str(getattr(Config, "HAPPYFAPPY_SEEDING_CHANNEL", "") or getattr(Config, "KUFIRC_SEEDING_CHANNEL", "") or "").strip()
    if seeding_chan:
        try:
            if not getattr(TgClient, "bot", None) or not getattr(TgClient.bot, "is_connected", False):
                try:
                    await TgClient.start_client()
                except Exception as tgc_err:
                    print(f"TgClient start note: {tgc_err}")

            if getattr(TgClient, "bot", None) and getattr(TgClient.bot, "is_connected", False):
                chan_id = int(seeding_chan) if seeding_chan.lstrip("-").isdigit() else seeding_chan
                ep_lines = [f"{i}. {escape(ospath.splitext(ospath.basename(v))[0])}" for i, v in enumerate(videos[:35], start=1)]
                if len(videos) > 35:
                    ep_lines.append(f"... and {len(videos) - 35} more videos")
                ep_block = "\n".join(ep_lines)

                caption = (
                    f"📦 <b>#HanimePack #Pack1 #BitPorn</b>\n\n"
                    f"📁 <b>Pack:</b> <code>{escape(folder_name)}</code>\n"
                    f"📊 <b>Total Size:</b> <code>{get_readable_file_size(total_size)}</code>\n"
                    f"🎬 <b>Episodes:</b> <code>{len(videos)} videos</code>\n"
                    f"🌱 <b>qBittorrent Seeding:</b> <code>{torrent_hash}</code>\n\n"
                    f"<blockquote expandable><b>Episodes in this pack:</b>\n{ep_block}</blockquote>\n\n"
                    f"🌐 <a href=\"{escape(upload_url)}\"><b>Open on BitPorn Tracker</b></a>"
                )
                btn = ButtonMaker()
                btn.url_button("🌐 Open on BitPorn", upload_url)
                from bot.modules.create_torrent import _qbit_webui_url
                webui = _qbit_webui_url()
                if webui:
                    btn.url_button("🌱 Web UI", webui)

                if ospath.isfile(preview_webp):
                    post_msg = await TgClient.bot.send_photo(chan_id, preview_webp, caption=caption, reply_markup=btn.build_menu(2))
                else:
                    post_msg = await TgClient.bot.send_message(chan_id, caption, reply_markup=btn.build_menu(2))

                reply_to_id = post_msg.id if post_msg else None
                await TgClient.bot.send_document(chan_id, accepted_torrent_path, caption=f"Torrent: {escape(folder_name)}", reply_to_message_id=reply_to_id)
                await TgClient.bot.send_document(chan_id, desc_file_path, caption=f"BBCode: {escape(folder_name)}", reply_to_message_id=reply_to_id)
                print(f"Posted release to Telegram channel {chan_id} successfully!")
        except Exception as tg_err:
            print(f"Telegram notification note: {tg_err}")

    print("\n🎉 === BITPORN PACK 1 COMPLETED SUCCESSFULLY! ===")
    print(f"Release URL: {upload_url}")
    print(f"Seeding Hash: {torrent_hash}")


if __name__ == "__main__":
    asyncio.run(main())
