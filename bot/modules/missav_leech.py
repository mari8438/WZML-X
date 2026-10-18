import gc
from asyncio import (
    CancelledError,
    FIRST_COMPLETED,
    Event,
    Lock,
    Semaphore,
    create_subprocess_exec,
    create_task,
    gather,
    sleep,
    wait,
)
from asyncio.subprocess import PIPE
from contextlib import suppress
from html import escape
from json import loads
from os import listdir as oslistdir, path as ospath
from pathlib import Path
from re import sub
from shutil import rmtree
from urllib.parse import urlsplit

from ..helper.ext_utils.files_utils import split_file

from httpx import AsyncClient
from PIL import Image, ImageFilter, ImageOps
from pyrogram.errors import FloodWait

from .. import DOWNLOAD_DIR, LOGGER, user_data
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import cmd_exec, sync_to_async
from ..helper.ext_utils.hstream_translation import HstreamTranslator
from ..helper.ext_utils.missav_resolver import (
    MissAVQuality,
    MissAVResolver,
    MissAVTitle,
    is_fc2_title,
)
from ..helper.ext_utils.missav_subtitles import generate_all_subtitles_for_video
from ..helper.mirror_leech_utils.download_utils.yt_dlp_download import YoutubeDLHelper
from ..helper.poster_engine.engine import POSTER_TEMPLATE_COUNT, render_poster_option
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import edit_message, send_message
from .batch_task_registry import BatchTaskController

_RUN_LOCK = Lock()
_ACTIVE_RUNS = {}
_QUALITY_TIMEOUT = 50 * 60
_INVALID_FILENAME = r'[\\/:*?"<>|]'


async def _wait_until_resumed(pause_event, cancel_event):
    while not pause_event.is_set() and not cancel_event.is_set():
        resumed = create_task(pause_event.wait())
        cancelled = create_task(cancel_event.wait())
        done, pending = await wait((resumed, cancelled), return_when=FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await gather(*pending, return_exceptions=True)
        if cancelled in done:
            return False
    return not cancel_event.is_set()


async def missav_pause(_, message):
    gid = (message.text or "").split(maxsplit=1)
    gid = gid[1].strip() if len(gid) > 1 else ""
    state = _ACTIVE_RUNS.get(gid)
    if not state:
        await send_message(message, "MissAV task not found.")
        return
    state["pause"].clear()
    state["paused"] = True
    await send_message(
        message,
        "⏸ <b>MissAV paused.</b> Current download or remux step may finish before resting.",
    )


async def missav_resume(_, message):
    gid = (message.text or "").split(maxsplit=1)
    gid = gid[1].strip() if len(gid) > 1 else ""
    state = _ACTIVE_RUNS.get(gid)
    if not state:
        await send_message(message, "MissAV task not found.")
        return
    state["paused"] = False
    state["pause"].set()
    await send_message(message, "▶️ <b>MissAV resumed.</b>")


def _parse_destination(message, tokens):
    target = getattr(Config, "MISSAV_DUMP_CHAT", "") or "-1003969712219"
    try:
        destination = int(target)
    except (TypeError, ValueError):
        destination = target if target else message.chat.id

    thread_id = (
        message.message_thread_id
        if getattr(message, "is_topic_message", False)
        else None
    )
    if "-up" not in tokens:
        return destination, thread_id
    index = tokens.index("-up")
    if index + 1 >= len(tokens):
        raise ValueError("<code>-up</code> requires CHAT_ID or CHAT_ID|TOPIC_ID")
    raw = tokens[index + 1].strip()
    if "|" in raw:
        raw, topic = raw.rsplit("|", 1)
        try:
            thread_id = int(topic)
        except ValueError as error:
            raise ValueError("Invalid topic id after <code>|</code>") from error
    try:
        destination = int(raw)
    except ValueError:
        destination = raw
    return destination, thread_id


def _parse_letter_range(value):
    value = str(value or "").strip().upper()
    if len(value) == 1 and "A" <= value <= "Z":
        return [value]
    if (
        len(value) == 3
        and value[1] == "-"
        and "A" <= value[0] <= "Z"
        and "A" <= value[2] <= "Z"
    ):
        if value[0] > value[2]:
            raise ValueError("MissAV range must be ascending, for example <code>A-G</code>")
        return [chr(code) for code in range(ord(value[0]), ord(value[2]) + 1)]
    raise ValueError("Use a MissAV URL, one letter, or a range such as <code>A-G</code>")


def _safe_filename(value):
    value = sub(_INVALID_FILENAME, " ", str(value or ""))
    value = sub(r"\s+", " ", value).strip(" .-")
    return value[:160] or "MissAV"


def _video_filename(item: MissAVTitle, quality: MissAVQuality, tamil_available: bool) -> str:
    title = _safe_filename(item.title)[:110].rstrip(" .-")
    label = quality.label or "source"
    subtitle_label = "Tamil + ESub" if tamil_available else "ESub"
    return (
        f"🄰🅂- {title} [{label}] [Jap] {subtitle_label} ~ [@Anime_Starfall🥰].mkv"
    )


def _genre_hashtags(genres):
    tags = []
    for genre in genres:
        tag = sub(r"[^a-z0-9]+", "_", str(genre).strip().lower()).strip("_")
        if tag and tag not in tags:
            tags.append(tag)
    return " ".join(f"#{tag}" for tag in tags[:15]) or "#jav #uncensored"


def _split_description(description, limit=3000):
    words = str(description or "N/A").split()
    chunks = []
    current = []
    for word in words:
        candidate = " ".join((*current, word))
        if current and len(escape(candidate, quote=False)) > limit:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks or ["N/A"]


def _poster_caption(item: MissAVTitle, tamil_available: bool) -> tuple[str, list[str]]:
    title = escape(item.title, quote=False)
    date = escape(item.date or "N/A", quote=False)
    views = f"{item.views:,}" if item.views else "N/A"
    genres = _genre_hashtags(item.genres)
    description = escape(item.description or "N/A", quote=False)
    subtitle_label = "Tamil + ESub" if tamil_available else "ESub"

    cast_part = f"║ 🎭 Cast : {escape(', '.join(item.actors[:5]), quote=False)}\n" if item.actors else ""
    maker_part = f"║ 🏢 Maker : {escape(item.director, quote=False)}\n" if item.director else ""

    header = (
        f"<b>「 {title} 」\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "╔════◇═══════════◇════\n"
        f"║ 👁 {views} Views | 📅 {date}\n"
        f"║ 🇯🇵 Japanese ~ {subtitle_label}\n"
        f"{cast_part}"
        f"{maker_part}"
        "╚════◇═══════════◇════\n\n"
        f"Tags : {genres}</b>"
    )
    full = (
        f"{header}\n\n<blockquote expandable><b>Synopsis :\n{description}"
        "</b></blockquote>\n\n<b>Dropped By ➤ [@Anime_Starfall🥰]</b>"
    )
    if len(full) <= 1024:
        return full, []
    compact = f"{header}\n\n<b>Dropped By ➤ [@Anime_Starfall🥰]</b>"
    replies = []
    chunks = _split_description(item.description)
    for index, chunk in enumerate(chunks):
        label = "Synopsis :\n" if index == 0 else ""
        replies.append(
            f"<blockquote expandable><b>{label}{escape(chunk, quote=False)}</b></blockquote>"
        )
    return compact, replies


async def _download_image(url, path, thumbnail=False):
    if not url:
        return ""
    try:
        async with AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
        await sync_to_async(_save_image, response.content, path, thumbnail)
        return path
    except Exception as error:
        LOGGER.warning(f"MissAV image download failed: {error}")
        return ""


def _save_image(content, path, thumbnail):
    from io import BytesIO

    image = Image.open(BytesIO(content)).convert("RGB")
    if thumbnail:
        image = ImageOps.fit(image, (320, 180), Image.Resampling.LANCZOS)
        image = image.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=2))
        image.save(path, "JPEG", quality=94, optimize=True, subsampling=0)
    else:
        image = ImageOps.fit(image, (1280, 720), Image.Resampling.LANCZOS)
        image.save(path, "JPEG", quality=94, optimize=True, subsampling=0)


async def _video_info(video_path):
    result = await cmd_exec(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            video_path,
        ]
    )
    if result[2] != 0 or not result[0]:
        return {"duration": 0, "width": 0, "height": 0}
    try:
        payload = loads(result[0])
        video = next(
            (
                stream
                for stream in payload.get("streams", [])
                if stream.get("codec_type") == "video"
            ),
            {},
        )
        duration = round(float(payload.get("format", {}).get("duration") or 0))
        return {
            "duration": duration,
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
        }
    except Exception:
        return {"duration": 0, "width": 0, "height": 0}


async def _remux_with_subtitles(
    source_video: str,
    target_mkv: str,
    item: MissAVTitle,
    subtitle_tracks: list[tuple[str, str]],
    cancel_event: Event,
    processes: set,
) -> str:
    """Remux downloaded video with Starfall brand, English, and Tamil subtitle tracks."""
    # subtitle_tracks: list of (lang_code, file_path), e.g. [("brand", path), ("eng", path), ("tam", path)]
    inputs = [path for _, path in subtitle_tracks if ospath.isfile(path)]
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_video,
    ]
    for sub_path in inputs:
        command.extend(("-i", sub_path))

    command.extend(("-map", "0:v", "-map", "0:a?"))
    for input_index in range(1, len(inputs) + 1):
        command.extend(("-map", f"{input_index}:0"))

    metadata = {
        "title": item.title,
        "artist": "@Anime_Starfall🥰",
        "album": "@Anime_Starfall🥰",
        "album_artist": "@Anime_Starfall🥰",
        "composer": "@Anime_Starfall🥰",
        "publisher": "@Anime_Starfall🥰",
        "copyright": "@Anime_Starfall🥰",
        "comment": "Encoded by @Anime_Starfall🥰",
        "description": item.description or item.title,
        "synopsis": item.description or item.title,
    }
    command.extend(("-c", "copy", "-c:s", "ass", "-metadata:s:a", "language=jpn"))
    for key, value in metadata.items():
        command.extend(("-metadata", f"{key}={value}"))

    # Map subtitle metadata:
    # 0 -> Brand subtitle ("Anime Starfall", default)
    # 1 -> English Subtitles ("eng")
    # 2 -> Tamil Subtitles ("tam")
    sub_index = 0
    for lang, _ in subtitle_tracks:
        if lang == "brand":
            command.extend(
                (
                    f"-metadata:s:s:{sub_index}",
                    "language=und",
                    f"-metadata:s:s:{sub_index}",
                    "title=Anime Starfall",
                    f"-disposition:s:{sub_index}",
                    "default",
                )
            )
            sub_index += 1
        elif lang == "eng":
            command.extend(
                (
                    f"-metadata:s:s:{sub_index}",
                    "language=eng",
                    f"-metadata:s:s:{sub_index}",
                    "title=English Subtitles",
                )
            )
            sub_index += 1
        elif lang == "tam":
            command.extend(
                (
                    f"-metadata:s:s:{sub_index}",
                    "language=tam",
                    f"-metadata:s:s:{sub_index}",
                    "title=Tamil Subtitles",
                )
            )
            sub_index += 1

    command.append(target_mkv)
    process = await create_subprocess_exec(*command, stdout=PIPE, stderr=PIPE)
    processes.add(process)
    communicate = create_task(process.communicate())
    cancelled = create_task(cancel_event.wait())
    try:
        done, _ = await wait((communicate, cancelled), return_when=FIRST_COMPLETED)
        if cancelled in done or cancel_event.is_set():
            process.terminate()
            await process.wait()
            raise RuntimeError("MissAV remux cancelled")
        _, stderr = await communicate
        if process.returncode:
            raise RuntimeError(f"FFmpeg remux error: {stderr.decode(errors='ignore')[-800:]}")
    finally:
        processes.discard(process)
        cancelled.cancel()
        with suppress(CancelledError):
            await cancelled

    # Delete source unremuxed file
    if source_video != target_mkv:
        with suppress(OSError):
            Path(source_video).unlink()
    return target_mkv


async def _download_sample_images(sample_urls, directory):
    if not sample_urls:
        return []
    sample_dir = ospath.join(directory, "samples")
    Path(sample_dir).mkdir(parents=True, exist_ok=True)
    slots = Semaphore(6)
    # Select up to 16 samples evenly spread
    step = max(1, len(sample_urls) // 16)
    selected = sample_urls[::step][:16]

    async with AsyncClient(follow_redirects=True, timeout=30) as client:
        async def download(index, url):
            async with slots:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    target = ospath.join(sample_dir, f"{index:03d}.jpg")
                    Path(target).write_bytes(response.content)
                    return target
                except Exception as error:
                    LOGGER.debug(f"Seek image download failed: {error}")
                    return ""

        results = await gather(*(download(i, url) for i, url in enumerate(selected)))
    return [path for path in results if path]


def _create_sample_collage(sample_paths, target):
    valid = []
    for path in sample_paths:
        try:
            with Image.open(path) as img:
                img.verify()
            valid.append(path)
        except Exception:
            continue
    if not valid:
        return ""

    count = len(valid)
    columns = 4 if count >= 4 else count
    rows = (count + columns - 1) // columns
    tile_width = 1280 // columns
    tile_height = round(tile_width * 9 / 16)

    canvas = Image.new("RGB", (1280, rows * tile_height), "black")
    for idx, path in enumerate(valid):
        row = idx // columns
        col = idx % columns
        with Image.open(path) as img:
            tile = ImageOps.fit(img.convert("RGB"), (tile_width, tile_height), Image.Resampling.LANCZOS)
            canvas.paste(tile, (col * tile_width, row * tile_height))

    canvas.save(target, "JPEG", quality=90, optimize=True, subsampling=0)
    return target


class _DirectListener:
    def __init__(self, owner_id, cancel_event):
        self.user_id = owner_id
        self.user_dict = user_data.get(owner_id, {})
        self._cancel_event = cancel_event

    @property
    def is_cancelled(self):
        return self._cancel_event.is_set()


async def _telegram_call(method, **kwargs):
    while True:
        try:
            return await method(**kwargs)
        except FloodWait as error:
            await sleep(max(int(error.value), 1))


async def _upload_title(prepared, destination, thread_id, cancel_event):
    if not prepared or cancel_event.is_set():
        return 0

    main_dump = getattr(Config, "LEECH_DUMP_CHAT", "")
    try:
        main_dump = int(main_dump) if main_dump else None
    except (ValueError, TypeError):
        pass

    use_dump_stage = bool(main_dump and main_dump != destination)
    stage_chat = main_dump if use_dump_stage else destination

    kwargs = {"chat_id": stage_chat}
    if not use_dump_stage and thread_id is not None:
        kwargs["message_thread_id"] = thread_id

    async def _copy_and_delete(msg, reply_to_id=None):
        if not msg or not use_dump_stage:
            return msg
        copy_kwargs = {
            "chat_id": destination,
            "from_chat_id": msg.chat.id,
            "message_id": msg.id,
        }
        if thread_id is not None:
            copy_kwargs["message_thread_id"] = thread_id
        if reply_to_id is not None:
            copy_kwargs["reply_to_message_id"] = reply_to_id
        try:
            copied = await _telegram_call(TgClient.bot.copy_message, **copy_kwargs)
            with suppress(Exception):
                await msg.delete()
            return copied
        except Exception as error:
            LOGGER.warning(f"copy_message to destination failed: {error}")
            return msg

    # 1. Poster + Caption
    poster_msg = None
    if prepared["poster"] and ospath.isfile(prepared["poster"]):
        try:
            poster_msg = await _telegram_call(
                TgClient.bot.send_photo,
                photo=prepared["poster"],
                caption=prepared["caption"],
                **kwargs,
            )
        except Exception as error:
            LOGGER.warning(f"MissAV poster upload failed, sending text: {error}")
            poster_msg = await _telegram_call(
                TgClient.bot.send_message,
                text=prepared["caption"],
                **kwargs,
            )
    else:
        poster_msg = await _telegram_call(
            TgClient.bot.send_message,
            text=prepared["caption"],
            **kwargs,
        )

    final_poster_msg = await _copy_and_delete(poster_msg) if poster_msg else None

    for reply in prepared.get("caption_replies", []):
        if cancel_event.is_set():
            break
        reply_kwargs = dict(kwargs)
        if poster_msg:
            reply_kwargs["reply_to_message_id"] = poster_msg.id
        reply_msg = await _telegram_call(TgClient.bot.send_message, text=reply, **reply_kwargs)
        await _copy_and_delete(
            reply_msg,
            reply_to_id=final_poster_msg.id if final_poster_msg else None,
        )

    # 2. Upload video qualities
    uploaded = 0
    for quality, path in prepared["videos"]:
        if cancel_event.is_set():
            break
        info = await _video_info(path)
        height = info["height"] or quality.height or 720
        width = info["width"] or round(height * 16 / 9)
        file_size = ospath.getsize(path) if ospath.isfile(path) else 0

        # Select client: route large files (> 2000 MiB) through Premium user session if available
        client = (
            TgClient.user
            if (TgClient.user and (file_size > TgClient.NON_PREMIUM_SPLIT_SIZE or not TgClient.bot))
            else TgClient.bot
        )

        media = {
            **kwargs,
            "video": path,
            "caption": f"<b>{escape(ospath.basename(path), quote=False)}</b>",
            "duration": info["duration"],
            "width": width,
            "height": height,
            "supports_streaming": True,
        }
        if prepared["thumb"] and ospath.isfile(prepared["thumb"]):
            media["thumb"] = prepared["thumb"]
        if prepared["cover"] and ospath.isfile(prepared["cover"]):
            media["cover"] = prepared["cover"]

        max_upload_size = (
            TgClient.PREMIUM_SPLIT_SIZE
            if (client == TgClient.user and TgClient.IS_PREMIUM_USER)
            else TgClient.NON_PREMIUM_SPLIT_SIZE
        )

        async def _send_media_or_doc(c, m, p):
            try:
                return await _telegram_call(c.send_video, **m)
            except Exception as err:
                LOGGER.warning(f"send_video failed ({err}), falling back to document...")
                doc_m = dict(m)
                doc_m.pop("video", None)
                doc_m.pop("supports_streaming", None)
                doc_m.pop("duration", None)
                doc_m.pop("width", None)
                doc_m.pop("height", None)
                doc_m.pop("cover", None)
                doc_m["document"] = p
                try:
                    return await _telegram_call(c.send_document, **doc_m)
                except Exception as doc_err:
                    if c != TgClient.user and TgClient.user:
                        LOGGER.info(f"Retrying upload via TgClient.user for {p}: {doc_err}")
                        return await _telegram_call(TgClient.user.send_document, **doc_m)
                    raise doc_err

        if file_size > max_upload_size:
            LOGGER.info(f"File {path} ({file_size} bytes) exceeds limit {max_upload_size}. Splitting...")
            listener = _DirectListener(prepared.get("user_id", 0), cancel_event)
            split_ok = await split_file(path, max_upload_size, listener)
            if split_ok:
                parent = ospath.dirname(path)
                stem = ospath.splitext(ospath.basename(path))[0]
                ext = ospath.splitext(path)[1]
                parts = sorted(
                    [
                        ospath.join(parent, f)
                        for f in oslistdir(parent)
                        if f.startswith(f"{stem}.part") and f.endswith(ext)
                    ]
                )
                if parts:
                    for part_idx, part_path in enumerate(parts, start=1):
                        part_media = dict(media)
                        part_media["video"] = part_path
                        part_media["caption"] = (
                            f"<b>{escape(ospath.basename(part_path), quote=False)} "
                            f"[Part {part_idx}/{len(parts)}]</b>"
                        )
                        part_msg = await _send_media_or_doc(client, part_media, part_path)
                        await _copy_and_delete(part_msg)
                    uploaded += 1
                    continue

        video_msg = await _send_media_or_doc(client, media, path)
        await _copy_and_delete(video_msg)
        uploaded += 1

    # 3. Sample collage
    collage = prepared.get("sample_collage")
    if not cancel_event.is_set() and collage and ospath.isfile(collage):
        try:
            collage_msg = await _telegram_call(
                TgClient.bot.send_photo,
                photo=collage,
                caption=f"<b>Sample Images</b>\n<b>{escape(prepared['item'].title, quote=False)}</b>",
                **kwargs,
            )
            await _copy_and_delete(collage_msg)
        except Exception as error:
            LOGGER.warning(f"MissAV sample collage upload failed: {error}")

    return uploaded


async def missav_leech(_, message):
    tokens = (message.text or message.caption or "").split()
    if len(tokens) < 2:
        await send_message(
            message,
            "<b>Usage:</b> <code>/mll URL</code>, <code>/mll A</code>, or "
            "<code>/mll A-G [-up CHAT_ID|TOPIC_ID]</code>",
        )
        return

    try:
        letters = None if tokens[1].startswith("http") else _parse_letter_range(tokens[1])
        destination, thread_id = _parse_destination(message, tokens)
        await TgClient.bot.get_chat(destination)
    except Exception as error:
        await send_message(message, f"<b>Invalid parameters:</b> <code>{escape(str(error))}</code>")
        return

    if _RUN_LOCK.locked():
        await send_message(message, "Another MissAV leech task is already running.")
        return

    async with _RUN_LOCK:
        controller = BatchTaskController("mll", message)
        cancel_event = Event()
        pause_event = Event()
        pause_event.set()
        _ACTIVE_RUNS[controller.gid] = {
            "pause": pause_event,
            "cancel": cancel_event,
            "paused": False,
        }
        processes = set()
        translator = None

        async def cancel_run(_):
            cancel_event.set()
            if translator:
                with suppress(Exception):
                    await translator.stop()
            for p in list(processes):
                with suppress(Exception):
                    p.terminate()

        controller.register_cancel_callback(cancel_run)
        root = ospath.join(DOWNLOAD_DIR, "missav", controller.gid)
        Path(root).mkdir(parents=True, exist_ok=True)

        cancel_cmd = f"/{BotCommands.CancelTaskCommand[1]}_{controller.gid}"
        pause_cmd = f"/mlpause{Config.CMD_SUFFIX} {controller.gid}"
        resume_cmd = f"/mlresume{Config.CMD_SUFFIX} {controller.gid}"
        controls = f"Pause: <code>{pause_cmd}</code> | Resume: <code>{resume_cmd}</code>\nStop: <code>{cancel_cmd}</code>"

        status = await send_message(
            message,
            f"<b>MissAV resolving...</b>\n{controls}",
        )

        uploaded = fc2_skipped = failed = 0
        try:
            # Initialize NLLB Tamil translator
            translator = HstreamTranslator()
            try:
                await translator.start(cancel_event)
                LOGGER.info("MissAV NLLB Tamil translator started successfully.")
            except Exception as error:
                LOGGER.warning(f"MissAV Tamil translation unavailable; falling back to ESub: {error}")
                translator = None

            async with MissAVResolver() as resolver:
                if letters is None:
                    items = await resolver.discover(tokens[1])
                else:
                    items = []
                    seen_urls = set()
                    for letter in letters:
                        for item in await resolver.discover_letter(letter):
                            if item.url not in seen_urls:
                                seen_urls.add(item.url)
                                items.append(item)

            if not items:
                raise RuntimeError("No eligible MissAV videos found (or all were FC2-PPV skipped).")

            status = await edit_message(
                status,
                f"<b>MissAV Leech</b>\n"
                f"Titles to process: <code>{len(items)}</code>\n"
                f"FC2 Filter: <code>Enabled (skipped)</code>\n"
                f"Tamil Translation: <code>{'Ready' if translator else 'ESub only'}</code>\n"
                f"{controls}",
            )

            # Process each video sequentially to protect RAM and CPU
            for index, item in enumerate(items, start=1):
                if cancel_event.is_set():
                    break
                if not await _wait_until_resumed(pause_event, cancel_event):
                    break

                if is_fc2_title(item.title) or is_fc2_title(item.url):
                    LOGGER.info(f"Skipping FC2-PPV video: {item.title}")
                    fc2_skipped += 1
                    continue

                if not item.qualities:
                    LOGGER.warning(f"No stream qualities available for {item.title}")
                    failed += 1
                    continue

                title_dir = ospath.join(root, f"{index:04d}")
                Path(title_dir).mkdir(parents=True, exist_ok=True)

                try:
                    # 1. Poster & Artwork
                    cover = await _download_image(item.thumbnail, ospath.join(title_dir, "cover.jpg"))
                    thumb = ""
                    if cover:
                        thumb = ospath.join(title_dir, "thumb.jpg")
                        await sync_to_async(_save_image, Path(cover).read_bytes(), thumb, True)

                    # Poster rendering
                    owner_settings = user_data.get(controller.user_id, {})
                    template = str(owner_settings.get("POST_TEMPLATE_ID") or Config.POST_TEMPLATE_ID or 1)
                    if template not in {str(v) for v in range(1, POSTER_TEMPLATE_COUNT + 1)}:
                        template = "1"
                    poster = ""
                    try:
                        poster_meta = {
                            "title": item.title,
                            "year": item.date[:4] if item.date else "",
                            "release_date": item.date,
                            "description": item.description,
                            "plot": item.description,
                            "synopsis": item.description,
                            "genres": ", ".join(item.genres) or "N/A",
                            "views": f"{item.views:,}" if item.views else "N/A",
                            "cast": ", ".join(item.actors) or "N/A",
                            "director": item.director or "N/A",
                            "category": "movie",
                            "landscape_url": item.thumbnail,
                            "portrait_url": item.thumbnail,
                            "poster_url": item.thumbnail,
                            "filename": item.title,
                            "brand": "MissAV",
                        }
                        poster = await render_poster_option(
                            poster_meta,
                            controller.user_id,
                            user_dict=owner_settings,
                            option=template,
                        )
                        local_poster = ospath.join(title_dir, "poster.jpg")
                        await sync_to_async(Path(poster).replace, local_poster)
                        poster = local_poster
                    except Exception as error:
                        LOGGER.warning(f"Poster engine fallback: {error}")
                        poster = cover

                    # 2. Download lowest quality stream first to generate subtitles
                    base_quality = item.qualities[-1]  # Sorted highest to lowest; last is lowest
                    headers = MissAVResolver.request_headers(item.url)
                    helper = YoutubeDLHelper(_DirectListener(controller.user_id, cancel_event))
                    raw_base_path = ospath.join(title_dir, f"base_{base_quality.label}.mkv")

                    LOGGER.info(f"Downloading base stream ({base_quality.label}) for subtitle generation...")
                    await helper.download_direct_hls(base_quality.url, raw_base_path, headers)

                    info = await _video_info(raw_base_path)
                    duration = info["duration"] or item.duration

                    # 3. Generate subtitles (Audio -> Whisper English -> NLLB Tamil + Brand)
                    subtitle_tracks, tamil_available = await generate_all_subtitles_for_video(
                        raw_base_path,
                        title_dir,
                        duration,
                        translator,
                    )

                    # 4. Process and mux each available quality
                    videos = []
                    for q_idx, quality in enumerate(item.qualities):
                        if cancel_event.is_set():
                            break
                        if not await _wait_until_resumed(pause_event, cancel_event):
                            break

                        target_filename = _video_filename(item, quality, tamil_available)
                        target_mkv = ospath.join(title_dir, target_filename)

                        # If this is the already-downloaded base quality, reuse it directly
                        if quality.url == base_quality.url and ospath.isfile(raw_base_path):
                            source_path = raw_base_path
                        else:
                            source_path = ospath.join(title_dir, f"raw_{quality.label}.mkv")
                            LOGGER.info(f"Downloading quality {quality.label} for {item.title}...")
                            await helper.download_direct_hls(quality.url, source_path, headers)

                        # Remux with subtitles
                        muxed = await _remux_with_subtitles(
                            source_path,
                            target_mkv,
                            item,
                            subtitle_tracks,
                            cancel_event,
                            processes,
                        )
                        if muxed and ospath.isfile(muxed):
                            videos.append((quality, muxed))

                    if not videos:
                        raise RuntimeError(f"No qualities successfully downloaded or muxed for {item.title}")

                    # 5. Sample collage
                    sample_collage = ""
                    if item.sample_urls:
                        sample_paths = await _download_sample_images(item.sample_urls, title_dir)
                        if sample_paths:
                            collage_file = ospath.join(title_dir, "sample_collage.jpg")
                            sample_collage = await sync_to_async(_create_sample_collage, sample_paths, collage_file)

                    caption, caption_replies = _poster_caption(item, tamil_available)
                    prepared = {
                        "item": item,
                        "poster": poster,
                        "caption": caption,
                        "caption_replies": caption_replies,
                        "thumb": thumb,
                        "cover": cover,
                        "videos": videos,
                        "sample_collage": sample_collage,
                        "user_id": controller.user_id,
                    }

                    # 6. Upload
                    uploaded += await _upload_title(prepared, destination, thread_id, cancel_event)

                except Exception as error:
                    failed += 1
                    LOGGER.error(f"Failed processing MissAV title {item.title}: {error}", exc_info=True)
                finally:
                    await sync_to_async(rmtree, title_dir, ignore_errors=True)
                    gc.collect()

                if status and not cancel_event.is_set():
                    with suppress(Exception):
                        status = await edit_message(
                            status,
                            f"<b>MissAV Leech Progress</b>\n"
                            f"Processed: <code>{index}/{len(items)}</code>\n"
                            f"Uploaded: <code>{uploaded}</code> | FC2 Skipped: <code>{fc2_skipped}</code> | Failed: <code>{failed}</code>\n"
                            f"State: <code>{'paused' if not pause_event.is_set() else 'running'}</code>\n"
                            f"{controls}",
                        )

            final_state = "cancelled" if cancel_event.is_set() else "completed"
            if status:
                await edit_message(
                    status,
                    f"<b>MissAV leech {final_state}.</b>\n"
                    f"Uploaded: <code>{uploaded}</code> | FC2 Skipped: <code>{fc2_skipped}</code> | Failed: <code>{failed}</code>",
                )

        except Exception as error:
            LOGGER.error(f"MissAV leech run error: {error}", exc_info=True)
            await send_message(message, f"<b>MissAV leech failed:</b>\n<code>{escape(str(error)[:800])}</code>")
        finally:
            cancel_event.set()
            if translator:
                with suppress(Exception):
                    await translator.stop()
            for p in list(processes):
                with suppress(Exception):
                    p.terminate()
            await sync_to_async(rmtree, root, ignore_errors=True)
            _ACTIVE_RUNS.pop(controller.gid, None)
            controller.close()
            gc.collect()
