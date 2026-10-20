from asyncio import sleep
from json import dumps, loads
from os import path as ospath
from typing import Optional

from .. import LOGGER, user_data
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.hindianime_resolver import (
    HindiAnimeCandidate,
    HindiAnimeResolver,
)
from ..helper.telegram_helper.message_utils import edit_message, send_message
from .rss import _parse_chat_value, _start_rss_download


def _seen_items() -> set[str]:
    """Retrieves seen deduplication keys from Config/database."""
    try:
        raw = getattr(Config, "HINDIANIME_SEEN_ITEMS", "") or "[]"
        data = loads(str(raw))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


async def _save_seen(seen: set[str]):
    """Persists seen keys to Config and MongoDB."""
    value = dumps(sorted(seen)[-3000:], separators=(",", ":"))
    Config.set("HINDIANIME_SEEN_ITEMS", value)
    try:
        await database.update_config({"HINDIANIME_SEEN_ITEMS": value})
    except Exception as err:
        LOGGER.warning(f"HindiAnimeZone: failed to persist seen items to DB: {err}")


def _prepare_user_settings(user_id: int):
    """Configures user settings for Hindi Anime Zone auto leech workflow.

    - AUTO_POSTER_ENABLED = True
    - POST_TEMPLATE_ID = 8 (Anime poster template)
    - AUTO_REMOVE_STREAMS = True (strips all non-Tamil audio tracks)
    - AUTO_KEEP_AUDIO_LANGS = 'Tamil' (strictly preserves Tamil audio)
    - AUTO_UNZIP = True, AUTO_MERGE = True (merges zip/batch packs)
    - Prepend [S{season}E{episode}] to autorename template
    """
    if user_id not in user_data:
        seed_path = "configs/user_settings_8814651149.json"
        if ospath.isfile(seed_path):
            try:
                with open(seed_path, "r", encoding="utf-8") as f:
                    data = loads(f.read())
                    user_data[user_id] = data.get("settings", {})
            except Exception as e:
                LOGGER.warning(f"HindiAnimeZone: failed loading seed user settings: {e}")
        if user_id not in user_data:
            user_data[user_id] = {}

    settings = user_data[user_id]

    # Audio stream removal settings: strictly keep Tamil only
    settings["AUTO_REMOVE_STREAMS"] = True
    settings["AUTO_KEEP_AUDIO_LANGS"] = "Tamil"

    # Zip batch unpacking & smart merging
    settings["AUTO_UNZIP"] = True
    settings["AUTO_MERGE"] = True

    # Poster & branding settings
    settings["AUTO_POSTER_ENABLED"] = True
    settings["POST_TEMPLATE_ID"] = 11
    settings["AUTO_POSTER_USE_AS_THUMBNAIL"] = True
    settings["AUTO_POSTER_SEND_MESSAGE"] = False

    # Auto Thumbnail & Template 11 Thumbnail
    settings["AUTO_THUMBNAIL"] = True
    settings["THUMBNAIL_MODE"] = "automatic"
    settings["FORCE_ANIME_THUMBNAIL"] = False

    # Auto Processing & Intro Subtitles (5min gap, 10sec show across full video length)
    settings["AUTO_PROCESS"] = True
    settings["AUTO_INTRO_SUBTITLE"] = True
    settings["INTRO_SUBTITLE_TEXT"] = "[@Anime_Starfall🥰]"
    settings["INTRO_SUBTITLE_DURATION"] = 10
    settings["INTRO_SUBTITLE_RANGES"] = ""

    # Source metadata removal and custom metadata [@Anime_Starfall🥰]
    settings["AUTO_METADATA"] = True
    settings["METADATA_TITLE"] = "[@Anime_Starfall🥰]"
    settings["METADATA"] = {
        "title": "[@Anime_Starfall🥰]",
        "comment": "[@Anime_Starfall🥰]",
        "author": "[@Anime_Starfall🥰]",
        "artist": "[@Anime_Starfall🥰]",
        "encoded_by": "[@Anime_Starfall🥰]",
    }
    settings["VIDEO_METADATA"] = {
        "title": "[@Anime_Starfall🥰]",
        "comment": "[@Anime_Starfall🥰]",
    }
    settings["AUDIO_METADATA"] = {
        "title": "[@Anime_Starfall🥰] - {audiolang}",
    }
    settings["SUBTITLE_METADATA"] = {
        "title": "[@Anime_Starfall🥰] - {sublang}",
    }

    settings["lremname_auto"] = "🄰🅂- [S{season}E{episode}] {name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~ [@Anime_Starfall🥰]"
    settings["LEECH_PREFIX"] = ""
    settings["LEECH_SUFFIX"] = ""


def _get_target_channel() -> str:
    """Returns the configured anime target channel ID."""
    val = (
        Config.HINDIANIME_CHANNEL_ID
        or getattr(Config, "ANIME_CHANNEL_ID", 0)
        or getattr(Config, "HAZ_CHANNEL_ID", 0)
        or -1004330385098
    )
    return str(val).strip() if val else ""


async def queue_hindianime_candidates(
    candidates: list[HindiAnimeCandidate],
    owner_id: Optional[int] = None,
    seen: Optional[set[str]] = None,
) -> int:
    """Queues eligible anime candidates into WZML-X leech engine with audio filtering and channel routing."""
    seen = seen if seen is not None else _seen_items()
    user_id = int(owner_id or Config.HINDIANIME_USER_ID or 8814651149)
    dest_chat = _get_target_channel()
    if not dest_chat or dest_chat == "0":
        LOGGER.warning("HindiAnimeZone: target channel is not configured in config.py")
        return 0

    chat_id, topic_id = _parse_chat_value(dest_chat)
    if not chat_id:
        LOGGER.warning(f"HindiAnimeZone: invalid chat ID: {dest_chat}")
        return 0

    staging_dest = Config.LEECH_DUMP_CHAT or -1003887117402
    staging_chat_id, staging_topic_id = _parse_chat_value(str(staging_dest))

    queued = 0
    for item in candidates:
        if not item.gdflix_url:
            continue
        if item.quality and "1080p" not in item.quality.lower():
            continue
        key = item.dedupe_key
        if key in seen:
            continue

        _prepare_user_settings(user_id)

        LOGGER.info(
            f"HindiAnimeZone: queuing '{item.file_title}' ({item.quality}) -> "
            f"Anime Channel ({dest_chat}) via: {item.gdflix_url} (staging: {staging_chat_id})"
        )

        try:
            await _start_rss_download(
                url=item.gdflix_url,
                command="leech",
                user_id=user_id,
                rss_chat_id=staging_chat_id or chat_id,
                rss_topic_id=staging_topic_id if staging_chat_id else topic_id,
                item_title=item.file_title,
                auto_leech=True,
                rename_mode="title",
                leech_by="bot",
                rss_upload_chat=dest_chat,
            )
            seen.add(key)
            queued += 1
            await sleep(1.5)
        except Exception as error:
            LOGGER.error(f"HindiAnimeZone: failed to queue '{item.file_title}': {error}", exc_info=True)

    if queued:
        await _save_seen(seen)

    return queued


async def hindianime_monitor():
    """Daily monitor job scheduled to run once a day."""
    if not getattr(Config, "HINDIANIME_AUTO_LEECH", False):
        return

    from ..helper.ext_utils.hstream_maintenance import hstream_maintenance

    if hstream_maintenance.pauses_feeds():
        LOGGER.info("HindiAnimeZone monitor paused for owner maintenance.")
        return

    LOGGER.info("HindiAnimeZone: running daily scheduled crawl...")
    solver_url = getattr(Config, "HINDIANIME_SOLVER_URL", "http://flaresolverr:8191/v1")
    try:
        candidates = await HindiAnimeResolver.get_tamil_candidates(max_pages=2, solver_url=solver_url)
        if candidates:
            queued = await queue_hindianime_candidates(candidates)
            LOGGER.info(f"HindiAnimeZone daily monitor queued {queued} new anime release(s).")
    except Exception as err:
        LOGGER.error(f"HindiAnimeZone daily monitor failed: {err}", exc_info=True)


@new_task
async def hindianime_handler(_, message):
    """Telegram command handler for /anime or /haz."""
    parts = (message.text or "").split()
    subcmd = parts[1].lower() if len(parts) > 1 else "status"

    if subcmd == "check":
        pages = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        notice = await send_message(message, f"🔎 <b>HindiAnimeZone:</b> Crawling 1080p Tamil anime releases (pages: {pages})...")
        solver_url = getattr(Config, "HINDIANIME_SOLVER_URL", "http://flaresolverr:8191/v1")
        try:
            candidates = await HindiAnimeResolver.get_tamil_candidates(max_pages=pages, solver_url=solver_url, quality_filter="1080p")
            if not candidates:
                await edit_message(notice, "No 1080p Tamil audio anime releases found on HindiAnimeZone.")
                return
            queued = await queue_hindianime_candidates(candidates, owner_id=message.from_user.id)
            await edit_message(
                notice,
                f"✅ <b>HindiAnimeZone:</b> Found {len(candidates)} 1080p candidate(s), queued <b>{queued}</b> release(s) to Anime channel.",
            )
        except Exception as err:
            LOGGER.error(f"HindiAnimeZone check failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>HindiAnimeZone check error:</b> <code>{err}</code>")

    elif subcmd in ["backlog", "sync", "start"]:
        days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 60
        notice = await send_message(message, f"🔎 <b>HindiAnimeZone:</b> Crawling last {days} days 1080p Tamil releases...")
        solver_url = getattr(Config, "HINDIANIME_SOLVER_URL", "http://flaresolverr:8191/v1")
        try:
            candidates = await HindiAnimeResolver.get_tamil_candidates(max_pages=5, solver_url=solver_url, days=days, quality_filter="1080p")
            if not candidates:
                await edit_message(notice, f"No 1080p Tamil anime releases found in the last {days} days.")
                return
            queued = await queue_hindianime_candidates(candidates, owner_id=message.from_user.id)
            await edit_message(
                notice,
                f"✅ <b>HindiAnimeZone:</b> Backlog crawl complete! Found {len(candidates)} 1080p release(s), queued <b>{queued}</b> new release(s) to Anime channel.",
            )
        except Exception as err:
            LOGGER.error(f"HindiAnimeZone backlog failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>HindiAnimeZone backlog error:</b> <code>{err}</code>")

    elif subcmd == "status":
        ch = _get_target_channel()
        auto = getattr(Config, "HINDIANIME_AUTO_LEECH", False)
        seen = _seen_items()
        solver = getattr(Config, "HINDIANIME_SOLVER_URL", "http://flaresolverr:8192/solve")
        text = (
            "🎌 <b>Hindi Anime Zone Auto Leech Status:</b>\n\n"
            f"<b>Auto Leech:</b> <code>{'Enabled' if auto else 'Disabled'}</code>\n"
            f"<b>Target Channel:</b> <code>{ch}</code>\n"
            f"<b>Check Interval:</b> <code>{getattr(Config, 'HINDIANIME_CHECK_INTERVAL_HOURS', 24)}h (Once a day)</code>\n"
            f"<b>Solver Sidecar:</b> <code>{solver}</code>\n"
            f"<b>Audio Filter:</b> <code>Remove non-Tamil streams (Tamil only preserved)</code>\n"
            f"<b>Quality Filter:</b> <code>All available qualities (1080p HQ, 1080p x265, 720p, etc.)</code>\n"
            f"<b>Zip Handling:</b> <code>Auto Unzip & Smart Merge enabled</code>\n"
            f"<b>Workflow User ID:</b> <code>{getattr(Config, 'HINDIANIME_USER_ID', 8814651149)}</code>\n"
            f"<b>Seen Releases:</b> <code>{len(seen)} cached</code>\n\n"
            "<b>Usage:</b>\n"
            "• <code>/anime check [pages]</code> - Check & leech now\n"
            "• <code>/anime status</code> - View current status\n"
            "• <code>/anime clear</code> - Clear seen cache"
        )
        await send_message(message, text)

    elif subcmd == "clear":
        await _save_seen(set())
        await send_message(message, "🗑️ <b>Hindi Anime Zone seen items cache cleared.</b>")

    else:
        await send_message(
            message,
            "<b>Hindi Anime Zone Commands:</b>\n"
            "• <code>/anime check [pages]</code> - Check and leech immediately\n"
            "• <code>/anime status</code> - View configuration status\n"
            "• <code>/anime clear</code> - Reset deduplication seen items",
        )


def start_hindianime_scheduler():
    """Initializes and schedules the daily Hindi Anime Zone monitor if enabled or configured."""
    has_channel = bool(
        Config.HINDIANIME_CHANNEL_ID
        or getattr(Config, "ANIME_CHANNEL_ID", 0)
        or getattr(Config, "HAZ_CHANNEL_ID", 0)
    )
    if not (getattr(Config, "HINDIANIME_AUTO_LEECH", False) or has_channel):
        return

    try:
        from apscheduler.triggers.interval import IntervalTrigger
        from .. import scheduler

        hours = int(getattr(Config, "HINDIANIME_CHECK_INTERVAL_HOURS", 24) or 24)
        if scheduler.get_job("hindianime") is None:
            scheduler.add_job(
                hindianime_monitor,
                trigger=IntervalTrigger(hours=hours),
                id="hindianime",
                name="Hindi Anime Zone Daily Leech",
                misfire_grace_time=600,
                max_instances=1,
                replace_existing=True,
            )
            LOGGER.info(f"HindiAnimeZone daily scheduler registered (every {hours} hours).")
            if not scheduler.running:
                scheduler.start()
    except Exception as err:
        LOGGER.warning(f"Could not register HindiAnimeZone daily scheduler: {err}")
