import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Optional, Tuple, List, Dict, Any

# Ensure event loop exists before importing Pyrogram on Python 3.14
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        AUDIO_VAULT_DIR,
        DEFAULT_TAMIL_CHANNEL_ID,
        RAM_MUX_DIR,
        TELEGRAM_API,
        TELEGRAM_HASH,
        USER_SESSION_STRING,
        DB_PATH,
        DATA_DIR,
    )
    from bot.modules.starfall_pipeline.db import (
        init_db,
        register_audio_entry,
        get_or_create_season,
        get_all_audio_entries,
    )
else:
    from .config import (
        AUDIO_VAULT_DIR,
        DEFAULT_TAMIL_CHANNEL_ID,
        RAM_MUX_DIR,
        TELEGRAM_API,
        TELEGRAM_HASH,
        USER_SESSION_STRING,
        DB_PATH,
        DATA_DIR,
    )
    from .db import (
        init_db,
        register_audio_entry,
        get_or_create_season,
        get_all_audio_entries,
    )

from pyrogram import Client
from pyrogram.types import Message

# Suppress Pyrogram verbose session warnings
for _logger_name in ("pyrogram", "pyrogram.session", "pyrogram.connection", "pyrogram.dispatcher", "pyrogram.client"):
    logging.getLogger(_logger_name).setLevel(logging.ERROR)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] [%(levelname)s] - %(message)s")
LOGGER = logging.getLogger("StarfallHarvester")

def is_single_episode(text: str) -> bool:
    """Check if message is a single episode rather than a multi-episode batch."""
    if re.search(r"S\d+E\d+[_-]\d+|Part[_\s-]*\d+|Batch|E\d+-\d+", text, re.IGNORECASE):
        return False
    if re.search(r"[Ss]\d{1,2}[Ee]\d{1,3}|(?:EP|Episode)[.\s_-]*\d{1,4}|[-_\s]\d{1,3}(?:v\d)?(?:\.|\s|$)", text, re.IGNORECASE):
        return True
    return False

def parse_episode_info(text: str) -> Tuple[str, int, int]:
    """
    Extract Anime Title, Season Number, and Episode Number from filename or caption.
    Handles:
      - "🄰🅂_S1E01_A_Couple_of_Cuckoos_1080p_10bit_BluRay_×265_Tamil_AAC_2.mkv" -> ("A Couple of Cuckoos", 1, 1)
      - "🄰🅂-[S1E01] A Couple of Cuckoos 1080p..." -> ("A Couple of Cuckoos", 1, 1)
      - "[Tamil] Solo Leveling 2024 1080p S01E05" -> ("Solo Leveling", 1, 5)
      - "Demon Slayer - 04 [Tamil].mkv" -> ("Demon Slayer", 1, 4)
      - "Bleach S02E15 Dual-Audio" -> ("Bleach", 2, 15)
    """
    raw = re.sub(r"\.(mkv|mp4|webm|avi)$", "", text.strip(), flags=re.IGNORECASE)
    qual_regex = r"(?:1080p|720p|2160p|480p|10bit|8bit|BluRay|WEB-?DL|WEBDL|HDTV|[x×]26[45]|HEVC|Tamil|AAC|Dual-Audio|ESub)"

    # 1. Standard SxxExx
    s_e = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", raw)
    if s_e:
        season = int(s_e.group(1))
        ep = int(s_e.group(2))
        before = raw[:s_e.start()].strip()
        clean_before = re.sub(r"\[.*?\]|\(.*?\)|🄰🅂|[@#]\w+", "", before).strip(" -_[]()")
        after = raw[s_e.end():].strip()
        clean_after = re.sub(r"\[.*?\]|\(.*?\)|🄰🅂|[@#]\w+", "", after).strip(" -_[]()")

        if len(clean_before) > 3 and not re.match(r"^" + qual_regex + r"$", clean_before, re.I):
            title = re.split(qual_regex, clean_before, flags=re.IGNORECASE)[0]
        else:
            title = re.split(qual_regex, clean_after, flags=re.IGNORECASE)[0]

        title = re.sub(r"\b(19|20)\d{2}\b", "", title)
        title = title.replace("_", " ").strip(" -_[]()")
        return title or "Unknown Anime", season, ep

    # 2. Episode / EP xx
    ep_match = re.search(r"(?:EP|Episode)[.\s_-]*(\d{1,4})", raw, re.IGNORECASE)
    if ep_match:
        ep = int(ep_match.group(1))
        clean_raw = re.sub(r"\[.*?\]|\(.*?\)|🄰🅂|[@#]\w+", "", raw)
        title = re.split(qual_regex, clean_raw[:ep_match.start()], flags=re.IGNORECASE)[0]
        title = re.sub(r"\b(19|20)\d{2}\b", "", title)
        title = title.replace("_", " ").strip(" -_[]()")
        return title or "Unknown Anime", 1, ep

    # 3. Simple numbering "Title - 05"
    num_match = re.search(r"[-_\s](\d{1,3})(?:v\d)?(?:\.|\s|$)", raw)
    if num_match:
        ep = int(num_match.group(1))
        clean_raw = re.sub(r"\[.*?\]|\(.*?\)|🄰🅂|[@#]\w+", "", raw[:num_match.start()])
        title = re.split(qual_regex, clean_raw, flags=re.IGNORECASE)[0]
        title = re.sub(r"\b(19|20)\d{2}\b", "", title)
        title = title.replace("_", " ").strip(" -_[]()")
        return title or "Unknown Anime", 1, ep

    return "Unknown Anime", 1, 1

def compute_sha256(filepath: str) -> str:
    """Compute SHA256 of file in 64 KB chunks."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def get_audio_duration_ms(filepath: str) -> int:
    """Extract audio duration in milliseconds using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath
        ]
        out = subprocess.check_output(cmd, text=True).strip()
        return int(float(out) * 1000)
    except Exception as e:
        LOGGER.error(f"Error probing audio duration for {filepath}: {e}")
        return 0

async def demux_audio_from_stream(source_video_path: str, dest_audio_path: str) -> bool:
    """
    Losslessly extract audio stream using ffmpeg without re-encoding (-c:a copy).
    """
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", source_video_path,
        "-vn", "-sn", "-dn",
        "-c:a", "copy",
        dest_audio_path
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        LOGGER.error(f"FFmpeg demux failed: {stderr.decode()}")
        return False
    return True

async def harvest_single_message(
    client: Client,
    message: Message,
    download_dir: str = RAM_MUX_DIR,
) -> Optional[int]:
    """
    Download a video message to RAM disk, extract AAC audio, save to library,
    auto-delete raw video immediately, and log records to SQLite.
    """
    media = message.video or message.document
    if not media:
        return None

    filename = getattr(media, "file_name", "") or f"telegram_msg_{message.id}.mkv"
    caption = message.caption or ""
    parse_text = caption or filename
    title, season_num, ep_num = parse_episode_info(parse_text)

    clean_show = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
    output_audio_name = f"{clean_show}_S{season_num:02d}E{ep_num:02d}.m4a"
    final_audio_path = os.path.join(AUDIO_VAULT_DIR, output_audio_name)

    # Fast-path: Reuse existing extracted audio in vault to avoid redundant redownloads
    if os.path.exists(final_audio_path) and os.path.getsize(final_audio_path) > 1000:
        LOGGER.info(f"⏩ Audio already present in vault: {output_audio_name}")
        season_id = await get_or_create_season(
            title_romaji=title,
            season_number=season_num,
            total_episodes=12,
            season_state="HARVESTING",
        )
        file_unique_id = getattr(media, "file_unique_id", f"msg_{message.id}")
        audio_id = await register_audio_entry(
            season_id=season_id,
            episode_number=ep_num,
            source_msg_id=message.id,
            source_file_unique=file_unique_id,
            local_m4a_path=final_audio_path,
            duration_ms=get_audio_duration_ms(final_audio_path),
            audio_sha256=compute_sha256(final_audio_path),
            file_size_bytes=os.path.getsize(final_audio_path),
        )
        return audio_id

    LOGGER.info(
        f"⏳ Harvesting Msg #{message.id}: {title} S{season_num:02d}E{ep_num:02d} "
        f"({media.file_size / (1024**2):.1f} MB)"
    )

    # 1. Download video directly into /dev/shm RAM disk
    temp_video_path = os.path.join(download_dir, f"raw_{message.id}_{filename}")
    temp_audio_path = os.path.join(download_dir, f"audio_{message.id}.m4a")

    try:
        await client.download_media(message, file_name=temp_video_path)
        if not os.path.exists(temp_video_path):
            LOGGER.error(f"Failed to download media for msg {message.id}")
            return None

        # 2. Extract AAC audio stream directly in RAM
        ok = await demux_audio_from_stream(temp_video_path, temp_audio_path)
        if not ok or not os.path.exists(temp_audio_path):
            LOGGER.error(f"Demuxing failed for msg {message.id}")
            return None

        # 3. Move audio to permanent vault (cross-device safe)
        shutil.move(temp_audio_path, final_audio_path)
        duration_ms = get_audio_duration_ms(final_audio_path)
        audio_sha = compute_sha256(final_audio_path)
        file_size = os.path.getsize(final_audio_path)

        # 3.1 Extract 16:9 thumbnail from source channel if present
        thumb_dest = os.path.join(DATA_DIR, "thumbnails", f"{clean_show}_S{season_num:02d}_thumb.jpg")
        if not os.path.exists(thumb_dest) and hasattr(media, "thumbs") and media.thumbs:
            try:
                os.makedirs(os.path.dirname(thumb_dest), exist_ok=True)
                await client.download_media(media.thumbs[-1], file_name=thumb_dest)
                LOGGER.info(f"Saved source 16:9 thumbnail to {thumb_dest}")
            except Exception as te:
                LOGGER.warning(f"Could not download source thumbnail: {te}")

        # 4. Associate with anime_seasons
        season_id = await get_or_create_season(
            title_romaji=title,
            season_number=season_num,
            total_episodes=12,
            season_state="HARVESTING",
        )

        # 5. Register in SQLite WAL DB
        file_unique_id = getattr(media, "file_unique_id", f"msg_{message.id}")
        audio_id = await register_audio_entry(
            season_id=season_id,
            episode_number=ep_num,
            source_msg_id=message.id,
            source_file_unique=file_unique_id,
            local_m4a_path=final_audio_path,
            duration_ms=duration_ms,
            audio_sha256=audio_sha,
            file_size_bytes=file_size,
        )

        LOGGER.info(
            f"✅ Harvested Msg #{message.id} -> {output_audio_name} "
            f"({duration_ms/1000:.1f}s, {file_size/(1024**2):.2f} MB, AudioID: {audio_id}, SeasonID: {season_id})"
        )
        return audio_id

    finally:
        # 6. Immediate Quota Governance: Delete raw video from RAM disk
        if os.path.exists(temp_video_path):
            try:
                os.remove(temp_video_path)
            except Exception:
                pass
        if os.path.exists(temp_audio_path):
            try:
                os.remove(temp_audio_path)
            except Exception:
                pass

async def harvest_channel(
    channel_id: int = DEFAULT_TAMIL_CHANNEL_ID,
    limit: int = 5,
    anime_name: Optional[str] = None,
    season_number: Optional[int] = None,
) -> List[int]:
    """
    Connect to Telegram using Premium session, harvest up to `limit` single episodes
    from channel, and register them in the SQLite WAL database.
    """
    season_str = f" S{season_number:02d}" if season_number is not None else ""
    LOGGER.info(f"Starting Tamil Audio Harvester for channel {channel_id} (Target limit: {limit} episodes, Filter: {anime_name}{season_str})")
    await init_db()

    app = Client(
        "starfall_harvester_session",
        api_id=TELEGRAM_API,
        api_hash=TELEGRAM_HASH,
        session_string=USER_SESSION_STRING,
        in_memory=True,
    )

    harvested_audio_ids = []

    async with app:
        me = await app.get_me()
        LOGGER.info(f"Connected as {me.first_name} (@{me.username}) [Premium={me.is_premium}]")

        # Warm up dialogs cache so private channel access hash is resolved in memory
        async for _ in app.get_dialogs(limit=50):
            pass

        chat = await app.get_chat(channel_id)
        LOGGER.info(f"Target Channel Found: '{chat.title}' (ID: {chat.id})")

        # Collect candidate single episode messages starting from message #1
        candidate_messages: List[Message] = []
        batch_size = 100
        current_id = 1
        max_scan_id = 10000

        while len(candidate_messages) < limit and current_id < max_scan_id:
            ids_to_fetch = list(range(current_id, current_id + batch_size))
            msgs = await app.get_messages(chat.id, ids_to_fetch)
            if not msgs:
                break
            for m in msgs:
                if not m:
                    continue
                media = m.video or m.document
                if not media:
                    continue
                text = m.caption or getattr(media, "file_name", "") or ""
                if is_single_episode(text):
                    t_parsed, s_parsed, _ = parse_episode_info(text)
                    if season_number is not None and s_parsed != season_number:
                        continue
                    if anime_name:
                        norm_target = re.sub(r"[^\w\s]", "", anime_name).lower().strip()
                        norm_parsed = re.sub(r"[^\w\s]", "", t_parsed).lower().strip()
                        if norm_target != norm_parsed:
                            target_words = set(norm_target.split())
                            parsed_words = set(norm_parsed.split())
                            extra = parsed_words - target_words
                            if extra & {"vigilantes", "movie", "film"}:
                                continue
                            if not (norm_target in norm_parsed or norm_parsed in norm_target):
                                continue
                    candidate_messages.append(m)
                    if len(candidate_messages) >= limit:
                        break
            current_id += batch_size

        LOGGER.info(f"Selected {len(candidate_messages)} single episode messages to harvest:")
        for idx, m in enumerate(candidate_messages, 1):
            fn = getattr(m.video or m.document, "file_name", "")
            LOGGER.info(f"  {idx}. Msg #{m.id}: {fn}")

        # Harvest each message sequentially to stay well within RAM disk limits
        for idx, m in enumerate(candidate_messages, 1):
            LOGGER.info(f"--- Processing {idx}/{len(candidate_messages)} ---")
            audio_id = await harvest_single_message(app, m, download_dir=RAM_MUX_DIR)
            if audio_id:
                harvested_audio_ids.append(audio_id)

    LOGGER.info(f"🎉 Successfully harvested {len(harvested_audio_ids)}/{limit} episodes.")
    return harvested_audio_ids

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Starfall Telegram Tamil Audio Harvester")
    parser.add_argument("--channel", type=int, default=DEFAULT_TAMIL_CHANNEL_ID, help="Telegram Channel ID")
    parser.add_argument("--limit", type=int, default=5, help="Number of episodes to harvest")
    args = parser.parse_args()

    async def _main():
        audio_ids = await harvest_channel(channel_id=args.channel, limit=args.limit)
        print("\n=== Harvest Summary ===")
        print(f"Total episodes harvested: {len(audio_ids)}")
        entries = await get_all_audio_entries(limit=10)
        print(f"\nIndexed Database Records ({len(entries)}):")
        for e in entries:
            dur_s = e["duration_ms"] / 1000
            sz_mb = e["file_size_bytes"] / (1024 ** 2)
            print(
                f"  • ID {e['audio_id']}: {e['title_romaji']} S{e['season_number']}E{e['episode_number']:02d} | "
                f"Msg #{e['source_msg_id']} | {dur_s:.1f}s | {sz_mb:.2f} MB | {os.path.basename(e['local_m4a_path'])}"
            )

    asyncio.run(_main())
