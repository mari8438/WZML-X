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


def _get_anime_task_settings(user_id: int, is_season_pack: bool = False) -> dict:
    """Returns anime-specific task settings without polluting global user_data."""
    seed_settings = {}
    seed_path = "configs/user_settings_8814651149.json"
    if ospath.isfile(seed_path):
        try:
            with open(seed_path, "r", encoding="utf-8") as f:
                data = loads(f.read())
                seed_settings = data.get("settings", {})
        except Exception:
            pass
    settings = dict(user_data.get(user_id) or seed_settings)

    # Audio stream removal settings: strictly keep Tamil only
    settings["AUTO_REMOVE_STREAMS"] = True
    settings["AUTO_KEEP_AUDIO_LANGS"] = "Tamil"

    # Zip batch unpacking & smart merging
    settings["AUTO_UNZIP"] = True
    settings["AUTO_MERGE"] = True

    # AutoRename settings: strictly enforce user template
    settings["AUTORENAME"] = True
    settings["RENAME_METHOD"] = "auto"
    settings["lremname_auto"] = "🄰🅂- [S{season}E{episode}] {name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~ [@Anime_Starfall🥰]"
    settings["LEECH_PREFIX"] = ""
    settings["LEECH_SUFFIX"] = ""

    # Differentiated poster & thumbnail rules
    if is_season_pack:
        settings["AUTO_POSTER_ENABLED"] = True
        settings["AUTO_POSTER_SEND_MESSAGE"] = True
        settings["AUTO_POSTER_USE_AS_THUMBNAIL"] = False
        settings["AUTO_THUMBNAIL"] = True
        settings["THUMBNAIL_MODE"] = "automatic"
        settings["FORCE_ANIME_THUMBNAIL"] = False
        settings["POST_TEMPLATE_ID"] = 11
    else:
        settings["AUTO_POSTER_ENABLED"] = True
        settings["AUTO_POSTER_SEND_MESSAGE"] = False
        settings["AUTO_POSTER_USE_AS_THUMBNAIL"] = True
        settings["POST_TEMPLATE_ID"] = 11
        settings["AUTO_THUMBNAIL"] = True
        settings["THUMBNAIL_MODE"] = "automatic"
        settings["FORCE_ANIME_THUMBNAIL"] = True

    # Auto Processing & Intro Subtitles
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
    return settings


def _prepare_user_settings(user_id: int, is_season_pack: bool = False):
    """Configures user settings for Hindi Anime Zone auto leech workflow (test compatibility)."""
    settings = _get_anime_task_settings(user_id, is_season_pack)
    user_data[user_id] = settings
    return settings


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
    target_chat: Optional[str] = None,
) -> int:
    """Queues eligible anime candidates into WZML-X leech engine with audio filtering and channel routing."""
    seen = seen if seen is not None else _seen_items()
    user_id = int(owner_id or Config.HINDIANIME_USER_ID or 8814651149)
    dest_chat = target_chat or _get_target_channel()
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

        task_settings = _get_anime_task_settings(user_id, is_season_pack=item.is_season_pack)

        LOGGER.info(
            f"HindiAnimeZone: queuing '{item.file_title}' ({item.quality}, zip={item.is_season_pack}) -> "
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
                rename_mode="none",
                leech_by="bot",
                rss_upload_chat=dest_chat,
                is_season_pack=item.is_season_pack,
                task_user_dict=task_settings,
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
    solver_url = getattr(Config, "HINDIANIME_SOLVER_URL", "http://flaresolverr:8192/solve")
    try:
        seen = _seen_items()
        candidates = await HindiAnimeResolver.get_tamil_candidates(max_pages=2, solver_url=solver_url, seen=seen)
        if candidates:
            queued = await queue_hindianime_candidates(candidates, seen=seen)
            LOGGER.info(f"HindiAnimeZone daily monitor queued {queued} new anime release(s).")
    except Exception as err:
        LOGGER.error(f"HindiAnimeZone daily monitor failed: {err}", exc_info=True)


@new_task
async def hindianime_handler(_, message):
    """Telegram command handler for /anime or /haz."""
    parts = (message.text or "").split()
    if len(parts) < 2:
        text = (
            "🎌 <b>Hindi Anime Zone Commands:</b>\n"
            "• <code>/haz URL [-up CHAT_ID|TOPIC_ID]</code> - Leech specific anime post URL\n"
            "• <code>/haz query [-up CHAT_ID|TOPIC_ID]</code> - Search anime title & leech 1080p Tamil\n"
            "• <code>/haz check [pages] [-up CHAT_ID]</code> - Check latest releases & leech\n"
            "• <code>/haz backlog [days] [-up CHAT_ID]</code> - Check recent days releases\n"
            "• <code>/haz status</code> - View current configuration status\n"
            "• <code>/haz clear</code> - Reset deduplication seen items cache"
        )
        await send_message(message, text)
        return

    dest_chat = _get_target_channel()
    clean_parts = []
    i = 1
    while i < len(parts):
        if parts[i] == "-up":
            if i + 1 < len(parts):
                dest_chat = parts[i + 1].strip()
            i += 2
            continue
        clean_parts.append(parts[i])
        i += 1

    if not clean_parts:
        subcmd = "status"
    else:
        subcmd = clean_parts[0].lower()

    solver_url = getattr(Config, "HINDIANIME_SOLVER_URL", "http://flaresolverr:8192/solve")

    if clean_parts and (clean_parts[0].startswith("http://") or clean_parts[0].startswith("https://")):
        target_url = clean_parts[0]
        notice = await send_message(message, f"🔎 <b>HindiAnimeZone:</b> Resolving anime URL: <code>{target_url}</code>...")
        try:
            candidates = await HindiAnimeResolver.get_candidates_for_url(target_url, solver_url=solver_url, quality_filter=None)
            if not candidates:
                await edit_message(notice, f"❌ No downloadable media candidates found for: <code>{target_url}</code>")
                return
            queued = await queue_hindianime_candidates(candidates, owner_id=message.from_user.id, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>HindiAnimeZone:</b> Resolved {len(candidates)} candidate(s), queued <b>{queued}</b> release(s) to <code>{dest_chat}</code>.",
            )
        except Exception as err:
            LOGGER.error(f"HindiAnimeZone URL resolve failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>HindiAnimeZone error:</b> <code>{err}</code>")

    elif subcmd == "check":
        pages = int(clean_parts[1]) if len(clean_parts) > 1 and clean_parts[1].isdigit() else 1
        LOGGER.info(f"HindiAnimeZone: user {message.from_user.id} requested check (pages={pages})")
        notice = await send_message(message, f"🔎 <b>HindiAnimeZone:</b> Crawling 1080p Tamil anime releases (pages: {pages})...")
        try:
            seen = _seen_items()
            candidates = await HindiAnimeResolver.get_tamil_candidates(max_pages=pages, solver_url=solver_url, quality_filter="1080p", seen=seen)
            if not candidates:
                await edit_message(notice, "No new 1080p Tamil audio anime releases found on HindiAnimeZone.")
                return
            queued = await queue_hindianime_candidates(candidates, owner_id=message.from_user.id, seen=seen, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>HindiAnimeZone:</b> Found {len(candidates)} 1080p candidate(s), queued <b>{queued}</b> release(s) to Anime channel.",
            )
        except Exception as err:
            LOGGER.error(f"HindiAnimeZone check failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>HindiAnimeZone check error:</b> <code>{err}</code>")

    elif subcmd in ["backlog", "sync", "start"]:
        days = int(clean_parts[1]) if len(clean_parts) > 1 and clean_parts[1].isdigit() else 60
        LOGGER.info(f"HindiAnimeZone: user {message.from_user.id} requested backlog crawl (days={days})")
        notice = await send_message(message, f"🔎 <b>HindiAnimeZone:</b> Crawling last {days} days 1080p Tamil releases...")
        try:
            seen = _seen_items()
            candidates = await HindiAnimeResolver.get_tamil_candidates(max_pages=5, solver_url=solver_url, days=days, quality_filter="1080p", seen=seen)
            if not candidates:
                await edit_message(notice, f"No new 1080p Tamil anime releases found in the last {days} days.")
                return
            queued = await queue_hindianime_candidates(candidates, owner_id=message.from_user.id, seen=seen, target_chat=dest_chat)
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
            "🎌 <b>Hindi Anime Zone Leech Status:</b>\n\n"
            f"<b>Auto Leech (Background):</b> <code>{'Enabled' if auto else 'Disabled'}</code>\n"
            f"<b>Target Channel:</b> <code>{ch}</code>\n"
            f"<b>Solver Sidecar:</b> <code>{solver}</code>\n"
            f"<b>Audio Filter:</b> <code>Remove non-Tamil streams (Tamil only preserved)</code>\n"
            f"<b>Quality Filter:</b> <code>1080p HQ, 1080p x265, Batch Zip</code>\n"
            f"<b>Zip Handling:</b> <code>Auto Unzip & Smart Merge enabled</code>\n"
            f"<b>Seen Releases:</b> <code>{len(seen)} cached</code>\n\n"
            "<b>Interactive Usage:</b>\n"
            "• <code>/haz URL [-up CHAT_ID]</code> - Leech specific post\n"
            "• <code>/haz query [-up CHAT_ID]</code> - Search & leech\n"
            "• <code>/haz check [pages]</code> - Check latest releases\n"
            "• <code>/haz clear</code> - Clear seen cache"
        )
        await send_message(message, text)

    elif subcmd == "clear":
        await _save_seen(set())
        await send_message(message, "🗑️ <b>Hindi Anime Zone seen items cache cleared.</b>")

    else:
        # Search query on demand
        query = " ".join(clean_parts)
        LOGGER.info(f"HindiAnimeZone: user {message.from_user.id} requested search for: '{query}'")
        notice = await send_message(message, f"🔎 <b>HindiAnimeZone:</b> Searching for <code>{query}</code>...")
        try:
            seen = _seen_items()
            candidates = await HindiAnimeResolver.search_tamil_candidates(query, solver_url=solver_url, quality_filter=None, seen=seen)
            if not candidates:
                await edit_message(notice, f"❌ No matching releases found on HindiAnimeZone for: <code>{query}</code>")
                return
            queued = await queue_hindianime_candidates(candidates, owner_id=message.from_user.id, seen=seen, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>HindiAnimeZone:</b> Found {len(candidates)} match(es), queued <b>{queued}</b> release(s) to target channel (<code>{dest_chat}</code>).",
            )
        except Exception as err:
            LOGGER.error(f"HindiAnimeZone search failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>HindiAnimeZone search error:</b> <code>{err}</code>")


def start_hindianime_scheduler():
    """Initializes and schedules the daily Hindi Anime Zone monitor ONLY if explicitly enabled."""
    if not getattr(Config, "HINDIANIME_AUTO_LEECH", False):
        LOGGER.info("HindiAnimeZone background scheduler disabled (on-demand mode active).")
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
