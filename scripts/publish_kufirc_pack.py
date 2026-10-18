import asyncio
import os
from os import path as ospath
import re
import sys
import time
from html import escape
from shutil import copy2

from torf import Torrent
import qbittorrentapi

from bot import LOGGER
from bot.core.config_manager import Config
from bot.core.torrent_manager import TorrentManager
from bot.core.tg_client import TgClient
from bot.helper.ext_utils.kufirc import KufircClient, KufircError
from bot.helper.ext_utils.luminapix import upload_image_details, LuminapixError
from bot.helper.ext_utils.files_utils import get_path_size
from bot.helper.ext_utils.media_utils import (
    format_clean_poster_title,
    get_readable_file_size,
)
from bot.helper.telegram_helper.button_build import ButtonMaker


async def main():
    folder_name = "Hanime Pack - 1"
    folder_path = f"/usr/src/app/torrents/seeding/{folder_name}"
    output_dir = "/usr/src/app/torrents/output"
    os.makedirs(output_dir, exist_ok=True)

    print(f"=== Starting Kufirc Megapack Publish for '{folder_name}' ===")
    if not ospath.isdir(folder_path):
        print(f"ERROR: folder not found: {folder_path}")
        return

    # 1. Count video files
    video_exts = {".mkv", ".mp4", ".avi", ".ts", ".webm"}
    videos = [
        ospath.join(folder_path, f)
        for f in sorted(os.listdir(folder_path))
        if ospath.splitext(f)[1].lower() in video_exts
    ]
    total_size = await get_path_size(folder_path)
    print(f"Found {len(videos)} videos in {folder_name}, total size: {get_readable_file_size(total_size)} ({total_size} bytes)")

    # 2. Upload cover to LuminaPix
    preview_webp = ospath.join(output_dir, f"{folder_name}.preview.webp")
    if not ospath.isfile(preview_webp):
        print(f"WARNING: {preview_webp} not found, checking for cover jpg")
        covers = [ospath.join(folder_path, f) for f in os.listdir(folder_path) if f.endswith(".jpg")]
        cover_to_upload = covers[0] if covers else None
    else:
        cover_to_upload = preview_webp

    print(f"Uploading cover to LuminaPix: {cover_to_upload}...")
    cover_details = await upload_image_details(cover_to_upload)
    cover_direct_url = cover_details["direct"]
    cover_page_url = cover_details["page"]
    print(f"Cover uploaded successfully: direct={cover_direct_url}, page={cover_page_url}")

    # 3. Find and upload preview contact sheets to LuminaPix
    screenshots_dir = ospath.join(folder_path, "Screenshots")
    sheet_files = []
    if ospath.isdir(screenshots_dir):
        sheet_files = [
            ospath.join(screenshots_dir, f)
            for f in sorted(os.listdir(screenshots_dir))
            if f.endswith("_12screens.jpg") or f.endswith("_12screens_2.jpg")
        ]
    print(f"Found {len(sheet_files)} generated contact sheets in Screenshots/")

    # Choose up to 15 representative contact sheets for BBCode presentation
    preview_sheets = sheet_files[:15]
    spoilers_list = []
    for idx, s_path in enumerate(preview_sheets, 1):
        try:
            print(f"Uploading preview sheet {idx}/{len(preview_sheets)}: {ospath.basename(s_path)}...")
            s_details = await upload_image_details(s_path)
            clean_title = ospath.basename(s_path).replace("_12screens_2.jpg", "").replace("_12screens.jpg", "")
            spoilers_list.append(
                f"[spoiler=Scene {idx:02d}: {clean_title}]\n"
                f"[center][url={s_details['page']}][img]{s_details['thumbnail']}[/img][/url][/center]\n"
                f"[/spoiler]"
            )
        except Exception as up_err:
            print(f"Warning: Failed to upload contact sheet {s_path}: {up_err}")

    # 4. Read metadata descriptions & genres from .hstream.json files
    genres_set = set()
    descriptions = []
    episodes_meta = []
    import json
    for v in videos:
        meta_file = f"{v}.hstream.json"
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
        f"{folder_name} is a comprehensive video pack containing {len(videos)} scenes.\n"
        + ("\n".join(descriptions[:5]) if descriptions else "")
    )
    tech_specs = f"Total Size: {get_readable_file_size(total_size)}\nScene Count: {len(videos)} videos\nFormats: MKV/MP4\nVideo Codec: H.264 / AVC\nAudio: AAC / 2.0 Channels"

    # 5. Build BBCode description for Kufirc
    bbcode = (
        "[font=Tahoma][color=#cbd5e1]\n"
        "[bg=#161b26,98%,center][bg=#00000000,96%,center]\n"
        f"[center][size=5][b][color=#93c5fd]{folder_name}[/color][/b][/size][/center]\n"
        "[/bg][/bg]\n\n"
        "[table=98%,nball,vat,center,#141721]\n"
        "[tr]\n"
        "[td=vat,32%,nopad]\n"
        f"[center][url={cover_page_url}][img]{cover_direct_url}[/img][/url][/center]\n"
        "[/td]\n"
        "[td=vat,68%]\n"
        "[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Pack Summary[/color][/b][/size][/bg][/bg]\n"
        f"{pack_summary}\n\n"
        "[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Release Details[/color][/b][/size][/bg][/bg]\n"
        f"[color=#f8fafc][b]Pack Title:[/b][/color] {folder_name}\n"
        f"[color=#f8fafc][b]Total Size:[/b][/color] {get_readable_file_size(total_size)}\n"
        f"[color=#f8fafc][b]Files Count:[/b][/color] {len(videos)} videos\n"
        f"[color=#f8fafc][b]Genres / Tags:[/b][/color] {', '.join(clean_genres) if clean_genres else 'Pack, Hentai, Anime'}\n\n"
        "[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Technical Specs[/color][/b][/size][/bg][/bg]\n"
        f"[code]{tech_specs}[/code]\n"
        "[/td]\n"
        "[/tr]\n"
        "[/table]\n\n"
        "[bg=#161b26,98%,center][bg=#00000000,96%,center]\n"
        "[center][size=4][b][color=#93c5fd]▌ Scene Previews & Contact Sheets[/color][/b][/size][/center]\n"
        "[/bg][/bg]\n\n"
        + "\n\n".join(spoilers_list)
        + "\n[/color][/font]\n"
    )

    desc_file_path = ospath.join(output_dir, f"{folder_name}.kufirc.description.txt")
    with open(desc_file_path, "w", encoding="utf-8") as df:
        df.write(bbcode)
    print(f"Generated Kufirc BBCode description ({len(bbcode)} chars) -> {desc_file_path}")

    # 6. Create private .torrent file for Kufirc
    announce_url = getattr(Config, "KUFIRC_ANNOUNCE_URL", "") or "http://kufirc.com:7456/pcscvrlpzrlko1pgjnunto103sg0abn6/announce"
    torrent_path = ospath.join(output_dir, f"{folder_name}.kufirc.torrent")
    print(f"Creating private torrent pointing to announce: {announce_url}...")
    t = Torrent(
        path=folder_path,
        trackers=[announce_url],
        private=True,
        source="Kufirc",
    )
    t.piece_size = 8 * 1024 * 1024  # 8MB piece size
    t.generate()
    t.write(torrent_path, overwrite=True)
    print(f"Torrent file generated: {torrent_path} ({ospath.getsize(torrent_path)} bytes)")

    # 7. Upload torrent to Kufirc
    client = KufircClient(
        base_url=getattr(Config, "KUFIRC_URL", "https://kufirc.com"),
        cookie_file=getattr(Config, "KUFIRC_COOKIE_FILE", "/usr/src/app/secrets/kufirc_cookies.txt"),
        announce_url=announce_url,
    )
    tags_str = "hentai, anime, pack, uncensored, 1080p, 48fps"
    if clean_genres:
        top_genres = [g.lower().replace(" ", ".") for g in clean_genres[:8]]
        tags_str = ", ".join(top_genres)

    print("Submitting to Kufirc tracker...")
    async with client:
        upload_res = await client.upload_torrent(
            torrent_path=torrent_path,
            title=folder_name,
            category=getattr(Config, "KUFIRC_CATEGORY", "13"),
            tags=tags_str,
            image=cover_direct_url,
            desc=bbcode,
            anonymous=bool(getattr(Config, "KUFIRC_ANONYMOUS", False)),
        )

    print("Upload Response:", upload_res)
    upload_url = upload_res["detail_url"]
    torrent_id = upload_res["torrent_id"]
    accepted_bytes = upload_res.get("accepted_bytes")

    # 8. Save accepted torrent file
    accepted_torrent_path = ospath.join(output_dir, f"{folder_name}.kufirc.accepted.torrent")
    if accepted_bytes:
        with open(accepted_torrent_path, "wb") as af:
            af.write(accepted_bytes)
        print(f"Saved accepted Kufirc torrent ({len(accepted_bytes)} bytes) -> {accepted_torrent_path}")
    else:
        copy2(torrent_path, accepted_torrent_path)
        print(f"Copied local torrent to accepted path -> {accepted_torrent_path}")

    # 9. Add to qBittorrent and seed
    print("Connecting to qBittorrent...")
    qbit = qbittorrentapi.Client(
        host="localhost",
        port=8090,
        username="admin",
        password=getattr(Config, "BASE_URL", ""),
    )
    # Derive password if necessary
    try:
        from bot.helper.ext_utils.bot_utils import derive_service_password
        qbit_pass = derive_service_password()
        qbit = qbittorrentapi.Client(
            host="localhost",
            port=8090,
            username="admin",
            password=qbit_pass,
        )
        qbit.auth_log_in()
        print("Logged into qBittorrent successfully")
    except Exception as qbit_auth_err:
        print(f"qBittorrent login note: {qbit_auth_err}")
        try:
            qbit = qbittorrentapi.Client(host="localhost", port=8090)
            qbit.auth_log_in()
        except Exception as e2:
            print(f"qBittorrent connection fallback: {e2}")

    with open(accepted_torrent_path, "rb") as tf:
        qbit.torrents_add(
            torrent_files=tf.read(),
            save_path="/usr/src/app/torrents/seeding",
            is_paused=False,
            auto_tmm=False,
        )
    print("Torrent added to qBittorrent for seeding!")

    # Wait a moment and fetch torrent hash
    time.sleep(3)
    torrents_list = qbit.torrents_info()
    pack_torrent = None
    for item in torrents_list:
        if item.name == folder_name:
            pack_torrent = item
            break

    torrent_hash = pack_torrent.hash if pack_torrent else "N/A"
    print(f"qBittorrent Seeding Hash: {torrent_hash}")

    # 10. Record in MongoDB
    try:
        from bot.helper.ext_utils.db_handler import database
        pack_doc = {
            "_id": folder_name,
            "pack_number": 1,
            "pack_name": folder_name,
            "tracker": "kufirc",
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
        print("Saved pack record to MongoDB successfully!")
    except Exception as db_err:
        print(f"MongoDB save note: {db_err}")

    # 11. Send Telegram notification to channel if configured
    seeding_chan = str(getattr(Config, "HAPPYFAPPY_SEEDING_CHANNEL", "") or getattr(Config, "KUFIRC_SEEDING_CHANNEL", "") or "").strip()
    if seeding_chan:
        try:
            chan_id = int(seeding_chan) if seeding_chan.lstrip("-").isdigit() else seeding_chan
            ep_lines = [f"{i}. {escape(ospath.splitext(ospath.basename(v))[0])}" for i, v in enumerate(videos[:35], start=1)]
            if len(videos) > 35:
                ep_lines.append(f"... and {len(videos) - 35} more videos")
            ep_block = "\n".join(ep_lines)

            caption = (
                f"📦 <b>#HanimePack #Pack1 #Kufirc</b>\n\n"
                f"📁 <b>Pack:</b> <code>{escape(folder_name)}</code>\n"
                f"📊 <b>Total Size:</b> <code>{get_readable_file_size(total_size)}</code>\n"
                f"🎬 <b>Episodes:</b> <code>{len(videos)} videos</code>\n"
                f"🌱 <b>qBittorrent Seeding:</b> <code>{torrent_hash}</code>\n\n"
                f"<blockquote expandable><b>Episodes in this pack:</b>\n{ep_block}</blockquote>\n\n"
                f"🌐 <a href=\"{escape(upload_url)}\"><b>Open on Kufirc Tracker</b></a>"
            )
            btn = ButtonMaker()
            btn.url_button("🌐 Open on Kufirc", upload_url)
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

    print("\n🎉 === KUFIRC PACK 1 COMPLETED SUCCESSFULLY! ===")
    print(f"Release URL: {upload_url}")
    print(f"Seeding Hash: {torrent_hash}")


if __name__ == "__main__":
    asyncio.run(main())
