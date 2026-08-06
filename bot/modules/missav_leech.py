from asyncio import Event, Semaphore, gather, wait_for
from contextlib import suppress
from html import escape
from os import path as ospath
from pathlib import Path
from re import sub
from shutil import rmtree

from pyrogram.errors import FloodWait

from .. import DOWNLOAD_DIR, LOGGER, user_data
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import sync_to_async
from ..helper.ext_utils.missav_resolver import MissAVResolver
from ..helper.mirror_leech_utils.download_utils.yt_dlp_download import YoutubeDLHelper
from ..helper.poster_engine.engine import render_poster_option
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import edit_message, send_message
from .batch_task_registry import BatchTaskController

_MINIMUM_DURATION = 60 * 60
_QUALITY_TIMEOUT = 45 * 60
_DOWNLOAD_WORKERS = 5
_UPLOAD_WORKERS = 5
_INVALID_FILENAME = r'[\\/:*?"<>|]'


def _safe_filename(value):
    value = sub(_INVALID_FILENAME, " ", str(value or ""))
    value = sub(r"\s+", " ", value).strip(" .-")
    return value[:170] or "MissAV"


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


def _parse_destination(message, tokens):
    destination = message.chat.id
    thread_id = message.message_thread_id if getattr(message, "is_topic_message", False) else None
    if "-up" not in tokens:
        if len(tokens) != 2:
            raise ValueError("Unexpected arguments; <code>/mll</code> supports one destination only")
        return destination, thread_id
    index = tokens.index("-up")
    if index + 1 >= len(tokens):
        raise ValueError("<code>-up</code> requires one CHAT_ID or CHAT_ID|TOPIC_ID")
    if index + 2 != len(tokens):
        raise ValueError("<code>/mll</code> supports one upload destination only")
    raw = tokens[index + 1]
    if "|" in raw:
        raw, topic = raw.rsplit("|", 1)
        if not topic.lstrip("-").isdigit():
            raise ValueError("Invalid topic id after <code>|</code>")
        thread_id = int(topic)
    destination = int(raw) if raw.lstrip("-").isdigit() else raw
    return destination, thread_id


def _caption(item):
    lines = [f"<b>{escape(item.title, quote=False)}</b>"]
    if item.date:
        lines.append(f"<b>Release:</b> {escape(item.date)}")
    if item.actors:
        lines.append(f"<b>Actors:</b> {escape(', '.join(item.actors), quote=False)}")
    if item.director:
        lines.append(f"<b>Director:</b> {escape(item.director, quote=False)}")
    lines.append(f"<b>Duration:</b> {item.duration // 3600}:{item.duration // 60 % 60:02d}:{item.duration % 60:02d}")
    if item.description:
        room = max(0, 1000 - len("\n".join(lines)))
        lines.append(escape(item.description[:room], quote=False))
    return "\n".join(lines)[:1024]


async def _telegram_call(method, **kwargs):
    for attempt in range(3):
        try:
            return await method(**kwargs)
        except FloodWait as error:
            if attempt == 2:
                raise
            from asyncio import sleep

            await sleep(max(1, int(error.value)))


async def _poster(item, root, owner_id):
    metadata = {
        "title": item.title,
        "description": item.description,
        "plot": item.description,
        "release_date": item.date,
        "year": item.date[:4] if item.date else "",
        "cast": ", ".join(item.actors),
        "director": item.director,
        "duration": str(item.duration),
        "landscape_url": item.thumbnail,
        "poster_url": item.thumbnail,
        "filename": item.title,
        "category": "movie",
        "brand": "MissAV",
    }
    generated = await render_poster_option(
        metadata,
        owner_id,
        user_dict=user_data.get(owner_id, {}),
        option="10",
    )
    output = ospath.join(root, "poster.jpg")
    await sync_to_async(Path(generated).replace, output)
    return output


class _DirectListener:
    def __init__(self, owner_id, cancel_event, quality_cancel_event=None):
        self.user_id = owner_id
        self.user_dict = user_data.get(owner_id, {})
        self.size = 0
        self._cancel_event = cancel_event
        self._quality_cancel_event = quality_cancel_event

    @property
    def is_cancelled(self):
        return self._cancel_event.is_set() or bool(
            self._quality_cancel_event and self._quality_cancel_event.is_set()
        )


async def _download_quality(item, quality, root, owner_id, cancel_event, slots):
    async with slots:
        if cancel_event.is_set():
            return None
        filename = f"{_safe_filename(item.title)} [{_safe_filename(quality.label)}].mkv"
        output = ospath.join(root, filename)
        quality_cancel_event = Event()
        helper = YoutubeDLHelper(
            _DirectListener(owner_id, cancel_event, quality_cancel_event)
        )
        headers = MissAVResolver.request_headers(item.url)
        try:
            return await wait_for(
                helper.download_direct_hls(quality.url, output, headers),
                timeout=_QUALITY_TIMEOUT,
            )
        except Exception as error:
            quality_cancel_event.set()
            LOGGER.error(f"MissAV quality skipped ({item.title} {quality.label}): {error}")
            with suppress(OSError):
                Path(output).unlink()
            return None


async def _upload_video(path, destination, thread_id, poster, cancel_event, slots):
    async with slots:
        if cancel_event.is_set():
            return 0
        kwargs = {
            "chat_id": destination,
            "video": path,
            "caption": f"<b>{escape(ospath.basename(path), quote=False)}</b>",
            "supports_streaming": True,
        }
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        if poster and ospath.isfile(poster):
            kwargs["thumb"] = poster
        try:
            await _telegram_call(TgClient.bot.send_video, **kwargs)
        except Exception as error:
            if "THUMB" not in str(error).upper():
                raise
            kwargs.pop("thumb", None)
            await _telegram_call(TgClient.bot.send_video, **kwargs)
        return 1


async def missav_leech(_, message):
    tokens = (message.text or "").split()
    if len(tokens) < 2:
        await send_message(
            message,
            "<b>Usage:</b> <code>/mll URL</code>, <code>/mll A</code>, or "
            "<code>/mll A-G [-up CHAT_ID|TOPIC_ID]</code>",
        )
        return
    try:
        letters = None if tokens[1].startswith("https://") else _parse_letter_range(tokens[1])
        destination, thread_id = _parse_destination(message, tokens)
        await TgClient.bot.get_chat(destination)
    except Exception as error:
        await send_message(message, f"<b>Invalid upload destination:</b> <code>{escape(str(error))}</code>")
        return

    controller = BatchTaskController("mll", message)
    cancel_event = Event()
    controller.register_cancel_callback(lambda _: cancel_event.set())
    root = ospath.join(DOWNLOAD_DIR, "missav", controller.gid)
    Path(root).mkdir(parents=True, exist_ok=True)
    status = await send_message(
        message,
        f"<b>MissAV resolving...</b>\nStop: <code>/{BotCommands.CancelTaskCommand[1]}_{controller.gid}</code>",
    )
    uploaded = skipped = failed = 0
    try:
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
            target = tokens[1].upper() if letters else tokens[1]
            raise RuntimeError(f"No public MissAV titles were found for {target}")
        await edit_message(status, f"<b>MissAV titles:</b> <code>{len(items)}</code>")
        for index, item in enumerate(items, start=1):
            if cancel_event.is_set():
                break
            if item.duration < _MINIMUM_DURATION:
                skipped += 1
                continue
            title_root = ospath.join(root, str(index))
            Path(title_root).mkdir(parents=True, exist_ok=True)
            try:
                poster = await _poster(item, title_root, controller.user_id)
                photo_kwargs = {
                    "chat_id": destination,
                    "photo": poster,
                    "caption": _caption(item),
                }
                if thread_id is not None:
                    photo_kwargs["message_thread_id"] = thread_id
                await _telegram_call(TgClient.bot.send_photo, **photo_kwargs)
                download_slots = Semaphore(_DOWNLOAD_WORKERS)
                downloads = await gather(
                    *(
                        _download_quality(item, quality, title_root, controller.user_id, cancel_event, download_slots)
                        for quality in item.qualities
                    )
                )
                videos = [path for path in downloads if path]
                if not videos:
                    raise RuntimeError("No public quality downloaded")
                upload_slots = Semaphore(_UPLOAD_WORKERS)
                results = await gather(
                    *(
                        _upload_video(path, destination, thread_id, poster, cancel_event, upload_slots)
                        for path in videos
                    ),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        failed += 1
                        LOGGER.error(f"MissAV upload failed: {result}")
                    else:
                        uploaded += result
            except Exception as error:
                failed += 1
                LOGGER.error(f"MissAV title failed ({item.url}): {error}", exc_info=True)
            finally:
                await sync_to_async(rmtree, title_root, ignore_errors=True)
            with suppress(Exception):
                status = await edit_message(
                    status,
                    f"<b>MissAV:</b> <code>{index}/{len(items)}</code>\n"
                    f"Uploaded: <code>{uploaded}</code> | Skipped under 1h: <code>{skipped}</code> | Failed: <code>{failed}</code>",
                )
        final = "cancelled" if cancel_event.is_set() else "completed"
        await edit_message(
            status,
            f"<b>MissAV {final}.</b>\nUploaded: <code>{uploaded}</code> | "
            f"Skipped under 1h: <code>{skipped}</code> | Failed: <code>{failed}</code>",
        )
    except Exception as error:
        LOGGER.error(f"MissAV run failed: {error}", exc_info=True)
        await edit_message(status, f"<b>MissAV failed:</b> <code>{escape(str(error)[:900])}</code>")
    finally:
        cancel_event.set()
        await sync_to_async(rmtree, root, ignore_errors=True)
        controller.close()
