import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import aiosqlite
from pyrogram import Client
from pyrogram.errors import FloodWait

# Detect if running in Docker container or host
BASE_DIR = "/usr/src/app" if os.path.exists("/usr/src/app/config.py") else "/home/administrator/starfall"
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "harvest_overnight.log")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)
LOGGER = logging.getLogger("OvernightHarvester")

API_ID = 32226674
API_HASH = "c03defc25ce571541f214b07ed51ce6a"
SESSION = "AQHPBdcAnEZfnuHmmgKmAdnimSOkgiCrLiiWYYRVG9D_jHRWrfUaA369827pLO7ra1LFuUQEeym3pMBXn-RyJn5xyn92LZ3jnfP8fp2QsmCNi_esoERO9o1cY0moRB8r_ha8Sf3YYbuNOtLlb3qsGo-duNXe1JcQ2ktJn58mNzqsYr0A2a_cUehxTMNuUe_AESCe2svHzpYnRBl3CgJf1RsW-w5tyS4QIBqYg3V4DsO9P87tCZYDooeif0acCVuPU_8w0RquVVvg5_8tYBWKYbX1WAQ3XUHO-vglqAK07D5Ae9EEIsHT3jEFTDhheZekeyxe1m2jm7mfvheApmC6FqpfJHbRVgAAAAIPtT73AA"
CHANNEL_ID = -1004335327733

DATA_DIR = os.path.join(BASE_DIR, "data")
AUDIO_VAULT_DIR = os.path.join(DATA_DIR, "tam_audios")
RAM_MUX_DIR = "/dev/shm/starfall_mux" if os.path.exists("/dev/shm") else os.path.join(DATA_DIR, "tmp_mux")
DB_PATH = os.path.join(DATA_DIR, "starfall_state.db")
THUMBNAILS_DIR = os.path.join(BASE_DIR, "thumbnails")

os.makedirs(AUDIO_VAULT_DIR, exist_ok=True)
os.makedirs(RAM_MUX_DIR, exist_ok=True)
os.makedirs(THUMBNAILS_DIR, exist_ok=True)

def is_merged_season_file(duration_sec: int, text: str) -> bool:
    if duration_sec > 2400:  # > 40 minutes is a merged batch or movie
        return True
    lower = text.lower()
    if any(k in lower for k in ["merged", "complete season", "all episodes", "full season", "all in one", "batch"]):
        return True
    if re.search(r"S\d+E\d+[-_~]\d+|E\d+[-_~]E?\d+|01[-_~]\d{2}", text, re.IGNORECASE):
        return True
    return False

def clean_title_name(raw_title: str) -> str:
    t = re.sub(r"\[.*?\]|\(.*?\)|🄰🅂|[@#]\w+", "", raw_title)
    qual_pattern = r"\b(?:1080p|720p|2160p|480p|10bit|8bit|NF|CR|WEB-?DL|WEBRip|BluRay|BDRip|HDTV|[x×]26[45]|HEVC|AV1|Tamil|AAC|Dual-Audio|ESub|DDP\d\.\d)\b"
    t = re.sub(qual_pattern, "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(19|20)\d{2}\b", "", t)
    t = re.sub(r"S\d+$", "", t).strip()
    t = t.replace("_", " ").strip(" -_[]():")
    return re.sub(r"\s+", " ", t).strip()

def parse_episode_info(text: str):
    clean_text = text.replace("🄰🅂", "").replace("🥰", "").strip()
    s_e = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", clean_text)
    if s_e:
        season = int(s_e.group(1))
        ep = int(s_e.group(2))
        after = clean_text[s_e.end():].strip(" -_[]():")
        before = clean_text[:s_e.start()].strip(" -_[]():")
        candidate = after if len(after) > len(before) else before
        return clean_title_name(candidate), season, ep

    ep_match = re.search(r"(?:EP|Episode)[.\s_-]*(\d{1,4})", clean_text, re.IGNORECASE)
    if ep_match:
        ep = int(ep_match.group(1))
        before = clean_text[:ep_match.start()].strip(" -_[]():")
        return clean_title_name(before), 1, ep

    num_match = re.search(r"[-_\s](\d{1,3})(?:v\d)?(?:\.|\s|$)", clean_text)
    if num_match:
        ep = int(num_match.group(1))
        before = clean_text[:num_match.start()].strip(" -_[]():")
        return clean_title_name(before), 1, ep

    return clean_title_name(clean_text), 1, 1

def compute_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def get_audio_duration_ms(filepath: str) -> int:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
        out = subprocess.check_output(cmd, text=True).strip()
        return int(float(out) * 1000)
    except Exception:
        return 0

async def demux_audio(source_video: str, dest_audio: str) -> bool:
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", source_video, "-vn", "-sn", "-dn", "-c:a", "copy", dest_audio]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    return proc.returncode == 0

async def main():
    LOGGER.info("🚀 Starting Starfall Automated Overnight Channel Harvester...")
    LOGGER.info(f"Target Channel: {CHANNEL_ID}")
    LOGGER.info(f"Audio Vault: {AUDIO_VAULT_DIR}")
    LOGGER.info(f"RAM Workspace: {RAM_MUX_DIR}")
    LOGGER.info(f"Thumbnails Vault: {THUMBNAILS_DIR}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA synchronous = NORMAL;")

        async with Client("harvester_prod", api_id=API_ID, api_hash=API_HASH, session_string=SESSION, in_memory=True) as app:
            me = await app.get_me()
            LOGGER.info(f"Connected as: {me.first_name} (@{me.username})")

            async with db.execute("SELECT source_msg_id FROM tamil_audio_library") as cur:
                rows = await cur.fetchall()
                harvested_ids = {r[0] for r in rows}
            LOGGER.info(f"Previously harvested episodes in DB: {len(harvested_ids)}")

            count = 0
            skipped_merged = 0
            skipped_existing = 0

            async for msg in app.get_chat_history(CHANNEL_ID):
                media = msg.video or msg.document
                if not media:
                    continue

                if msg.id in harvested_ids:
                    skipped_existing += 1
                    continue

                duration_sec = getattr(media, "duration", 0) or 0
                filename = getattr(media, "file_name", "") or ""
                caption = msg.caption or ""
                check_text = f"{filename} {caption}"

                if is_merged_season_file(duration_sec, check_text):
                    LOGGER.info(f"⏩ Skipping single merged season file: Msg #{msg.id} ({duration_sec//60} mins, {filename[:50]})")
                    skipped_merged += 1
                    continue

                title, season_num, ep_num = parse_episode_info(check_text)
                clean_title = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
                if not clean_title or len(clean_title) < 2:
                    clean_title = f"Anime_{msg.id}"
                dest_audio_name = f"{clean_title}_S{season_num:02d}E{ep_num:02d}.m4a"
                final_audio_path = os.path.join(AUDIO_VAULT_DIR, dest_audio_name)

                # 1. Harvest original 16:9 landscape logo thumbnail if available
                thumb_target = os.path.join(THUMBNAILS_DIR, f"{clean_title}_S{season_num:02d}_thumb.jpg")
                if not os.path.exists(thumb_target) and getattr(media, "thumbs", None):
                    try:
                        await app.download_media(media.thumbs[-1].file_id, file_name=thumb_target)
                        LOGGER.info(f"📸 Harvested original 16:9 thumbnail: {os.path.basename(thumb_target)}")
                    except Exception as e:
                        LOGGER.warning(f"Could not download thumbnail for {clean_title}: {e}")

                if os.path.exists(final_audio_path) and os.path.getsize(final_audio_path) > 1024*1024:
                    LOGGER.info(f"⏩ Audio already exists on disk: {dest_audio_name}")
                    continue

                LOGGER.info(f"📥 Downloading Msg #{msg.id}: {clean_title} S{season_num}E{ep_num} ({media.file_size / (1024**2):.1f} MB)...")
                temp_video = os.path.join(RAM_MUX_DIR, f"temp_{msg.id}_{filename}")
                temp_audio = os.path.join(RAM_MUX_DIR, f"audio_{msg.id}.m4a")

                try:
                    await app.download_media(msg, file_name=temp_video)
                    if not os.path.exists(temp_video) or os.path.getsize(temp_video) == 0:
                        LOGGER.error(f"Download failed for msg #{msg.id}")
                        continue

                    ok = await demux_audio(temp_video, temp_audio)
                    if not ok or not os.path.exists(temp_audio):
                        LOGGER.error(f"Demux failed for msg #{msg.id}")
                        continue

                    shutil.move(temp_audio, final_audio_path)
                    dur_ms = get_audio_duration_ms(final_audio_path)
                    sha = compute_sha256(final_audio_path)
                    fsize = os.path.getsize(final_audio_path)
                    file_uid = getattr(media, "file_unique_id", f"msg_{msg.id}")

                    # Associate season
                    async with db.execute("SELECT season_id FROM anime_seasons WHERE title_romaji = ? AND season_number = ?", (clean_title, season_num)) as cur:
                        row = await cur.fetchone()
                        if row:
                            season_id = row[0]
                        else:
                            await db.execute(
                                "INSERT INTO anime_seasons (title_romaji, season_number, total_episodes, season_state) VALUES (?, ?, ?, ?)",
                                (clean_title, season_num, 12, "HARVESTING")
                            )
                            await db.commit()
                            async with db.execute("SELECT last_insert_rowid()") as cur2:
                                season_id = (await cur2.fetchone())[0]

                    await db.execute(
                        """
                        INSERT INTO tamil_audio_library (
                            season_id, episode_number, source_msg_id, source_file_unique,
                            local_m4a_path, duration_ms, audio_sha256, file_size_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_file_unique) DO UPDATE SET
                            local_m4a_path = excluded.local_m4a_path,
                            duration_ms = excluded.duration_ms,
                            audio_sha256 = excluded.audio_sha256,
                            file_size_bytes = excluded.file_size_bytes
                        """,
                        (season_id, ep_num, msg.id, file_uid, final_audio_path, dur_ms, sha, fsize)
                    )
                    await db.commit()

                    count += 1
                    LOGGER.info(f"✅ Harvested #{count}: {dest_audio_name} ({dur_ms/1000:.1f}s, {fsize/(1024**2):.2f} MB)")

                finally:
                    if os.path.exists(temp_video):
                        try:
                            os.remove(temp_video)
                        except Exception:
                            pass
                    if os.path.exists(temp_audio):
                        try:
                            os.remove(temp_audio)
                        except Exception:
                            pass

    LOGGER.info(f"🎉 Harvesting Complete! Total extracted: {count}, Merged skipped: {skipped_merged}, Already in DB: {skipped_existing}")

if __name__ == "__main__":
    asyncio.run(main())
