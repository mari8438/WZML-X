from html import escape
from contextlib import suppress
import re
from json import dump, load
from os import path as ospath
from re import sub as re_sub
from shlex import split as shlex_split
from shutil import copy2
from asyncio import Event, create_subprocess_exec, gather, sleep, wait_for
from asyncio.subprocess import PIPE, STDOUT
from io import BytesIO
from time import time
from urllib.parse import unquote, urlsplit

from aiofiles import open as aiopen
from aiofiles.os import listdir, makedirs, path as aiopath, remove as aioremove
from aioshutil import rmtree
from httpx import AsyncClient
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pyrogram.filters import create
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from .. import LOGGER
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import cmd_exec, derive_service_password, new_task, sync_to_async
from ..helper.ext_utils.happyfappy import HappyFappyClient, HappyFappyError
from ..helper.ext_utils.happyfappy_transaction import (
    TorrentValidationError,
    source_fingerprint,
    find_duplicate_transaction,
    find_transaction_by_source_url,
    update_transaction,
    validate_accepted_torrent,
)
from ..helper.ext_utils.media_utils import (
    format_clean_poster_title,
    get_release_description,
    get_media_info,
    get_multiple_frames_thumbnail,
    get_video_thumbnail,
    take_ss,
)
from ..helper.ext_utils.files_utils import get_path_size
from ..helper.ext_utils.pixhost import upload_image_details
from ..core.torrent_manager import TorrentManager
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import delete_message, edit_message, send_file, send_message

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".m4v", ".3gp",
    ".ogv", ".divx", ".rmvb", ".asf",
}


def _safe_name(name, fallback="release.mkv"):
    name = unquote((name or "").split("?", 1)[0].rsplit("/", 1)[-1]).strip()
    if not name:
        name = fallback
    name = re_sub(r'[\\/:*?"<>|\r\n]+', "_", name)
    name = re_sub(r"\s+", " ", name).strip(" .")
    return name[:180] or fallback


def _tracker_list():
    raw = str(getattr(Config, "CTORRENT_TRACKERS", "") or "")
    trackers = []
    for part in re_sub(r"[,\r]+", "\n", raw).splitlines():
        tracker = part.strip()
        if tracker and tracker not in trackers:
            trackers.append(tracker)
    return trackers


def _qbit_webui_url():
    base_url = str(getattr(Config, "BASE_URL", "") or "").rstrip("/")
    bot_id = (str(getattr(Config, "BOT_TOKEN", "") or "").split(":", 1)[0] or "0")
    if not base_url:
        return ""
    password = derive_service_password(bot_id, "qbit")
    return f"{base_url}/qbit/?pass={password}"


async def _unique_path(directory, filename):
    await makedirs(directory, exist_ok=True)
    stem, ext = ospath.splitext(filename)
    candidate = ospath.join(directory, filename)
    count = 2
    while await aiopath.exists(candidate):
        candidate = ospath.join(directory, f"{stem}_{count}{ext}")
        count += 1
    return candidate


async def _edit_progress(message, title, current=0, total=0, extra=""):
    if total:
        percent = round(current * 100 / total, 2)
        size = f"{get_readable_file_size(current)} / {get_readable_file_size(total)}"
        text = f"{title}\nProgress: {percent}%\nDone: {size}"
    elif current:
        text = f"{title}\nDone: {get_readable_file_size(current)}"
    else:
        text = title
    if extra:
        text += f"\n{extra}"
    return await edit_message(message, text)


async def _download_link(link, out_dir, status_msg):
    filename = _safe_name(urlsplit(link).path, "release.mkv")
    out_path = await _unique_path(out_dir, filename)
    downloaded = 0
    total = 0
    last_edit = 0

    async with AsyncClient(follow_redirects=True, timeout=None) as client:
        async with client.stream("GET", link) as response:
            response.raise_for_status()
            if cd := response.headers.get("content-disposition"):
                if "filename=" in cd:
                    filename = _safe_name(cd.rsplit("filename=", 1)[-1].strip('" '))
                    out_path = await _unique_path(out_dir, filename)
            total = int(response.headers.get("content-length") or 0)
            async with aiopen(out_path, "wb") as f:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    await f.write(chunk)
                    now = time()
                    if now - last_edit > 5:
                        await _edit_progress(
                            status_msg,
                            "Create Torrent: downloading source...",
                            downloaded,
                            total,
                        )
                        last_edit = now
    return out_path


async def _download_reply(message, out_dir, status_msg):
    reply = message.reply_to_message
    media = getattr(reply, reply.media.value) if reply and reply.media else None
    if not media:
        return None
    filename = _safe_name(getattr(media, "file_name", "") or "telegram_video.mkv")
    out_path = await _unique_path(out_dir, filename)
    total = getattr(media, "file_size", 0) or 0
    last_edit = 0

    async def progress(current, _):
        nonlocal last_edit
        now = time()
        if now - last_edit > 5:
            await _edit_progress(
                status_msg,
                "Create Torrent: downloading Telegram file...",
                current,
                total,
            )
            last_edit = now

    downloaded = await reply.download(file_name=out_path, progress=progress)
    return downloaded or out_path


def _safe_local_path(value):
    allowed_roots = (
        "/usr/src/app/downloads/",
        "/usr/src/app/torrents/",
    )
    if not value.startswith(allowed_roots):
        return ""
    return value


async def _confirm_hstream_stream(message, episode):
    token = f"ctorrent_hs_{message.from_user.id}_{int(time() * 1000)}"
    choices = {}
    lines = [f"<b>{escape(episode.title)}</b>", "Choose the HStream quality to download:"]
    for index, stream in enumerate(episode.streams):
        choices[str(index)] = stream
        lines.append(
            f"{index + 1}. <code>{escape(stream.label)}</code> "
            f"{escape(stream.resolution)} {escape(stream.codec)} {escape(stream.bit)}"
        )
    from ..helper.telegram_helper.button_build import ButtonMaker

    buttons = ButtonMaker()
    for index, stream in enumerate(episode.streams):
        buttons.data_button(stream.label[:24], f"{token}:{index}")
    buttons.data_button("Cancel", f"{token}:cancel")
    prompt = await send_message(message, "\n".join(lines), buttons.build_menu(2))
    event = Event()
    result = [None]

    async def callback(_, query):
        if query.from_user.id != message.from_user.id or not query.data.startswith(f"{token}:"):
            return
        await query.answer()
        value = query.data.split(":", 1)[1]
        result[0] = choices.get(value) if value != "cancel" else False
        event.set()

    handler = TgClient.bot.add_handler(
        CallbackQueryHandler(callback, filters=create(lambda _, __, update: update.data.startswith(token)))
    )
    try:
        await wait_for(event.wait(), timeout=120)
    except TimeoutError:
        result[0] = None
    finally:
        TgClient.bot.remove_handler(*handler)
    if result[0] is None:
        await edit_message(prompt, "HStream quality confirmation timed out; download cancelled.")
    elif result[0] is False:
        await edit_message(prompt, "HStream download cancelled by owner.")
    else:
        await edit_message(prompt, f"Confirmed: <code>{escape(result[0].label)}</code>. Download starting.")
    return result[0]


async def _wait_for_manual_torrent(message, destination, status_msg, happyfappy_client):
    """Wait for the owner to send the accepted HappyFappy .torrent file."""
    prompt = await send_message(
        message,
        "HappyFappy accepted the upload, but its torrent link could not be retrieved. "
        "Please send the accepted .torrent file or its HappyFappy torrent URL here within 3 minutes. "
        "The source will be preserved.",
    )
    event = Event()
    result = [None]
    manual_path = f"{destination}.manual"

    async def receive(_, incoming):
        if incoming.from_user is None or incoming.from_user.id != message.from_user.id:
            return
        document = getattr(incoming, "document", None)
        filename = str(getattr(document, "file_name", "") or "")
        text = str(getattr(incoming, "text", "") or getattr(incoming, "caption", "") or "")
        link_match = re.search(r"https://(?:www\.)?happyfappy\.net/torrents\.php\?[^\s<>]+", text, re.I)
        if link_match:
            candidate = link_match.group(0).rstrip(".,)>")
            parsed = urlsplit(candidate)
            if re.search(r"(?:^|&)action=download(?:&|$)", parsed.query, re.I):
                happyfappy_client.uploaded_torrent_url = candidate
                try:
                    await happyfappy_client.download_uploaded_torrent(manual_path)
                    await sync_to_async(copy2, manual_path, destination)
                    await aioremove(manual_path)
                    result[0] = "link"
                    LOGGER.info("Received and downloaded owner-provided HappyFappy torrent link")
                    event.set()
                except HappyFappyError:
                    happyfappy_client.uploaded_torrent_url = ""
                return
        if (
            not document
            or not filename.lower().endswith(".torrent")
        ):
            return
        downloaded = await incoming.download(file_name=manual_path)
        if not downloaded or not await aiopath.isfile(manual_path):
            return
        async with aiopen(manual_path, "rb") as torrent_file:
            header = await torrent_file.read(4096)
        if b"announce" not in header and b"info" not in header:
            return
        await sync_to_async(copy2, manual_path, destination)
        await aioremove(manual_path)
        result[0] = destination
        LOGGER.info("Received owner-provided HappyFappy torrent file")
        event.set()

    handler = TgClient.bot.add_handler(
        MessageHandler(
            receive,
                filters=create(
                    lambda _, __, update: bool(
                        (getattr(update, "document", None) or getattr(update, "text", None))
                        and getattr(getattr(update, "from_user", None), "id", None)
                        == message.from_user.id
                    )
                ),
        ),
        group=-1,
    )
    try:
        await wait_for(event.wait(), timeout=180)
    except TimeoutError:
        pass
    finally:
        TgClient.bot.remove_handler(*handler)
    if result[0]:
        await edit_message(prompt, "Manual HappyFappy torrent received. Starting qBittorrent verification...")
        if status_msg and result[0]:
            await _edit_progress(status_msg, "Create Torrent: manual HappyFappy torrent received; hash-checking...")
    else:
        LOGGER.warning("Timed out waiting for owner-provided HappyFappy torrent file or link")
        await edit_message(prompt, "Manual torrent wait timed out; keeping the source. No local fallback will be seeded.")
    return result[0]


async def _download_hstream_link(link, out_dir, status_msg, message):
    parsed = urlsplit(link)
    if parsed.netloc.lower().split(":", 1)[0] not in {"hstream.moe", "www.hstream.moe"}:
        raise RuntimeError("HappyFappy mode accepts Hstream.moe episode URLs only")
    from ..helper.ext_utils.hstream_resolver import HstreamCatalogItem, HstreamResolver

    slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not slug or parsed.path.rstrip("/").split("/")[-2:-1] != ["hentai"]:
        raise RuntimeError("Use an Hstream episode URL such as https://hstream.moe/hentai/<slug>")
    async with HstreamResolver() as resolver:
        episode = await resolver.resolve(HstreamCatalogItem(slug, 0, slug))
    if not episode.streams or not any(stream.urls for stream in episode.streams):
        raise RuntimeError("Hstream returned no playable video stream")
    stream = await _confirm_hstream_stream(message, episode)
    if not stream:
        raise RuntimeError("HStream quality was not confirmed by the owner")
    stream_url = stream.urls[0]
    output = await _unique_path(out_dir, _safe_name(episode.title, "hstream_release") + ".mkv")
    await _edit_progress(status_msg, "Create Torrent: downloading Hstream video...")
    process = await create_subprocess_exec(
        "yt-dlp", "--newline", "--no-playlist", "--no-check-certificates",
        "--progress-template", "download:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(progress._total_bytes_str)s",
        "--referer", link, "--merge-output-format", "mkv",
        "-f", "bv*+ba/b", "-o", output, stream_url,
        stdout=PIPE,
        stderr=STDOUT,
    )
    last_update = 0
    while line := await process.stdout.readline():
        text = line.decode(errors="ignore").strip()
        if not text.startswith("download:"):
            continue
        fields = text.removeprefix("download:").split("|", 3)
        if len(fields) != 4 or time() - last_update < 5:
            continue
        percent, speed, eta, total = fields
        await _edit_progress(
            status_msg,
            "Create Torrent: downloading Hstream video...",
            extra=f"Quality: <code>{escape(stream.label)}</code>\nProgress: <code>{escape(percent)}</code>\nSpeed: <code>{escape(speed)}</code>\nETA: <code>{escape(eta)}</code>\nTotal: <code>{escape(total)}</code>",
        )
        last_update = time()
    stderr = (await process.stderr.read()).decode(errors="ignore")
    code = await process.wait()
    if code != 0 or not await aiopath.exists(output):
        raise RuntimeError(stderr or "Hstream video download failed")
    metadata_path = f"{output}.hstream.json"
    with open(metadata_path, "w", encoding="utf-8") as metadata_file:
        dump(
            {
                "title": episode.title,
                "year": episode.year,
                "description": episode.description,
                "genres": episode.genres,
                "portrait_url": episode.portrait_url,
                "landscape_url": episode.landscape_url,
                "sample_urls": episode.sample_urls,
                "source_url": episode.source_url,
            },
            metadata_file,
            ensure_ascii=False,
        )
    return output


async def _resolve_source(message, status_msg, hstream_only=False):
    storage_dir = str(Config.CTORRENT_STORAGE_DIR or "/usr/src/app/torrents/seeding")
    await makedirs(storage_dir, exist_ok=True)
    text = message.text or message.caption or ""
    try:
        args = shlex_split(text)
    except ValueError:
        args = text.split()
    value = next((item for item in args[1:] if not item.startswith("--")), "")
    if value.startswith(("http://", "https://")):
        if hstream_only:
            return await _download_hstream_link(value, storage_dir, status_msg, message)
        return await _download_link(value, storage_dir, status_msg)
    if hstream_only:
        raise RuntimeError("HappyFappy mode requires an Hstream.moe episode URL")
    if value and (local_path := _safe_local_path(value)) and await aiopath.exists(local_path):
        if local_path.startswith(storage_dir.rstrip("/") + "/"):
            return local_path
        dest = await _unique_path(storage_dir, _safe_name(ospath.basename(local_path)))
        await sync_to_async(copy2, local_path, dest)
        return dest
    if message.reply_to_message:
        return await _download_reply(message, storage_dir, status_msg)
    return None


def _parse_folder_mode(message):
    text = message.text or message.caption or ""
    args = text.split(maxsplit=1)
    if len(args) < 2:
        return None
    rest = args[1]
    try:
        tokens = shlex_split(rest)
    except ValueError:
        tokens = rest.split()

    if "-" in tokens:
        sep = tokens.index("-")
        after_sep = tokens[sep + 1 :]
        links = [t for t in after_sep if t.startswith(("http://", "https://"))]
        if links:
            name_tokens = [t for t in tokens[:sep] if not t.startswith("-")]
            folder_name = _safe_name(" ".join(name_tokens), "release_folder")
            return folder_name, links
        if after_sep and all(t.isdigit() or t.startswith("-") for t in after_sep):
            pack_num = next((t for t in after_sep if t.isdigit()), "")
            name_tokens = [t for t in tokens[:sep] if not t.startswith("-")]
            full_name = f"{' '.join(name_tokens)} - {pack_num}".strip(" -")
            return _safe_name(full_name, "Hanime Pack"), []
        if sep > 0:
            name_tokens = [t for t in tokens[:sep] if not t.startswith("-")]
            folder_name = _safe_name(" ".join(name_tokens), "release_folder")
            return folder_name, []

    for flag in ("-m", "-i"):
        if flag in tokens:
            idx = tokens.index(flag)
            if idx + 1 < len(tokens):
                folder_name = _safe_name(tokens[idx + 1], "release_folder")
                links = [t for t in tokens if t.startswith(("http://", "https://"))]
                return folder_name, links

    if "--happyfappy" in tokens:
        pos_args = [t for t in tokens if not t.startswith("-")]
        if pos_args:
            first_arg = " ".join(pos_args)
            return _safe_name(first_arg, "release_folder"), []

    return None


async def _make_torrent(source_path, private=None, trackers=None, piece_length=None, source_tag=None):
    output_dir = str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    await makedirs(output_dir, exist_ok=True)
    torrent_name = f"{ospath.basename(source_path).strip()}.torrent"
    torrent_path = await _unique_path(output_dir, _safe_name(torrent_name, "release.torrent"))
    cmd = ["mktorrent", "-o", torrent_path]
    private = Config.CTORRENT_PRIVATE if private is None else private
    if private:
        cmd.append("-p")
    if source_tag:
        cmd.extend(["-s", str(source_tag)])
    trackers = _tracker_list() if trackers is None else trackers
    if trackers:
        cmd.extend(["-a", ",".join(trackers)])
    piece_length = piece_length or getattr(Config, "CTORRENT_PIECE_LENGTH", 23)
    try:
        piece_length = max(14, min(int(piece_length), 24))
    except (TypeError, ValueError):
        piece_length = 23
    cmd.extend(["-l", str(piece_length)])
    cmd.append(source_path)
    stdout, stderr, code = await cmd_exec(cmd)
    if code != 0 or not await aiopath.exists(torrent_path):
        raise RuntimeError(stderr or stdout or "mktorrent failed")
    return torrent_path, trackers


def _font(size):
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _truncate(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


def _format_duration(seconds):
    seconds = max(0, int(float(seconds or 0)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"


def _normalize_channels(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value or "")
    return {
        1: "1.0",
        2: "2.0",
        6: "5.1",
        8: "7.1",
    }.get(value, f"{value}ch")


def _clean_codec(value):
    value = str(value or "").lower()
    if value in {"hevc", "h265", "x265"}:
        return "H.265/HEVC"
    if value in {"h264", "avc", "x264"}:
        return "H.264/AVC"
    if value == "eac3":
        return "E-AC3"
    return value.upper() if value else ""


async def _probe_summary(source_path):
    summary = {
        "title": ospath.basename(source_path),
        "size": get_readable_file_size(await aiopath.getsize(source_path)),
        "duration": "",
        "resolution": "",
        "video": "",
        "bit_depth": "",
        "dynamic_range": "",
        "audio": "",
        "subtitles": "",
        "source_folder": ospath.dirname(source_path),
    }
    try:
        stdout, _, code = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                source_path,
            ]
        )
        if code != 0 or not stdout:
            return summary
        import json

        data = json.loads(stdout)
        fmt = data.get("format") or {}
        summary["duration"] = _format_duration(fmt.get("duration") or 0)
        audio = []
        subs = []
        for stream in data.get("streams", []):
            codec_type = stream.get("codec_type")
            tags = stream.get("tags") or {}
            lang = tags.get("language") or "und"
            if codec_type == "video" and not summary["video"]:
                width = stream.get("width")
                height = stream.get("height")
                if width and height:
                    summary["resolution"] = f"{width}x{height}"
                summary["video"] = _clean_codec(stream.get("codec_name"))
                bit_depth = (
                    stream.get("bits_per_raw_sample")
                    or stream.get("bits_per_sample")
                    or ""
                )
                if bit_depth:
                    summary["bit_depth"] = f"{bit_depth}bit"
                dyn_src = " ".join(
                    str(stream.get(k, ""))
                    for k in ("color_transfer", "color_primaries", "pix_fmt", "profile")
                ).lower()
                if "dovi" in dyn_src or "dolby" in dyn_src:
                    summary["dynamic_range"] = "DV"
                elif "smpte2084" in dyn_src or "hdr" in dyn_src:
                    summary["dynamic_range"] = "HDR"
                else:
                    summary["dynamic_range"] = "SDR"
            elif codec_type == "audio":
                item = " ".join(
                    part
                    for part in (
                        lang.upper(),
                        _clean_codec(stream.get("codec_name")),
                        _normalize_channels(stream.get("channels")),
                    )
                    if part
                )
                if item and item not in audio:
                    audio.append(item)
            elif codec_type == "subtitle":
                if lang and lang not in subs:
                    subs.append(lang)
        summary["audio"] = ", ".join(audio[:4])
        summary["subtitles"] = ", ".join(subs[:8])
    except Exception:
        return summary
    return summary


def _compact_media_info(summary):
    return "\n".join(
        f"{label}: {value}"
        for label, value in (
            ("File Size", summary.get("size")),
            ("Duration", summary.get("duration")),
            ("Resolution", summary.get("resolution")),
            ("Video", " ".join(
                part
                for part in (
                    summary.get("video"),
                    summary.get("bit_depth"),
                    summary.get("dynamic_range"),
                )
                if part
            )),
            ("Audio", summary.get("audio")),
            ("Subtitles", summary.get("subtitles")),
        )
        if value
    )


async def _create_contact_sheet_with_header(source_path, summary, title, output_dir=None):
    shots_dir = await take_ss(source_path, 15)
    if not shots_dir:
        return await get_multiple_frames_thumbnail(source_path, "5x3", False)
    try:
        shots = [
            ospath.join(shots_dir, name)
            for name in sorted(await listdir(shots_dir))
            if name.lower().endswith((".jpg", ".jpeg", ".png"))
        ][:15]
        if len(shots) < 15:
            return await get_multiple_frames_thumbnail(source_path, "5x3", False)

        output_dir = output_dir or str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
        await makedirs(output_dir, exist_ok=True)
        output = await _unique_path(output_dir, f"{_safe_name(title, 'contact_sheet')}.jpg")
        cols, rows = 5, 3
        cell_w, cell_h = 320, 180
        gap, outer, header_h, caption_h = 12, 18, 132, 24
        width = outer * 2 + cols * cell_w + (cols - 1) * gap
        height = outer * 2 + header_h + rows * (cell_h + caption_h) + (rows - 1) * gap
        canvas = Image.new("RGB", (width, height), "#15171c")
        draw = ImageDraw.Draw(canvas)
        title_font = _font(24)
        body_font = _font(15)
        small_font = _font(13)

        draw.text((outer, outer), _truncate(title, 82), fill="#ffffff", font=title_font)
        header_lines = [
            f"File: {_truncate(ospath.basename(source_path), 96)}",
            " | ".join(
                part
                for part in (
                    f"Size {summary.get('size')}",
                    f"Duration {summary.get('duration')}",
                    f"Resolution {summary.get('resolution')}",
                    f"Video {summary.get('video')} {summary.get('bit_depth')} {summary.get('dynamic_range')}".strip(),
                )
                if part and not part.endswith(" ")
            ),
            f"Audio: {_truncate(summary.get('audio') or 'Unknown', 120)}",
        ]
        for index, line in enumerate(header_lines):
            draw.text((outer, outer + 34 + index * 22), line, fill="#d5d8df", font=body_font)

        duration = (await get_media_info(source_path))[0] or 0
        interval = duration / 16 if duration else 0
        for index, shot in enumerate(shots):
            row = index // cols
            col = index % cols
            left = outer + col * (cell_w + gap)
            top = outer + header_h + row * (cell_h + caption_h + gap)
            with Image.open(shot) as image:
                image = image.convert("RGB")
                image = ImageOps.contain(image, (cell_w, cell_h), Image.Resampling.LANCZOS)
                frame = Image.new("RGB", (cell_w, cell_h), "#0d0f14")
                frame.paste(image, ((cell_w - image.width) // 2, (cell_h - image.height) // 2))
                canvas.paste(frame, (left, top))
            draw.rectangle([left, top, left + cell_w - 1, top + cell_h - 1], outline="#343844", width=1)
            draw.text(
                (left + 8, top + cell_h + 5),
                f"{index + 1:02}  {_format_duration(interval * (index + 1))}",
                fill="#cdd1d8",
                font=small_font,
            )
        canvas.save(output, "JPEG", quality=95, optimize=True)
        return output
    finally:
        await rmtree(shots_dir, ignore_errors=True)


BBCODE_TEMPLATES = {
    "anime_release": """[BG=https://images2.imgbox.com/88/eb/JAaZN8L1_o.jpg,100%][font=Palatino Linotype][color=#FFFFFF][BR]
[BG=#343434C0,60%,center][BG=#34343400,96%,center][BR][size=8][align=center][i] {title} [/i][/align][/size][BR][/BG][/BG]

[BG=#343434C0,85%,center][BG=#34343400,98%,center][align=center]
###

[BR][/align][/BG][/BG]

[BG=#343434C0,85%,center][BG=#34343400,96%,center][align=center][img]https://images2.imgbox.com/94/6c/om852SEy_o.png[/img][/align]
[size=2][align=justify][i]
{description}

{media_info}

[/i][/align][/size]
[/BG][/BG]

[BG=#343434C0,85%,center][BG=#34343400,98%,center][align=center][img]https://images2.imgbox.com/62/cd/IpX6mU4M_o.png[/img]
[BR]###
[BR][/align][/BG][/BG]
[/COLOR][/FONT][/BG]""",
    "dark_bg": """[center][size=6][b]{title}[/b][/size][/center]

[center][img]{thumbnail_link}[/img][/center]

[b]Description[/b]
{description}

[b]Media Info[/b]
[code]{media_info}[/code]

[center][img]{contact_sheet_link}[/img][/center]""",
    "classic": """[center][b][size=5]{title}[/size][/b][/center]

[b]Description[/b]
{description}

[b]Media Info[/b]
[spoiler]{media_info}[/spoiler]

Thumbnail: {thumbnail_link}
Contact Sheet: {contact_sheet_link}
Screenshots: {screenshots_link}""",
    "minimal": """[b]{title}[/b]

{description}

{media_info}

Thumbnail: {thumbnail_link}
Contact Sheet: {contact_sheet_link}""",
    "happyfappy": """[font=Tahoma][color=#cbd5e1]
[bg=#161b26,98%,center][bg=#00000000,96%,center]
[center][size=5][b][color=#93c5fd]{title}[/color][/b][/size][/center]
[/bg][/bg]

[table=98%,nball,vat,center,#141721]
[tr]
[td=vat,32%,nopad]
[center][url={thumbnail_page}][img]{thumbnail_link}[/img][/url][/center]
[/td]
[td=vat,68%]
[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Synopsis[/color][/b][/size][/bg][/bg]
{description}

[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Release Details[/color][/b][/size][/bg][/bg]
[color=#f8fafc][b]Title:[/b][/color] {title}
[color=#f8fafc][b]Genres / Tags:[/b][/color] {genres}

[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Technical Specs[/color][/b][/size][/bg][/bg]
[code]{media_info}[/code]
[/td]
[/tr]
[/table]

[bg=#161b26,98%,center][bg=#00000000,96%,center]
[center][size=4][b][color=#93c5fd]▌ Screenshots[/color][/b][/size][/center]
[/bg][/bg]

[center][url={contact_sheet_page}][img]{contact_sheet_link}[/img][/url][/center]

[center][size=2][color=#94a3b8][b]Direct Image Links:[/b] [url={thumbnail_link}]Cover[/url] | [url={contact_sheet_link}]Screenshots[/url][/color][/size][/center]
[/color][/font]""",
    "happyfappy_pack": """[font=Tahoma][color=#cbd5e1]
[bg=#161b26,98%,center][bg=#00000000,96%,center]
[center][size=5][b][color=#93c5fd]{title}[/color][/b][/size][/center]
[/bg][/bg]

[table=98%,nball,vat,center,#141721]
[tr]
[td=vat,32%,nopad]
[center][url={thumbnail_page}][img]{thumbnail_link}[/img][/url][/center]
[/td]
[td=vat,68%]
[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Pack Summary[/color][/b][/size][/bg][/bg]
{description}

[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Release Details[/color][/b][/size][/bg][/bg]
[color=#f8fafc][b]Pack Title:[/b][/color] {title}
[color=#f8fafc][b]Total Size:[/b][/color] {total_size}
[color=#f8fafc][b]Files Count:[/b][/color] {files_count} videos
[color=#f8fafc][b]Genres / Tags:[/b][/color] {genres}

[bg=#1e2433,98%,left][bg=#00000000,96%][size=3][b][color=#93c5fd]▌ Technical Specs[/color][/b][/size][/bg][/bg]
[code]{media_info}[/code]
[/td]
[/tr]
[/table]

[bg=#161b26,98%,center][bg=#00000000,96%,center]
[center][size=4][b][color=#93c5fd]▌ Scene Previews & Contact Sheets[/color][/b][/size][/center]
[/bg][/bg]

{spoilers}

[center][size=2][color=#94a3b8][b]Note:[/b] Full resolution contact sheets for all {files_count} scenes are included in the [b]Screenshots[/b] directory inside the torrent payload.[/color][/size][/center]
[/color][/font]""",
}


async def _load_bbcode_template(preset=None):
    template_path = str(getattr(Config, "CTORRENT_BBCODE_TEMPLATE_PATH", "") or "").strip()
    if template_path and await aiopath.exists(template_path):
        async with aiopen(template_path, encoding="utf-8", errors="ignore") as f:
            return await f.read()
    preset = str(preset or getattr(Config, "CTORRENT_BBCODE_TEMPLATE", "anime_release") or "anime_release").strip().lower()
    return BBCODE_TEMPLATES.get(preset) or BBCODE_TEMPLATES["anime_release"]


async def _write_description(source_path, title, summary, image_links=None, template_preset=None):
    output_dir = str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    media_info = _compact_media_info(summary)
    description = summary.get("description") or await get_release_description(title)
    if not description or not str(description).strip():
        description = f"{title} is a video release with the media details listed below."
    template = await _load_bbcode_template(template_preset)
    image_links = image_links or {}
    values = {
        "title": title,
        "description": description,
        "genres": ", ".join(summary.get("genres", [])) or "Not specified.",
        "media_info": media_info,
        "mediainfo": media_info,
        "thumbnail_link": image_links.get("thumbnail", ""),
        "thumbnail_page": image_links.get("thumbnail_page") or image_links.get("thumbnail", ""),
        "contact_sheet_link": image_links.get("contact_sheet", ""),
        "contact_sheet_page": image_links.get("contact_sheet_page") or image_links.get("contact_sheet", ""),
        "screenshots_link": image_links.get("contact_sheet", ""),
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value or ""))
    desc_path = await _unique_path(output_dir, f"{_safe_name(title, 'description')}.description.txt")
    async with aiopen(desc_path, "w", encoding="utf-8") as f:
        await f.write(rendered.rstrip() + "\n")
    return desc_path


def _read_hstream_metadata(source_path):
    metadata_path = f"{source_path}.hstream.json"
    try:
        with open(metadata_path, encoding="utf-8") as metadata_file:
            value = load(metadata_file)
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


async def _create_preview_webp(source_path, title, duration=10, output_dir=None):
    output_dir = output_dir or str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    await makedirs(output_dir, exist_ok=True)
    output = await _unique_path(output_dir, f"{_safe_name(title, 'preview')}.preview.webp")
    total_duration = (await get_media_info(source_path))[0] or 0

    # Sample video (-sv) workflow: sample 4 parts of 5s each (total 20s) across the video,
    # then speed up 2x (setpts=0.5*PTS) into a dynamic 10-second fast-paced preview.
    part_dur = 5.0
    if total_duration and total_duration > 30:
        starts = [
            total_duration * 0.15,
            total_duration * 0.40,
            total_duration * 0.65,
            total_duration * 0.85,
        ]
    elif total_duration and total_duration > 15:
        part_dur = max(2.5, (total_duration - 4.0) / 4.0)
        starts = [
            total_duration * 0.10,
            total_duration * 0.35,
            total_duration * 0.60,
            total_duration * 0.80,
        ]
    else:
        starts = [0.0]
        part_dur = min(float(duration), max(1.0, float(total_duration))) if total_duration else float(duration)

    cmd = ["ffmpeg", "-y"]
    for s in starts:
        cmd.extend(["-ss", f"{s:.2f}", "-t", f"{part_dur:.2f}", "-i", source_path])

    n = len(starts)
    if n > 1:
        inputs_str = "".join(f"[{i}:v]" for i in range(n))
        filter_complex = f"{inputs_str}concat=n={n}:v=1:a=0[vcat];[vcat]setpts=0.5*PTS,fps=12,scale=400:-1:flags=lanczos[out]"
    else:
        filter_complex = "fps=12,scale=400:-1:flags=lanczos[out]"

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-vcodec", "libwebp",
        "-lossless", "0",
        "-compression_level", "4",
        "-qscale", "70",
        "-loop", "0",
        "-an",
        output,
    ])

    result = await cmd_exec(cmd)
    if result[2] != 0 or not await aiopath.isfile(output) or (await aiopath.getsize(output) == 0):
        LOGGER.warning("FFmpeg sample-video preview WebP generation failed: %s", (result[1] or "")[:300])
        fallback_cmd = [
            "ffmpeg", "-y",
            "-ss", f"{starts[0]:.2f}",
            "-t", str(duration),
            "-i", source_path,
            "-vf", "fps=12,scale=400:-1:flags=lanczos",
            "-vcodec", "libwebp",
            "-lossless", "0",
            "-compression_level", "4",
            "-qscale", "70",
            "-loop", "0",
            "-an",
            output,
        ]
        fb_res = await cmd_exec(fallback_cmd)
        if fb_res[2] != 0 or not await aiopath.isfile(output) or (await aiopath.getsize(output) == 0):
            LOGGER.warning("FFmpeg single-seek fallback also failed: %s", (fb_res[1] or "")[:300])
            return ""

    file_size = await aiopath.getsize(output)
    if file_size > 9.5 * 1024 * 1024:
        LOGGER.warning("Preview WebP exceeds 9.5MB (%d bytes), re-encoding with optimized profile", file_size)
        fallback_output = await _unique_path(output_dir, f"{_safe_name(title, 'preview')}.opt.webp")
        re_cmd = ["ffmpeg", "-y"]
        for s in starts:
            re_cmd.extend(["-ss", f"{s:.2f}", "-t", f"{part_dur:.2f}", "-i", source_path])
        if n > 1:
            inputs_str = "".join(f"[{i}:v]" for i in range(n))
            re_filter = f"{inputs_str}concat=n={n}:v=1:a=0[vcat];[vcat]setpts=0.5*PTS,fps=10,scale=360:-1:flags=lanczos[out]"
        else:
            re_filter = "fps=10,scale=360:-1:flags=lanczos[out]"
        re_cmd.extend([
            "-filter_complex", re_filter,
            "-map", "[out]",
            "-vcodec", "libwebp",
            "-lossless", "0",
            "-compression_level", "4",
            "-qscale", "60",
            "-loop", "0",
            "-an",
            fallback_output,
        ])
        re_result = await cmd_exec(re_cmd)
        if re_result[2] == 0 and await aiopath.isfile(fallback_output) and (await aiopath.getsize(fallback_output) > 0):
            with suppress(OSError):
                await aioremove(output)
            output = fallback_output

    return output


async def _create_hstream_contact_sheet(metadata, title, output_dir):
    urls = [str(url).strip() for url in metadata.get("sample_urls", []) if str(url).strip()]
    if not urls:
        return ""
    sample_dir = await _unique_path(output_dir, f"{_safe_name(title, 'hstream')}_samples")
    await makedirs(sample_dir, exist_ok=True)
    async with AsyncClient(
        follow_redirects=True,
        timeout=45,
        verify=False,
        headers={"Referer": metadata.get("source_url", "https://hstream.moe/")},
    ) as client:
        async def fetch(index, url):
            try:
                response = await client.get(url)
                response.raise_for_status()
                with Image.open(BytesIO(response.content)) as image:
                    target = ospath.join(sample_dir, f"{index:02d}.jpg")
                    image.convert("RGB").save(target, "JPEG", quality=92, optimize=True)
                    return target
            except Exception as error:
                LOGGER.warning("Hstream screenshot %s failed: %s", index + 1, error)
                return ""

        screenshots = [item for item in await gather(*(fetch(i, url) for i, url in enumerate(urls))) if item]
    if not screenshots:
        return ""

    if len(screenshots) < 12:
        selected = []
        while len(selected) < 12:
            for s in screenshots:
                selected.append(s)
                if len(selected) == 12:
                    break
    elif len(screenshots) == 12:
        selected = screenshots
    else:
        indices = [round(i * (len(screenshots) - 1) / 11) for i in range(12)]
        selected = [screenshots[i] for i in indices]

    output = await _unique_path(output_dir, f"{_safe_name(title, 'hstream')}_12screens.jpg")
    cols, rows, cell_w, cell_h, gap = 4, 3, 320, 180, 10
    canvas = Image.new("RGB", (cols * cell_w + (cols + 1) * gap, rows * cell_h + (rows + 1) * gap), "#101218")
    for index, screenshot in enumerate(selected):
        with Image.open(screenshot) as image:
            image = ImageOps.contain(image.convert("RGB"), (cell_w, cell_h), Image.Resampling.LANCZOS)
            frame = Image.new("RGB", (cell_w, cell_h), "#000000")
            frame.paste(image, ((cell_w - image.width) // 2, (cell_h - image.height) // 2))
        canvas.paste(frame, (gap + (index % cols) * (cell_w + gap), gap + (index // cols) * (cell_h + gap)))
    canvas.save(output, "JPEG", quality=94, optimize=True)
    return output


async def _download_image(url, output_path):
    if not str(url or "").startswith(("http://", "https://")):
        return ""
    try:
        async with AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
        async with aiopen(output_path, "wb") as image_file:
            await image_file.write(response.content)
        return output_path
    except Exception:
        return ""


async def _send_artifacts(message, source_path, torrent_path, trackers, send=True, image_links=None, template_preset=None):
    size = await aiopath.getsize(source_path)
    hstream_metadata = _read_hstream_metadata(source_path)
    title, _, _ = format_clean_poster_title(ospath.basename(source_path))
    title = hstream_metadata.get("title") or title or ospath.splitext(ospath.basename(source_path))[0]
    summary = await _probe_summary(source_path)
    summary["title"] = title
    hstream_description = str(hstream_metadata.get("description") or "").strip()
    genres = [
        str(value).strip()
        for value in hstream_metadata.get("genres", [])
        if str(value).strip() and "rape" not in str(value).casefold()
    ]
    summary["genres"] = genres
    summary["year"] = str(hstream_metadata.get("year") or "")
    if hstream_description:
        if genres:
            hstream_description += f" Genres include {', '.join(genres)}."
        summary["description"] = hstream_description
    cover_path = await _download_image(
        hstream_metadata.get("portrait_url") or hstream_metadata.get("landscape_url"),
        ospath.join(ospath.dirname(source_path), f"{_safe_name(title, 'hstream')}.hstream-cover.jpg"),
    )
    preview_cover = ""
    if template_preset == "happyfappy":
        preview_cover = await _create_preview_webp(source_path, title)
    thumb = preview_cover or cover_path or await get_video_thumbnail(source_path, None)
    if hstream_metadata.get("sample_urls"):
        sheet = await _create_hstream_contact_sheet(
            hstream_metadata, title, str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
        )
    else:
        sheet = ""
    if not sheet:
        sheet = await _create_contact_sheet_with_header(source_path, summary, title)
    if not sheet:
        raise RuntimeError("Could not create contact sheet for release")
    desc_path = await _write_description(source_path, title, summary, image_links, template_preset)
    if hstream_description:
        template = await _load_bbcode_template(template_preset)
        rendered = template
        values = {
            "title": title,
            "description": hstream_description,
            "genres": ", ".join(genres) or "Not specified.",
            "media_info": _compact_media_info(summary),
            "mediainfo": _compact_media_info(summary),
            "thumbnail_link": (image_links or {}).get("thumbnail", ""),
            "thumbnail_page": (image_links or {}).get("thumbnail_page") or (image_links or {}).get("thumbnail", ""),
            "contact_sheet_link": (image_links or {}).get("contact_sheet", ""),
            "contact_sheet_page": (image_links or {}).get("contact_sheet_page") or (image_links or {}).get("contact_sheet", ""),
            "screenshots_link": (image_links or {}).get("contact_sheet", ""),
        }
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value or ""))
        async with aiopen(desc_path, "w", encoding="utf-8") as desc_file:
            await desc_file.write(rendered.rstrip() + "\n")

    if send and thumb:
        thumb_caption = "10-second animated preview cover (.webp)" if thumb.lower().endswith(".webp") else "HD video thumbnail"
        await send_file(message, thumb, thumb_caption)
    elif send:
        await send_message(message, "Create Torrent: HD thumbnail failed, continuing.")

    if send and sheet:
        sheet_caption = "12 screenshot contact sheet" if "_12screens" in sheet else "15 screenshot contact sheet"
        await send_file(message, sheet, sheet_caption)
    elif send:
        await send_message(message, "Create Torrent: contact sheet failed, continuing.")

    if send:
        await send_file(message, desc_path, "BBCode description")

    caption = (
        "<b>Created Torrent</b>\n"
        f"File: <code>{escape(ospath.basename(source_path))}</code>\n"
        f"Size: <code>{get_readable_file_size(size)}</code>\n"
        f"Title: <code>{escape(title)}</code>\n"
        f"Saved Folder: <code>{escape(ospath.dirname(source_path))}</code>\n"
        f"Torrent Path: <code>{escape(torrent_path)}</code>\n"
        f"Trackers: <code>{len(trackers)}</code>\n"
        f"Mode: <code>{'Private' if Config.CTORRENT_PRIVATE else 'Public'}</code>"
    )
    if send:
        await send_file(message, torrent_path, caption)
    return {"title": title, "summary": summary, "thumb": thumb, "sheet": sheet, "desc_path": desc_path}


def _normalize_gazelle_tag(tag):
    cleaned = re.sub(r"[^a-zA-Z0-9.]+", ".", str(tag or "").strip()).strip(".")
    cleaned = re.sub(r"\.+", ".", cleaned).lower()
    return cleaned


def _happyfappy_tags(title, summary):
    raw = str(getattr(Config, "HAPPYFAPPY_TAGS", "") or "")
    tags = []
    for item in raw.replace(",", " ").split():
        t = _normalize_gazelle_tag(item)
        if t and "rape" not in t and t not in tags:
            tags.append(t)
    for g in summary.get("genres", []):
        t = _normalize_gazelle_tag(g)
        if t and "rape" not in t and t not in tags:
            tags.append(t)
    for value in (
        summary.get("resolution"),
        summary.get("video"),
        summary.get("dynamic_range"),
        summary.get("year"),
        str(summary.get("title", "")).rsplit(".", 1)[-1],
    ):
        if not value:
            continue
        t = _normalize_gazelle_tag(value)
        if t and "rape" not in t and t not in tags:
            tags.append(t)
    # Guarantee at least 5 meaningful tags for HappyFappy
    fallback_tags = ["hentai", "anime", "pron", "video", "japanese", "hd", "uncensored"]
    for ft in fallback_tags:
        if len(tags) >= 6:
            break
        if ft not in tags:
            tags.append(ft)
    return " ".join(tags)


async def _seed_with_qbit(torrent_path, source_path, status_msg=None, transaction_torrent_path=None):
    transaction_torrent_path = transaction_torrent_path or torrent_path
    if not TorrentManager.qbittorrent:
        raise RuntimeError("qBittorrent is unavailable; refusing to seed")
    if not await aiopath.exists(source_path):
        raise RuntimeError(f"qBittorrent source is missing: {source_path}")
    update_transaction(transaction_torrent_path, "HASH_CHECKING", source_path=source_path)
    source_size = await get_path_size(source_path)
    from aioqbt.api import AddFormBuilder
    form = AddFormBuilder.with_client(TorrentManager.qbittorrent)
    async with aiopen(torrent_path, "rb") as torrent:
        data = await torrent.read()
    tag = f"happyfappy-{int(time())}"
    existing = await TorrentManager.qbittorrent.torrents.info()
    existing_hashes = {str(getattr(item, "hash", "")) for item in existing}
    form = form.include_file(data).savepath(ospath.dirname(source_path)).tags([tag])
    try:
        await TorrentManager.qbittorrent.torrents.add(form.build())
    except Exception as exc:
        if not await TorrentManager.qbittorrent.torrents.info(tag=tag):
            raise RuntimeError(f"qBittorrent add failed: {exc}") from exc
    torrents = []
    for _ in range(15):
        torrents = await TorrentManager.qbittorrent.torrents.info(tag=tag)
        if torrents:
            break
        # qBittorrent can acknowledge the add before its tag index is ready.
        await sleep(1)
    if not torrents:
        all_torrents = await TorrentManager.qbittorrent.torrents.info()
        source_name = ospath.basename(source_path)
        torrents = [
            item for item in all_torrents
            if str(getattr(item, "hash", "")) not in existing_hashes
            and getattr(item, "name", "") == source_name
            and getattr(item, "content_path", "") == source_path
        ]
    if not torrents:
        raise RuntimeError("qBittorrent did not report the uploaded torrent")
    if len(torrents) > 1:
        raise RuntimeError("qBittorrent reported multiple matching torrents; refusing ambiguous seed")
    torrent_hash = torrents[0].hash
    reported_size = int(getattr(torrents[0], "size", 0) or 0)
    if reported_size and abs(reported_size - source_size) > 1024 * 1024:
        raise RuntimeError(
            f"qBittorrent source size mismatch: file={source_size} bytes, torrent={reported_size} bytes"
        )
    reported_path = str(getattr(torrents[0], "content_path", "") or "")
    expected_dir = ospath.dirname(source_path)
    reported_dir = str(getattr(torrents[0], "save_path", "") or "")
    if reported_path and not ospath.isabs(reported_path):
        reported_path = ospath.join(reported_dir, reported_path)
    if reported_path and ospath.abspath(reported_path) != ospath.abspath(source_path):
        raise RuntimeError(
            f"qBittorrent content path mismatch: expected={source_path}, reported={reported_path}"
        )
    if reported_dir and ospath.abspath(reported_dir) != ospath.abspath(expected_dir):
        raise RuntimeError(
            f"qBittorrent save path mismatch: expected={expected_dir}, reported={reported_dir}"
        )
    if status_msg:
        qbit_url = _qbit_webui_url()
        qbit_link = f'\n<a href="{escape(qbit_url)}">Open qBittorrent live status</a>' if qbit_url else ""
        await _edit_progress(
            status_msg,
            f"Create Torrent: hash-checking in qBittorrent... <code>{get_readable_file_size(source_size)}</code>{qbit_link}",
        )
    await TorrentManager.qbittorrent.torrents.recheck([torrent_hash])
    last_report = 0
    last_state = "unknown"
    last_progress = 0.0
    last_progress_at = time()
    recheck_retries = 0
    for _ in range(180):
        if not await aiopath.exists(source_path):
            raise RuntimeError(f"qBittorrent source disappeared during hash-check: {source_path}")
        checked = await TorrentManager.qbittorrent.torrents.info(hashes=[torrent_hash])
        if checked:
            state = str(getattr(checked[0], "state", "unknown"))
            progress = float(getattr(checked[0], "progress", 0) or 0)
            amount_left = int(getattr(checked[0], "amount_left", 0) or 0)
            if progress > last_progress or amount_left < source_size:
                last_progress_at = time()
            last_state, last_progress = state, progress
            now = time()
            if now - last_report >= 5:
                LOGGER.info(
                    "qBittorrent hash-check: hash=%s state=%s progress=%.3f amount_left=%s path=%s",
                    torrent_hash,
                    state,
                    progress,
                    amount_left,
                    source_path,
                )
                if status_msg:
                    await _edit_progress(
                        status_msg,
                        f"Create Torrent: hash-checking... {progress * 100:.1f}% "
                        f"(<code>{escape(state)}</code>)",
                    )
                last_report = now
            if state in {"missingFiles", "error"}:
                raise RuntimeError(
                    f"qBittorrent hash-check failed: state={state}, progress={progress:.3f}, "
                    f"path={source_path}"
                )
            if progress >= 0.999 and amount_left == 0:
                break
            if (
                state in {"checkingResumeData", "checkingDL"}
                and recheck_retries < 2
                and time() - last_progress_at >= 30
            ):
                recheck_retries += 1
                LOGGER.warning(
                    "qBittorrent hash-check stalled; pausing and retrying recheck "
                    "(%s/2): hash=%s state=%s progress=%.3f path=%s",
                    recheck_retries,
                    torrent_hash,
                    state,
                    progress,
                    source_path,
                )
                await TorrentManager.qbittorrent.torrents.pause([torrent_hash])
                await sleep(1)
                await TorrentManager.qbittorrent.torrents.recheck([torrent_hash])
                last_progress_at = time()
        await sleep(2)
    else:
        raise RuntimeError(
            f"qBittorrent hash-check stalled: state={last_state}, progress={last_progress:.3f}, "
            f"source={source_path}, size={source_size} bytes"
        )
    await TorrentManager.qbittorrent.torrents.start([torrent_hash])
    final_state = "unknown"
    for _ in range(30):
        checked = await TorrentManager.qbittorrent.torrents.info(hashes=[torrent_hash])
        if checked:
            final_state = str(getattr(checked[0], "state", "unknown"))
            amount_left = int(getattr(checked[0], "amount_left", 0) or 0)
            if amount_left == 0 and final_state in {"uploading", "seeding", "forcedUP", "stalledUP"}:
                update_transaction(
                    transaction_torrent_path,
                    "SEEDING",
                    qbit_hash=torrent_hash,
                    qbit_state=final_state,
                )
                if status_msg:
                    qbit_url = _qbit_webui_url()
                    qbit_link = f'\n<a href="{escape(qbit_url)}">Open qBittorrent live status</a>' if qbit_url else ""
                    await _edit_progress(
                        status_msg,
                        f"Create Torrent: seeding confirmed — <code>{escape(final_state)}</code>{qbit_link}",
                    )
                return torrent_hash
            if final_state in {"error", "missingFiles"}:
                raise RuntimeError(f"qBittorrent did not start seeding: state={final_state}")
        await sleep(2)
    raise RuntimeError(f"qBittorrent did not reach a seeding state: state={final_state}")


async def _remove_failed_happyfappy_source(source_path):
    if not source_path or not await aiopath.exists(source_path):
        return False
    try:
        if await aiopath.isdir(source_path):
            LOGGER.info("Preserving batch folder source on failure: %s", source_path)
            return False
        await aioremove(source_path)
        return True
    except Exception as e:
        LOGGER.warning(f"Failed to remove source {source_path}: {e}")
        return False


async def _resume_happyfappy_artifacts(state, source_path):
    source_name = ospath.basename(source_path)
    stem = ospath.splitext(source_name)[0]
    output_dir = str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    title = str(state.get("title") or source_name)
    title_clean = ospath.splitext(title)[0]

    thumb = str(state.get("thumb") or "")
    if not thumb or not await aiopath.isfile(thumb):
        thumb_candidates = [
            ospath.join(output_dir, f"{title_clean}.preview.webp"),
            ospath.join(output_dir, f"{stem}.preview.webp"),
            ospath.join(ospath.dirname(source_path), f"{title_clean}.hstream-cover.jpg"),
            ospath.join(ospath.dirname(source_path), f"{stem}.hstream-cover.jpg"),
        ]
        for c in thumb_candidates:
            if await aiopath.isfile(c):
                thumb = c
                break

    sheet = str(state.get("sheet") or "")
    if not sheet or not await aiopath.isfile(sheet):
        sheet_candidates = [
            ospath.join(output_dir, f"{title_clean}_12screens.jpg"),
            ospath.join(output_dir, f"{stem}_12screens.jpg"),
        ]
        for c in sheet_candidates:
            if await aiopath.isfile(c):
                sheet = c
                break

    desc_path = str(state.get("description_path") or "")
    if not desc_path or not await aiopath.isfile(desc_path):
        desc_candidates = [
            ospath.join(output_dir, f"{title_clean}.description.txt"),
            ospath.join(output_dir, f"{stem}.description.txt"),
        ]
        for c in desc_candidates:
            if await aiopath.isfile(c):
                desc_path = c
                break

    hstream_metadata = _read_hstream_metadata(source_path)
    summary = await _probe_summary(source_path)
    title = hstream_metadata.get("title") or title_clean
    summary["title"] = title
    genres = [
        str(v).strip()
        for v in hstream_metadata.get("genres", [])
        if str(v).strip() and "rape" not in str(v).casefold()
    ]
    if genres:
        summary["genres"] = genres
    if hstream_metadata.get("year"):
        summary["year"] = str(hstream_metadata["year"])

    artifacts = {
        "title": title,
        "summary": summary,
        "thumb": thumb,
        "sheet": sheet,
        "desc_path": desc_path,
    }
    for key in ("thumb", "sheet", "desc_path"):
        if not artifacts[key] or not await aiopath.isfile(artifacts[key]):
            raise RuntimeError(f"Cannot resume HappyFappy transaction; missing {key}: {artifacts[key]}")

    async with aiopen(artifacts["desc_path"], encoding="utf-8") as desc_file:
        description = await desc_file.read()
    image_match = re.search(r"https://[^\s\[\]()]+\.pixhost\.cc/[^\s\[\]()]+", description, re.I)
    if image_match:
        artifacts["resume"] = True
        artifacts["resume_image_url"] = image_match.group(0).rstrip(".,")
    else:
        artifacts["resume"] = False
    return artifacts


async def _publish_happyfappy(message, source_path, torrent_path, artifacts, status_msg=None, skip_dupe=False):
    required = ("HAPPYFAPPY_ANNOUNCE_URL", "HAPPYFAPPY_USERNAME", "HAPPYFAPPY_PASSWORD")
    missing = [key for key in required if not str(getattr(Config, key, "") or "").strip()]
    if missing:
        raise RuntimeError(f"HappyFappy is not configured: {', '.join(missing)}")
    if not artifacts["thumb"] or not artifacts["sheet"]:
        raise RuntimeError("Pixhost upload requires both a thumbnail and contact sheet")
    if status_msg and not artifacts.get("resume"):
        await _edit_progress(status_msg, "Create Torrent: uploading cover and 12-image contact sheet to Pixhost...")
    if artifacts.get("resume"):
        image_url = artifacts["resume_image_url"]
        direct_cover = re.sub(
            r"^https://t(\d*)\.((?:pixhost\.(?:cc|to))|pixho\.st)/thumbs/",
            r"https://img\1.\2/images/",
            image_url,
        )
        links = {
            "thumbnail": direct_cover,
            "thumbnail_page": image_url,
            "contact_sheet": image_url,
            "contact_sheet_page": image_url,
        }
    else:
        update_transaction(torrent_path, "TORRENT_CREATED", source_path=source_path)
        cover = await upload_image_details(artifacts["thumb"])
        contact_sheet = await upload_image_details(artifacts["sheet"])
        links = {
            "thumbnail": cover.get("direct") or cover["thumbnail"],
            "thumbnail_page": cover["page"],
            "contact_sheet": contact_sheet["thumbnail"],
            "contact_sheet_page": contact_sheet["page"],
        }
    tags = _happyfappy_tags(artifacts["title"], artifacts["summary"])
    if len(tags.split()) < 5:
        for extra in ("hentai", "anime", "pron", "video", "hd"):
            if extra not in tags.split():
                tags += f" {extra}"
            if len(tags.split()) >= 5:
                break
    if len(tags.split()) < 5:
        raise RuntimeError("HappyFappy needs at least five meaningful tags; set HAPPYFAPPY_TAGS")
    if not artifacts.get("resume"):
        artifacts["desc_path"] = await _write_description(
            source_path, artifacts["title"], artifacts["summary"], links, "happyfappy"
        )
    await send_file(message, artifacts["desc_path"], "Final HappyFappy BBCode with Pixhost image links")
    if not artifacts.get("resume"):
        update_transaction(torrent_path, "ARTWORK_READY", description_path=artifacts["desc_path"])
    client = HappyFappyClient(
        getattr(Config, "HAPPYFAPPY_URL", "https://www.happyfappy.net"),
        Config.HAPPYFAPPY_USERNAME,
        Config.HAPPYFAPPY_PASSWORD,
        getattr(Config, "HAPPYFAPPY_COOKIE", "") or getattr(Config, "HAPPYFAPPY_COOKIE_FILE", ""),
    )
    try:
        skip_configured = bool(getattr(Config, "HAPPYFAPPY_SKIP_DUPE", True))
        if status_msg:
            await _edit_progress(
                status_msg,
                "Create Torrent: checking HappyFappy login..."
                if skip_configured
                else "Create Torrent: checking HappyFappy login and duplicates...",
            )
        await client.login()
        if skip_configured or skip_dupe:
            LOGGER.warning("Owner-configured HappyFappy dupe check skip for %s", ospath.basename(torrent_path))
        else:
            positive, _ = await client.check_dupe(torrent_path)
            if positive:
                source_removed = False
                if getattr(Config, "HAPPYFAPPY_DELETE_SOURCE_ON_FAILURE", False):
                    source_removed = await _remove_failed_happyfappy_source(source_path)
                await send_message(
                    message,
                    "HappyFappy dupe check found a possible match. Upload stopped."
                    + (" Original source deleted." if source_removed else ""),
                )
                return None
        async with aiopen(artifacts["desc_path"], encoding="utf-8") as desc_file:
            description = await desc_file.read()
        if status_msg:
            await _edit_progress(status_msg, "Create Torrent: submitting approved release to HappyFappy...")
        upload_url = await client.submit(
            torrent_path,
            artifacts["title"],
            tags,
            # HappyFappy's cover field must receive the direct image URL.
            # The Pixhost show/page URL is suitable for BBCode links, but it
            # is not rendered as the cover image by HappyFappy.
            links["thumbnail"],
            description,
            getattr(Config, "HAPPYFAPPY_CATEGORY", ""),
            bool(getattr(Config, "HAPPYFAPPY_ANONYMOUS", False)),
        )
        artifacts["happyfappy_accepted"] = True
        update_transaction(
            torrent_path,
            "HAPPYFAPPY_ACCEPTED",
            upload_url=upload_url,
            torrent_id=client.upload_details.get("torrent_id", ""),
            download_url=client.upload_details.get("download_url", ""),
            warnings=client.upload_details.get("warnings", []),
            validation_errors=client.upload_details.get("validation_errors", []),
        )
        if status_msg:
            await _edit_progress(status_msg, "Create Torrent: downloading the accepted HappyFappy torrent...")
        accepted_torrent_path = f"{torrent_path}.accepted.torrent"
        try:
            await client.download_uploaded_torrent(accepted_torrent_path)
        except HappyFappyError as error:
            LOGGER.warning(
                "HappyFappy official torrent download failed (%s); directly seeding with prepared local torrent",
                error,
            )
            await sync_to_async(copy2, torrent_path, accepted_torrent_path)
    finally:
        await client.close()
    try:
        validation = validate_accepted_torrent(
            accepted_torrent_path,
            source_path,
            getattr(Config, "HAPPYFAPPY_ANNOUNCE_URL", ""),
        )
    except (TorrentValidationError, OSError) as error:
        if accepted_torrent_path != torrent_path and await aiopath.isfile(torrent_path):
            LOGGER.warning(
                "Accepted torrent validation failed (%s); falling back to validated local torrent",
                error,
            )
            await sync_to_async(copy2, torrent_path, accepted_torrent_path)
            validation = validate_accepted_torrent(
                accepted_torrent_path,
                source_path,
                getattr(Config, "HAPPYFAPPY_ANNOUNCE_URL", ""),
            )
        else:
            raise RuntimeError(f"Accepted HappyFappy torrent validation failed: {error}") from error
    update_transaction(
        torrent_path,
        "ACCEPTED_TORRENT_READY",
        accepted_torrent_path=accepted_torrent_path,
        **validation,
    )
    torrent_hash = await _seed_with_qbit(
        accepted_torrent_path,
        source_path,
        status_msg,
        transaction_torrent_path=torrent_path,
    )
    await send_message(message, f"HappyFappy accepted the torrent: <code>{escape(upload_url)}</code>\nSeeding started: <code>{torrent_hash}</code>")
    return upload_url


async def _send_folder_artifacts(message, folder_path, torrent_path, trackers):
    screenshots_dir = ospath.join(folder_path, "Screenshots")
    await makedirs(screenshots_dir, exist_ok=True)
    videos = []
    for name in sorted(await listdir(folder_path)):
        path = ospath.join(folder_path, name)
        if await aiopath.isfile(path) and ospath.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
            videos.append(path)

    for video in videos:
        title, _, _ = format_clean_poster_title(ospath.basename(video))
        title = title or ospath.splitext(ospath.basename(video))[0]
        summary = await _probe_summary(video)
        sheet = await _create_contact_sheet_with_header(
            video, summary, title, screenshots_dir
        )
        if sheet:
            await send_file(
                message,
                sheet,
                f"Contact sheet: <code>{escape(ospath.basename(video))}</code>",
            )

    caption = (
        "<b>Created Folder Torrent</b>\n"
        f"Folder: <code>{escape(ospath.basename(folder_path))}</code>\n"
        f"Files: <code>{len(videos)}</code>\n"
        f"Torrent Path: <code>{escape(torrent_path)}</code>\n"
        f"Trackers: <code>{len(trackers)}</code>\n"
        f"Mode: <code>{'Private' if Config.CTORRENT_PRIVATE else 'Public'}</code>"
    )
    await send_file(message, torrent_path, caption)


def _extract_links_from_message(msg):
    if not msg:
        return []
    text = str(getattr(msg, "text", "") or getattr(msg, "caption", "") or "")
    return [
        token.strip("(),;\"'<>")
        for token in text.split()
        if token.strip("(),;\"'<>").startswith(("http://", "https://"))
    ]


def _pack_transaction_file(pack_name, output_dir=None):
    output_dir = output_dir or str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    safe = _safe_name(pack_name, "pack")
    return ospath.join(output_dir, f"{safe}.pack_transaction.json")


def load_pack_transaction(pack_name, output_dir=None):
    txn_file = _pack_transaction_file(pack_name, output_dir)
    try:
        if ospath.isfile(txn_file):
            with open(txn_file, "r", encoding="utf-8") as f:
                return load(f)
    except Exception as e:
        LOGGER.warning("Could not read pack transaction %s: %s", txn_file, e)
    return {}


def update_pack_transaction(pack_name, stage, output_dir=None, **kwargs):
    txn_file = _pack_transaction_file(pack_name, output_dir)
    try:
        data = load_pack_transaction(pack_name, output_dir)
        data["pack_name"] = pack_name
        data["stage"] = stage
        data["updated_at"] = time()
        for k, v in kwargs.items():
            data[k] = v
        with open(txn_file, "w", encoding="utf-8") as f:
            dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        LOGGER.warning("Could not write pack transaction %s: %s", txn_file, e)


async def _download_pack_link(link, out_dir, status_msg, message, item_index=1, total_items=1):
    parsed = urlsplit(link)
    netloc = parsed.netloc.lower().split(":", 1)[0]
    if netloc in {"hstream.moe", "www.hstream.moe"}:
        from ..helper.ext_utils.hstream_resolver import HstreamCatalogItem, HstreamResolver
        slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if slug and parsed.path.rstrip("/").split("/")[-2:-1] == ["hentai"]:
            async with HstreamResolver() as resolver:
                episode = await resolver.resolve(HstreamCatalogItem(slug, 0, slug))
            if episode.streams and any(stream.urls for stream in episode.streams):
                stream = episode.streams[0]
                output = await _unique_path(out_dir, _safe_name(episode.title, "hstream_release") + ".mkv")
                if await aiopath.exists(output) and (await aiopath.getsize(output)) > 1024 * 1024:
                    LOGGER.info("Pack video already exists, skipping re-download: %s", output)
                    return output
                if status_msg:
                    await _edit_progress(
                        status_msg,
                        f"Create Torrent: downloading pack video {item_index}/{total_items}...",
                        extra=f"Title: <code>{escape(episode.title)}</code>\nQuality: <code>{escape(stream.label)}</code>",
                    )
                process = await create_subprocess_exec(
                    "yt-dlp", "--newline", "--no-playlist", "--no-check-certificates",
                    "--referer", link, "--merge-output-format", "mkv",
                    "-f", "bv*+ba/b", "-o", output, stream.urls[0],
                    stdout=PIPE, stderr=STDOUT,
                )
                await process.communicate()
                if await aiopath.exists(output) and (await aiopath.getsize(output)) > 0:
                    metadata_path = f"{output}.hstream.json"
                    with open(metadata_path, "w", encoding="utf-8") as metadata_file:
                        dump({
                            "title": episode.title,
                            "year": episode.year,
                            "description": episode.description,
                            "genres": episode.genres,
                            "portrait_url": episode.portrait_url,
                            "landscape_url": episode.landscape_url,
                            "sample_urls": episode.sample_urls,
                            "source_url": episode.source_url,
                        }, metadata_file, ensure_ascii=False)
                    return output
    return await _download_link(link, out_dir, status_msg)


async def _collect_hstream_pack_auto(message, folder_path, folder_name, status_msg, min_size_bytes, reverse=False):
    from ..helper.ext_utils.db_handler import database
    from ..helper.ext_utils.hstream_resolver import HstreamResolver, select_hstream_pack_stream

    current_size = await get_path_size(folder_path)
    if current_size >= min_size_bytes:
        LOGGER.info("Pack '%s' already meets minimum size requirement: %d >= %d bytes", folder_name, current_size, min_size_bytes)
        return True, []

    await makedirs(folder_path, exist_ok=True)
    packaged_slugs = await database.get_packaged_hstream_slugs()
    LOGGER.info("MongoDB reported %d previously packaged Hstream slugs", len(packaged_slugs))

    async with HstreamResolver() as resolver:
        catalog_items = await resolver.discover_all(reverse=reverse)
        LOGGER.info("Fetched %d Hstream catalog items (reverse=%s)", len(catalog_items), reverse)

        candidates = [item for item in catalog_items if item.slug not in packaged_slugs]
        LOGGER.info("Unpackaged candidate episodes available: %d", len(candidates))

        collected_episodes = []
        for index, item in enumerate(candidates, start=1):
            if current_size >= min_size_bytes:
                LOGGER.info("Pack '%s' reached target size %d >= %d bytes", folder_name, current_size, min_size_bytes)
                break

            await _edit_progress(
                status_msg,
                f"Create Torrent: resolving episode {index} for {folder_name}...",
                extra=f"🎬 <code>{escape(item.series_title)} (Ep {item.episode})</code>\n"
                      f"📊 Pack Size: <code>{get_readable_file_size(current_size)} / {get_readable_file_size(min_size_bytes)}</code>",
            )

            try:
                episode = await resolver.resolve(item)
            except Exception as res_err:
                LOGGER.warning("Could not resolve episode %s: %s", item.slug, res_err)
                continue

            stream = select_hstream_pack_stream(episode.streams)
            if not stream or not stream.urls:
                LOGGER.warning("No streams available for %s", item.slug)
                continue

            fps_str = "48fps" if ("48fps" in stream.label or stream.fps >= 40) else ""
            media_tag = " ".join(v for v in (stream.resolution, stream.codec, fps_str) if v)
            safe_title = _safe_name(f"{episode.title} [{media_tag}] [Jap] ESub ~ [@Anime_Starfall🥰].mkv")
            output_file = ospath.join(folder_path, safe_title)

            if await aiopath.isfile(output_file) and (await aiopath.getsize(output_file)) > 0:
                LOGGER.info("Episode already downloaded, reusing: %s", output_file)
            else:
                await _edit_progress(
                    status_msg,
                    f"Create Torrent: downloading episode for {folder_name}...",
                    extra=f"🎬 <code>{escape(safe_title)}</code>\n"
                          f"🎞 Quality: <code>{escape(stream.label or stream.resolution)}</code>\n"
                          f"📊 Pack Size: <code>{get_readable_file_size(current_size)} / {get_readable_file_size(min_size_bytes)}</code>",
                )
                stream_url = stream.urls[0]
                cmd = [
                    "yt-dlp", "--newline", "--no-playlist", "--no-check-certificates",
                    "--progress-template", "download:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(progress._total_bytes_str)s",
                    "--referer", item.url, "--merge-output-format", "mkv",
                    "-f", "bv*+ba/b", "-o", output_file,
                ]
                if ospath.isfile("cookies.txt"):
                    cmd.extend(("--cookies", "cookies.txt"))
                cmd.append(stream_url)
                process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=STDOUT)
                last_update = 0
                while line := await process.stdout.readline():
                    text = line.decode(errors="ignore").strip()
                    if not text.startswith("download:"):
                        continue
                    fields = text.removeprefix("download:").split("|", 3)
                    if len(fields) != 4 or time() - last_update < 5:
                        continue
                    percent, speed, eta, total = fields
                    last_update = time()
                    await _edit_progress(
                        status_msg,
                        f"Create Torrent: downloading episode for {folder_name}...",
                        extra=f"🎬 <code>{escape(safe_title)}</code>\n"
                              f"🎞 Quality: <code>{escape(stream.label or stream.resolution)}</code>\n"
                              f"📊 Pack Size: <code>{get_readable_file_size(current_size)} / {get_readable_file_size(min_size_bytes)}</code>\n"
                              f"⏬ Progress: <code>{escape(percent)}</code> | {escape(speed)} | ETA: {escape(eta)}",
                    )
                await process.wait()
                if not (await aiopath.isfile(output_file)) or (await aiopath.getsize(output_file)) == 0:
                    LOGGER.warning("yt-dlp download failed for %s", item.slug)
                    continue

            meta_path = f"{output_file}.hstream.json"
            try:
                with open(meta_path, "w", encoding="utf-8") as mf:
                    dump({
                        "slug": item.slug,
                        "title": episode.title,
                        "year": episode.year,
                        "description": episode.description,
                        "genres": episode.genres,
                        "portrait_url": episode.portrait_url,
                        "landscape_url": episode.landscape_url,
                        "sample_urls": episode.sample_urls,
                        "source_url": episode.source_url,
                        "stream_label": stream.label,
                        "resolution": stream.resolution,
                        "fps": stream.fps,
                    }, mf, ensure_ascii=False)
            except Exception as meta_err:
                LOGGER.warning("Could not write episode metadata: %s", meta_err)

            ep_size = await aiopath.getsize(output_file)
            collected_episodes.append({
                "slug": item.slug,
                "title": episode.title,
                "stream_label": stream.label,
                "file_path": output_file,
                "size_bytes": ep_size,
            })
            current_size = await get_path_size(folder_path)

    return current_size >= min_size_bytes, collected_episodes


async def _collect_pack_videos_interactive(message, folder_path, folder_name, status_msg, min_size_bytes):
    current_size = await get_path_size(folder_path)
    if current_size >= min_size_bytes:
        LOGGER.info("Pack '%s' meets minimum size requirement: %d >= %d bytes", folder_name, current_size, min_size_bytes)
        return True

    LOGGER.info("Pack '%s' under minimum threshold (%d < %d bytes); launching interactive collector", folder_name, current_size, min_size_bytes)
    token = f"hfp_{message.id}_{int(time() * 1000)}"
    buttons = ButtonMaker()
    buttons.data_button("✅ Done / Process Pack", f"{token}:done")
    buttons.data_button("❌ Cancel", f"{token}:cancel")

    def _format_prompt(c_size):
        c_gb = c_size / (1024**3)
        target_gb = min_size_bytes / (1024**3)
        missing_gb = max(0.0, target_gb - c_gb)
        try:
            from os import listdir as os_listdir
            videos = [
                f for f in os_listdir(folder_path)
                if ospath.splitext(f)[1].lower() in VIDEO_EXTENSIONS
            ]
        except Exception:
            videos = []
        return (
            f"⚠️ <b>HappyFappy Pack: Under {target_gb:.1f} GB Minimum Threshold</b>\n\n"
            f"📁 <b>Pack Folder:</b> <code>{escape(folder_name)}</code>\n"
            f"📊 <b>Current Size:</b> <code>{c_gb:.2f} GiB</code> / <code>{target_gb:.2f} GiB</code>\n"
            f"⏳ <b>Missing:</b> <code>{missing_gb:.2f} GiB</code>\n"
            f"🎬 <b>Videos in Pack:</b> <code>{len(videos)}</code>\n\n"
            f"👉 <i>Send more video links or Telegram video files to add them to this pack.</i>\n"
            f"Or click <b>Done</b> to proceed with the current size anyway."
        )

    prompt = await send_message(message, _format_prompt(current_size), buttons.build_menu(2))
    event = Event()
    result = [True]

    async def callback(_, query):
        if query.from_user.id != message.from_user.id or not query.data.startswith(f"{token}:"):
            return
        await query.answer()
        action = query.data.split(":", 1)[1]
        LOGGER.info("Interactive collector action clicked: '%s' for pack '%s'", action, folder_name)
        result[0] = (action != "cancel")
        event.set()

    async def incoming_handler(_, incoming):
        if incoming.from_user is None or incoming.from_user.id != message.from_user.id:
            return
        incoming_text = str(getattr(incoming, "text", "") or getattr(incoming, "caption", "") or "").strip()
        new_links = [w.strip("(),;\"'<>") for w in incoming_text.split() if w.strip("(),;\"'<>").startswith(("http://", "https://"))]
        has_media = bool(getattr(incoming, "video", None) or getattr(incoming, "document", None))
        if not new_links and not has_media:
            return
        LOGGER.info("Interactive collector received item: %d links, media=%s for pack '%s'", len(new_links), has_media, folder_name)

        if new_links:
            for l in new_links:
                try:
                    await _download_pack_link(l, folder_path, status_msg, incoming)
                except Exception as dl_err:
                    LOGGER.warning("Pack link download failed: %s", dl_err)
        elif has_media:
            try:
                await _download_reply(incoming, folder_path, status_msg)
            except Exception as dl_err:
                LOGGER.warning("Pack telegram media download failed: %s", dl_err)

        nonlocal current_size
        current_size = await get_path_size(folder_path)
        try:
            extra_notice = "\n\n🎉 <b>30GB+ minimum reached! Click Done to upload.</b>" if current_size >= min_size_bytes else ""
            await edit_message(prompt, _format_prompt(current_size) + extra_notice, buttons.build_menu(2))
        except Exception:
            pass

    cb_handler = TgClient.bot.add_handler(
        CallbackQueryHandler(callback, filters=create(lambda _, __, update: getattr(update, "data", "").startswith(token)))
    )
    msg_handler = TgClient.bot.add_handler(
        MessageHandler(incoming_handler, filters=create(lambda _, __, msg: getattr(msg, "chat", None) and msg.chat.id == message.chat.id))
    )

    try:
        await wait_for(event.wait(), timeout=900)
    except TimeoutError:
        result[0] = True
    finally:
        try:
            TgClient.bot.remove_handler(*cb_handler)
            TgClient.bot.remove_handler(*msg_handler)
            await delete_message(prompt)
        except Exception:
            pass

    return result[0]


async def _generate_pack_artifacts(folder_path, folder_name, status_msg=None):
    output_dir = str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    await makedirs(output_dir, exist_ok=True)
    screenshots_dir = ospath.join(folder_path, "Screenshots")
    await makedirs(screenshots_dir, exist_ok=True)

    videos = []
    for item in sorted(await listdir(folder_path)):
        p = ospath.join(folder_path, item)
        if await aiopath.isfile(p) and ospath.splitext(item)[1].lower() in VIDEO_EXTENSIONS:
            videos.append(p)

    if not videos:
        raise RuntimeError(f"No video files found in pack folder: {folder_path}")

    # 1. 10s animated WebP pack cover from first video
    cover_path = ospath.join(output_dir, f"{_safe_name(folder_name, 'cover')}.preview.webp")
    if not await aiopath.isfile(cover_path):
        if status_msg:
            await _edit_progress(status_msg, "Create Torrent: generating 10-second animated pack cover (.webp)...")
        cover_path = await _create_preview_webp(videos[0], folder_name)
    cover_details = await upload_image_details(cover_path)

    # 2. Generate contact sheets for ALL videos into Screenshots/
    if status_msg:
        await _edit_progress(status_msg, f"Create Torrent: generating contact sheets for {len(videos)} scenes in Screenshots/...")

    all_sheets = []
    combined_genres = set()
    combined_descriptions = []

    for v in videos:
        v_stem = ospath.splitext(ospath.basename(v))[0]
        sheet_path = ospath.join(screenshots_dir, f"{v_stem}_12screens.jpg")
        meta = _read_hstream_metadata(v)
        if meta.get("genres"):
            combined_genres.update(meta["genres"])
        if meta.get("description"):
            combined_descriptions.append(f"{meta.get('title', v_stem)}: {meta['description']}")

        if not await aiopath.isfile(sheet_path):
            summary = await _probe_summary(v)
            v_title, _, _ = format_clean_poster_title(ospath.basename(v))
            v_title = v_title or v_stem
            if meta.get("sample_urls"):
                sheet_path = await _create_hstream_contact_sheet(meta, v_title, screenshots_dir)
            else:
                sheet_path = await _create_contact_sheet_with_header(v, summary, v_title, screenshots_dir)
        all_sheets.append((v, sheet_path))

    # 3. Upload representative contact sheets to Pixhost for BBCode presentation
    max_preview = int(getattr(Config, "HAPPYFAPPY_PACK_MAX_PREVIEW_SHEETS", 15))
    preview_items = all_sheets[:max_preview]
    if status_msg:
        await _edit_progress(status_msg, f"Create Torrent: uploading {len(preview_items)} preview contact sheets to Pixhost...")

    spoilers_list = []
    for idx, (v, s_path) in enumerate(preview_items, 1):
        try:
            s_details = await upload_image_details(s_path)
            v_sum = await _probe_summary(v)
            v_title, _, _ = format_clean_poster_title(ospath.basename(v))
            v_title = v_title or ospath.splitext(ospath.basename(v))[0]
            res = f"{v_sum.get('width', '')}x{v_sum.get('height', '')}" if v_sum.get('width') else "HD"
            dur = v_sum.get("duration", "N/A")
            sz = get_readable_file_size(await aiopath.getsize(v))
            spoilers_list.append(
                f"[spoiler=Scene {idx:02d}: {v_title} ({res}, {dur}, {sz})]\n"
                f"[center][url={s_details['page']}][img]{s_details['thumbnail']}[/img][/url][/center]\n"
                f"[/spoiler]"
            )
        except Exception as up_err:
            LOGGER.warning("Failed to upload preview contact sheet %s: %s", s_path, up_err)

    # 4. Generate BBCode description
    total_size = await get_path_size(folder_path)
    clean_genres = [g for g in combined_genres if "rape" not in g.casefold()]
    pack_desc = (
        f"{folder_name} is a comprehensive video pack containing {len(videos)} scenes.\n"
        + ("\n".join(combined_descriptions[:5]) if combined_descriptions else "")
    )
    tech_specs = f"Total Size: {get_readable_file_size(total_size)}\nScene Count: {len(videos)} videos\nFormats: MKV/MP4\nVideo Codec: H.264 / AVC\nAudio: AAC / 2.0 Channels"

    template = await _load_bbcode_template("happyfappy_pack")
    rendered = template
    values = {
        "title": folder_name,
        "description": pack_desc,
        "total_size": get_readable_file_size(total_size),
        "files_count": str(len(videos)),
        "genres": ", ".join(clean_genres) or "Pack, Hentai, Anime",
        "media_info": tech_specs,
        "thumbnail_link": cover_details.get("direct") or cover_details["thumbnail"],
        "thumbnail_page": cover_details["page"],
        "spoilers": "\n\n".join(spoilers_list),
    }
    for k, v in values.items():
        rendered = rendered.replace("{" + k + "}", str(v or ""))

    desc_path = ospath.join(output_dir, f"{_safe_name(folder_name, 'pack')}.description.txt")
    async with aiopen(desc_path, "w", encoding="utf-8") as df:
        await df.write(rendered.rstrip() + "\n")

    return {
        "cover_path": cover_path,
        "cover_details": cover_details,
        "desc_path": desc_path,
        "total_size": total_size,
        "videos": videos,
        "genres": clean_genres,
    }


async def _update_seeding_channel_pinned_index(chan_id):
    from ..helper.ext_utils.db_handler import database
    packs = await database.get_all_completed_hstream_packs()
    if not packs:
        return

    clean_chan = str(chan_id).removeprefix("-100")
    total_bytes = sum(p.get("total_size_bytes", 0) for p in packs)
    total_episodes = sum(p.get("episodes_count", 0) for p in packs)

    lines = [
        "📚 <b>HAPPYFAPPY MEGAPACKS CATALOG</b>",
        "──────────────────────────────",
    ]
    for p in packs:
        p_name = p.get("pack_name", "Pack")
        p_size = p.get("readable_size", "")
        p_eps = p.get("episodes_count", 0)
        p_url = p.get("upload_url", "")
        post_id = p.get("channel_message_id")
        post_link = f"https://t.me/c/{clean_chan}/{post_id}" if post_id else ""
        link_str = f" ➔ <a href=\"{post_link}\">View Post</a>" if post_link else ""
        lines.append(f"📁 <b>{escape(p_name)}</b> • {p_size} ({p_eps} eps){link_str}")
        if p_url:
            lines.append(f"   └ 🌐 <a href=\"{escape(p_url)}\">Tracker Release</a>")
    lines.append("──────────────────────────────")
    lines.append(f"📊 <b>Total Seeded:</b> <code>{get_readable_file_size(total_bytes)}</code> | <b>Total Packs:</b> <code>{len(packs)}</code>")
    lines.append(f"🎬 <b>Total Episodes:</b> <code>{total_episodes} videos</code>")
    lines.append("🌱 <i>All packs 24/7 seeded via qBittorrent</i>")

    index_text = "\n".join(lines)
    index_id = await database.get_hstream_pinned_index_id()

    try:
        if index_id:
            try:
                await TgClient.bot.edit_message_text(chan_id, index_id, index_text, disable_web_page_preview=True)
                return
            except Exception:
                pass

        new_index = await TgClient.bot.send_message(chan_id, index_text, disable_web_page_preview=True)
        if new_index:
            try:
                await TgClient.bot.pin_chat_message(chan_id, new_index.id, disable_notification=True)
            except Exception as pin_err:
                LOGGER.warning("Could not pin index message in channel: %s", pin_err)
            await database.set_hstream_pinned_index_id(new_index.id)
    except Exception as e:
        LOGGER.warning("Failed to manage seeding channel pinned index: %s", e)


async def _handle_happyfappy_pack(message, folder_name, links, status_msg=None, skip_dupe=False):
    required = ("HAPPYFAPPY_ANNOUNCE_URL", "HAPPYFAPPY_USERNAME", "HAPPYFAPPY_PASSWORD")
    missing = [key for key in required if not str(getattr(Config, key, "") or "").strip()]
    if missing:
        raise RuntimeError(f"HappyFappy is not configured: {', '.join(missing)}")

    is_hstream_auto = False
    pack_number = None
    fn_lower = folder_name.lower().strip()
    if fn_lower == "auto":
        from ..helper.ext_utils.db_handler import database
        pack_number = await database.get_next_hstream_pack_number()
        folder_name = f"Hanime Pack - {pack_number}"
        is_hstream_auto = True
    elif "hanime pack" in fn_lower:
        is_hstream_auto = True
        num_match = re.search(r"hanime\s*pack\s*-\s*(\d+)", folder_name, re.IGNORECASE)
        if num_match:
            pack_number = int(num_match.group(1))
        else:
            from ..helper.ext_utils.db_handler import database
            pack_number = await database.get_next_hstream_pack_number()
            folder_name = f"Hanime Pack - {pack_number}"

    storage_dir = str(Config.CTORRENT_STORAGE_DIR or "/usr/src/app/torrents/seeding")
    folder_path = ospath.join(storage_dir, folder_name)
    await makedirs(folder_path, exist_ok=True)
    output_dir = str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output")
    await makedirs(output_dir, exist_ok=True)

    if not links and message.reply_to_message:
        links = _extract_links_from_message(message.reply_to_message)

    LOGGER.info("Starting HappyFappy pack: folder='%s', initial_links=%d, auto_hstream=%s", folder_name, len(links), is_hstream_auto)

    auto_episodes = []
    min_gb = float(getattr(Config, "HAPPYFAPPY_PACK_MIN_SIZE_GB", 30.0))
    min_bytes = int(min_gb * (1024 ** 3))

    if is_hstream_auto and not links:
        proceed, auto_episodes = await _collect_hstream_pack_auto(
            message, folder_path, folder_name, status_msg, min_bytes, reverse=False
        )
        if not proceed:
            await edit_message(status_msg, f"Create Torrent HappyFappy Pack: auto-collection stopped. Files preserved in <code>{escape(folder_name)}</code>.")
            return None
    else:
        if links:
            for index, link in enumerate(links, start=1):
                await _edit_progress(
                    status_msg,
                    f"Create Torrent: downloading pack video {index}/{len(links)}...",
                    extra=f"Folder: <code>{escape(folder_name)}</code>",
                )
                try:
                    await _download_pack_link(link, folder_path, status_msg, message, index, len(links))
                except Exception as dl_err:
                    LOGGER.warning("Pack link %s download failed: %s", link, dl_err)

        proceed = await _collect_pack_videos_interactive(message, folder_path, folder_name, status_msg, min_bytes)
        if not proceed:
            await edit_message(status_msg, f"Create Torrent HappyFappy Pack: stopped by owner. Files preserved in <code>{escape(folder_name)}</code>.")
            return None

    update_pack_transaction(folder_name, "GENERATING_ARTIFACTS", output_dir=output_dir, folder_path=folder_path)
    artifacts = await _generate_pack_artifacts(folder_path, folder_name, status_msg)
    update_pack_transaction(folder_name, "ARTWORK_READY", output_dir=output_dir, desc_path=artifacts["desc_path"])

    await send_file(message, artifacts["desc_path"], f"HappyFappy Megapack BBCode: {escape(folder_name)}")
    if artifacts["cover_path"] and await aiopath.isfile(artifacts["cover_path"]):
        await send_file(message, artifacts["cover_path"], "10-second animated preview pack cover (.webp)")

    if status_msg:
        await _edit_progress(status_msg, "Create Torrent: creating private torrent for pack...")
    announce = str(getattr(Config, "HAPPYFAPPY_ANNOUNCE_URL", "") or "").strip()
    torrent_path, trackers = await _make_torrent(
        folder_path,
        private=True,
        trackers=[announce],
        piece_length=getattr(Config, "CTORRENT_PIECE_LENGTH", 23),
        source_tag="HF",
    )
    update_pack_transaction(folder_name, "TORRENT_CREATED", output_dir=output_dir, torrent_path=torrent_path)

    client = HappyFappyClient(
        getattr(Config, "HAPPYFAPPY_URL", "https://www.happyfappy.net"),
        Config.HAPPYFAPPY_USERNAME,
        Config.HAPPYFAPPY_PASSWORD,
        getattr(Config, "HAPPYFAPPY_COOKIE", "") or getattr(Config, "HAPPYFAPPY_COOKIE_FILE", ""),
    )
    skip_configured = bool(getattr(Config, "HAPPYFAPPY_SKIP_DUPE", True))
    if status_msg:
        await _edit_progress(
            status_msg,
            "Create Torrent: checking HappyFappy login..."
            if skip_configured
            else "Create Torrent: checking HappyFappy login and pack duplicates...",
        )
    await client.login()
    if not (skip_configured or skip_dupe):
        positive, _ = await client.check_dupe(torrent_path)
        if positive:
            await send_message(message, "HappyFappy dupe check found a match. Batch folder preserved.")
            return None

    async with aiopen(artifacts["desc_path"], encoding="utf-8") as desc_file:
        description = await desc_file.read()
    if status_msg:
        await _edit_progress(status_msg, "Create Torrent: submitting approved pack to HappyFappy...")

    tags = _happyfappy_tags(folder_name, {"genres": artifacts["genres"]})
    if len(tags.split()) < 5:
        for extra in ("hentai", "anime", "pack", "megapack", "video", "hd", "pron"):
            if extra not in tags.split():
                tags += f" {extra}"
            if len(tags.split()) >= 5:
                break

    cover_direct_url = artifacts["cover_details"].get("direct") or artifacts["cover_details"]["thumbnail"]
    upload_url = await client.submit(
        torrent_path,
        folder_name,
        tags,
        cover_direct_url,
        description,
        getattr(Config, "HAPPYFAPPY_CATEGORY", "1"),
        bool(getattr(Config, "HAPPYFAPPY_ANONYMOUS", False)),
    )
    update_pack_transaction(folder_name, "HAPPYFAPPY_ACCEPTED", output_dir=output_dir, upload_url=upload_url)

    if status_msg:
        await _edit_progress(status_msg, "Create Torrent: downloading accepted torrent and initiating qBittorrent seeding...")
    accepted_torrent_path = f"{torrent_path}.accepted.torrent"
    try:
        await client.download_uploaded_torrent(accepted_torrent_path)
    except HappyFappyError as error:
        LOGGER.warning("HappyFappy official torrent download failed (%s); seeding with local torrent", error)
        await sync_to_async(copy2, torrent_path, accepted_torrent_path)

    torrent_hash = await _seed_with_qbit(
        accepted_torrent_path,
        folder_path,
        status_msg,
        transaction_torrent_path=torrent_path,
    )
    update_pack_transaction(folder_name, "SEEDING", output_dir=output_dir, torrent_hash=torrent_hash)

    completion_text = (
        f"✅ <b>HappyFappy Pack Uploaded & Seeding</b>\n\n"
        f"📁 <b>Pack:</b> <code>{escape(folder_name)}</code>\n"
        f"🎬 <b>Videos:</b> <code>{len(artifacts['videos'])}</code>\n"
        f"📊 <b>Total Size:</b> <code>{get_readable_file_size(artifacts['total_size'])}</code>\n"
        f"🌐 <b>Tracker Release:</b> <a href=\"{escape(upload_url)}\">Open on HappyFappy</a>\n"
        f"🌱 <b>qBittorrent Seeding:</b> <code>{torrent_hash}</code>"
    )
    await send_message(message, completion_text)
    await send_file(message, accepted_torrent_path, f"Official HappyFappy Torrent: {escape(folder_name)}")

    pack_doc = None
    if is_hstream_auto:
        from ..helper.ext_utils.db_handler import database
        pack_doc = {
            "_id": folder_name,
            "pack_number": pack_number or 1,
            "pack_name": folder_name,
            "status": "COMPLETED",
            "total_size_bytes": artifacts["total_size"],
            "readable_size": get_readable_file_size(artifacts["total_size"]),
            "episodes_count": len(artifacts["videos"]),
            "episodes": auto_episodes or [
                {"slug": ospath.basename(v), "title": ospath.basename(v), "size_bytes": 0}
                for v in artifacts["videos"]
            ],
            "torrent_hash": torrent_hash,
            "upload_url": upload_url,
            "completed_at": int(time()),
        }
        await database.save_hstream_pack(pack_doc)

    seeding_channel = str(getattr(Config, "HAPPYFAPPY_SEEDING_CHANNEL", "") or "").strip()
    if seeding_channel:
        try:
            chan_id = int(seeding_channel) if seeding_channel.lstrip("-").isdigit() else seeding_channel

            ep_lines = []
            for i, v in enumerate(artifacts["videos"][:35], start=1):
                clean_t = ospath.splitext(ospath.basename(v))[0]
                ep_lines.append(f"{i}. {escape(clean_t)}")
            if len(artifacts["videos"]) > 35:
                ep_lines.append(f"... and {len(artifacts['videos']) - 35} more videos")
            ep_block = "\n".join(ep_lines)

            tag_pack = f"Pack{pack_number}" if pack_number else "Pack"
            channel_caption = (
                f"📦 <b>#HanimePack #{tag_pack} #HappyFappy</b>\n\n"
                f"📁 <b>Pack:</b> <code>{escape(folder_name)}</code>\n"
                f"📊 <b>Total Size:</b> <code>{get_readable_file_size(artifacts['total_size'])}</code>\n"
                f"🎬 <b>Episodes:</b> <code>{len(artifacts['videos'])} videos</code>\n"
                f"🌱 <b>qBittorrent Seeding:</b> <code>{torrent_hash}</code>\n\n"
                f"<blockquote expandable><b>Episodes in this pack:</b>\n{ep_block}</blockquote>\n\n"
                f"🌐 <a href=\"{escape(upload_url)}\"><b>Open on HappyFappy Tracker</b></a>"
            )

            btn = ButtonMaker()
            btn.url_button("🌐 Open on HappyFappy", upload_url)
            webui = _qbit_webui_url()
            if webui:
                btn.url_button("🌱 Web UI", webui)

            post_msg = None
            if artifacts["cover_path"] and await aiopath.isfile(artifacts["cover_path"]):
                post_msg = await TgClient.bot.send_photo(chan_id, artifacts["cover_path"], caption=channel_caption, reply_markup=btn.build_menu(2))
            else:
                post_msg = await TgClient.bot.send_message(chan_id, channel_caption, reply_markup=btn.build_menu(2))

            reply_to_id = post_msg.id if post_msg else None
            await TgClient.bot.send_document(chan_id, accepted_torrent_path, caption=f"Torrent: {escape(folder_name)}", reply_to_message_id=reply_to_id)
            if artifacts["desc_path"] and await aiopath.isfile(artifacts["desc_path"]):
                await TgClient.bot.send_document(chan_id, artifacts["desc_path"], caption=f"BBCode: {escape(folder_name)}", reply_to_message_id=reply_to_id)

            if pack_doc and post_msg:
                from ..helper.ext_utils.db_handler import database
                pack_doc["channel_message_id"] = post_msg.id
                await database.save_hstream_pack(pack_doc)

            await _update_seeding_channel_pinned_index(chan_id)
        except Exception as chan_err:
            LOGGER.warning("Failed to forward pack to HAPPYFAPPY_SEEDING_CHANNEL (%s): %s", seeding_channel, chan_err)

    return upload_url


@new_task
async def create_torrent(_, message):
    command_text = message.text or message.caption or ""
    uid = getattr(message.from_user, "id", "unknown")
    LOGGER.info("Create Torrent command received from user %s: %s", uid, command_text)
    status_msg = await send_message(message, "Create Torrent: preparing source...")
    source_path = None
    happyfappy_mode = False
    artifacts = {}
    try:
        command_tokens = set(command_text.split())
        happyfappy_mode = "--happyfappy" in command_tokens
        skip_dupe = "--skip-dupe" in command_tokens
        if happyfappy_mode and message.from_user.id != Config.OWNER_ID:
            LOGGER.warning("HappyFappy rejected: user %s is not OWNER_ID %s", uid, Config.OWNER_ID)
            await edit_message(status_msg, "HappyFappy publishing is owner-only.")
            return
        folder_mode = _parse_folder_mode(message)
        if folder_mode:
            folder_name, links = folder_mode
            LOGGER.info("Create Torrent folder mode: name='%s', links_count=%d, happyfappy=%s", folder_name, len(links), happyfappy_mode)
            if happyfappy_mode:
                return await _handle_happyfappy_pack(
                    message, folder_name, links, status_msg=status_msg, skip_dupe=skip_dupe
                )
            if not links:
                await edit_message(
                    status_msg,
                    "Folder mode needs links: <code>/ctorrent 'Folder Name' - link1 link2</code>",
                )
                return
            storage_dir = str(Config.CTORRENT_STORAGE_DIR or "/usr/src/app/torrents/seeding")
            folder_path = ospath.join(storage_dir, folder_name)
            await makedirs(folder_path, exist_ok=True)
            for index, link in enumerate(links, start=1):
                await _edit_progress(
                    status_msg,
                    f"Create Torrent: downloading folder source {index}/{len(links)}...",
                    extra=f"Folder: <code>{escape(folder_name)}</code>",
                )
                await _download_link(link, folder_path, status_msg)
            await _edit_progress(
                status_msg,
                "Create Torrent: generating folder contact sheets and torrent...",
                extra=f"Folder: <code>{escape(folder_name)}</code>",
            )
            torrent_path, trackers = await _make_torrent(folder_path)
            await _send_folder_artifacts(message, folder_path, torrent_path, trackers)
            await edit_message(status_msg, "Create Torrent folder mode: completed.")
            return

        if happyfappy_mode:
            resume_state = find_transaction_by_source_url(
                str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output"),
                command_text,
            )
            if resume_state:
                source_path = str(resume_state.get("source_path") or "")
                torrent_path = str(resume_state.get("torrent_path") or "")
                if not source_path or not torrent_path:
                    raise RuntimeError("HappyFappy resume transaction is missing source or torrent path")
                if not await aiopath.isfile(source_path) or not await aiopath.isfile(torrent_path):
                    raise RuntimeError("HappyFappy resume transaction source or local torrent is missing")
                source_size = await aiopath.getsize(source_path)
                if int(resume_state.get("source_size", -1)) != source_size:
                    raise RuntimeError("HappyFappy resume source size no longer matches the transaction")
                if resume_state.get("source_hash") != source_fingerprint(source_path):
                    raise RuntimeError("HappyFappy resume source hash no longer matches the transaction")
                artifacts = await _resume_happyfappy_artifacts(resume_state, source_path)
                await _edit_progress(status_msg, "Create Torrent: resuming preserved HappyFappy transaction...")
                upload_url = await _publish_happyfappy(
                    message,
                    source_path,
                    torrent_path,
                    artifacts,
                    status_msg=status_msg,
                    skip_dupe=skip_dupe,
                )
                await edit_message(status_msg, "Create Torrent HappyFappy resume: completed.")
                return

        source_path = await _resolve_source(message, status_msg, hstream_only=happyfappy_mode)
        if not source_path:
            await edit_message(
                status_msg,
                "Reply to a Telegram video/file or send <code>/ctorrent direct_link</code>.",
            )
            return
        source_size = await aiopath.getsize(source_path)
        source_hash = source_fingerprint(source_path)
        if happyfappy_mode:
            duplicate = find_duplicate_transaction(
                str(Config.CTORRENT_OUTPUT_DIR or "/usr/src/app/torrents/output"),
                command_text,
                source_size,
                source_hash,
                ospath.basename(source_path),
            )
            if duplicate:
                raise RuntimeError(
                    "An unfinished HappyFappy transaction already exists for this source: "
                    f"{duplicate.get('torrent_path', 'unknown')}"
                )
        await _edit_progress(
            status_msg,
            "Create Torrent: generating thumbnail, contact sheet, and torrent...",
            extra=f"Source: <code>{escape(ospath.basename(source_path))}</code>",
        )
        if happyfappy_mode:
            announce = str(getattr(Config, "HAPPYFAPPY_ANNOUNCE_URL", "") or "").strip()
            if not announce:
                raise RuntimeError("HAPPYFAPPY_ANNOUNCE_URL is required for a private torrent")
            torrent_path, trackers = await _make_torrent(
                source_path,
                private=True,
                trackers=[announce],
                piece_length=getattr(Config, "CTORRENT_PIECE_LENGTH", 23),
                source_tag="HF",
            )
            update_transaction(
                torrent_path,
                "DOWNLOADED",
                source_path=source_path,
                source_url=command_text,
                source_size=source_size,
                source_hash=source_hash,
                title=ospath.basename(source_path),
            )
            artifacts = await _send_artifacts(
                message,
                source_path,
                torrent_path,
                trackers,
                send=False,
                template_preset="happyfappy",
            )
            update_transaction(
                torrent_path,
                "ARTWORK_READY",
                description_path=artifacts.get("desc_path", ""),
                thumb=artifacts.get("thumb", ""),
                sheet=artifacts.get("sheet", ""),
                title=artifacts.get("title", ""),
            )
            upload_url = await _publish_happyfappy(
                message,
                source_path,
                torrent_path,
                artifacts,
                status_msg=status_msg,
                skip_dupe=skip_dupe,
            )
            await edit_message(
                status_msg,
                "Create Torrent HappyFappy mode: completed."
                if upload_url
                else "Create Torrent HappyFappy mode: stopped after dupe review.",
            )
        else:
            torrent_path, trackers = await _make_torrent(source_path)
            await _send_artifacts(message, source_path, torrent_path, trackers)
            await edit_message(status_msg, "Create Torrent: completed.")
    except Exception as e:
        LOGGER.error("Create Torrent execution failed: %s", e, exc_info=True)
        source_removed = False
        if (
            happyfappy_mode
            and not artifacts.get("happyfappy_accepted")
            and getattr(Config, "HAPPYFAPPY_DELETE_SOURCE_ON_FAILURE", False)
        ):
            source_removed = await _remove_failed_happyfappy_source(source_path)
        suffix = "\nOriginal source deleted after failure." if source_removed else ""
        await edit_message(status_msg, f"Create Torrent failed:\n<code>{escape(str(e))}</code>{suffix}")
