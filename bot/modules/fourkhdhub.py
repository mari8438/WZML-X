from json import dumps, loads
from os import path as ospath
from typing import Optional

from .. import LOGGER, user_data
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.fourkhdhub_resolver import (
    FourKHDHubCandidate,
    FourKHDHubResolver,
)
from ..helper.telegram_helper.message_utils import edit_message, send_message
from .rss import _parse_chat_value, _start_rss_download


def _seen_items() -> set[str]:
    """Retrieves seen deduplication keys from Config/database."""
    try:
        raw = getattr(Config, "FOURKHDHUB_SEEN_ITEMS", "") or "[]"
        data = loads(str(raw))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


async def _save_seen(seen: set[str]):
    """Persists seen keys to Config and MongoDB."""
    value = dumps(sorted(seen)[-3000:], separators=(",", ":"))
    Config.set("FOURKHDHUB_SEEN_ITEMS", value)
    try:
        await database.update_config({"FOURKHDHUB_SEEN_ITEMS": value})
    except Exception as err:
        LOGGER.warning(f"4KHDHub: failed to persist seen items to DB: {err}")


def _get_fourkhd_task_settings(user_id: int, is_series: bool) -> dict:
    """Returns task-specific settings for 4KHDHub without mutating global user_data."""
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
    base_rename = (
        settings.get("lremname_auto")
        or getattr(Config, "LEECH_FILENAME_REMNAME_AUTO", "")
        or "{name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~"
    )

    clean_base = base_rename.replace("[S{season}E{episode}] ", "").replace("[S{season}E{episode}]", "").strip()

    if is_series:
        settings["AUTO_POSTER_ENABLED"] = True
        settings["lremname_auto"] = f"[S{{season}}E{{episode}}] {clean_base}"
    else:
        settings["lremname_auto"] = clean_base

    return settings


def _prepare_user_settings(user_id: int, is_series: bool):
    """Configures user settings for 4khdhub leech workflow (test compatibility)."""
    settings = _get_fourkhd_task_settings(user_id, is_series)
    user_data[user_id] = settings
    return settings


def _get_target_channel(is_series: bool) -> str:
    """Returns the configured channel/topic ID based on category."""
    if is_series:
        val = Config.FOURKHDHUB_WEBSERIES_CHANNEL_ID or getattr(Config, "HDHUB_WEBSERIES_CHANNEL_ID", 0)
    else:
        val = Config.FOURKHDHUB_HOLLYWOOD_CHANNEL_ID or getattr(Config, "HDHUB_HOLLYWOOD_CHANNEL_ID", 0)
    return str(val).strip() if val else ""


async def queue_fourkhd_candidates(
    candidates: list[FourKHDHubCandidate],
    owner_id: Optional[int] = None,
    seen: Optional[set[str]] = None,
    target_chat: Optional[str] = None,
) -> int:
    """Queues eligible candidates into WZML-X leech engine with proper channel routing and user settings."""
    seen = seen if seen is not None else _seen_items()
    user_id = int(owner_id or Config.FOURKHDHUB_USER_ID or 8814651149)
    queued = 0

    for item in candidates:
        if not item.hub_url:
            continue
        key = item.dedupe_key
        if key in seen:
            continue

        dest_chat = target_chat or _get_target_channel(item.is_series)
        if not dest_chat or dest_chat == "0":
            LOGGER.warning(
                f"4KHDHub: skipping '{item.file_title}' because destination channel for "
                f"{'Webseries' if item.is_series else 'Hollywood'} is not configured in config.py"
            )
            continue

        chat_id, topic_id = _parse_chat_value(dest_chat)
        if not chat_id:
            LOGGER.warning(f"4KHDHub: invalid chat ID: {dest_chat}")
            continue

        task_settings = _get_fourkhd_task_settings(user_id, item.is_series)

        LOGGER.info(
            f"4KHDHub: queuing '{item.file_title}' ({item.quality}) -> "
            f"{'Webseries' if item.is_series else 'Hollywood'} ({dest_chat})"
        )

        try:
            await _start_rss_download(
                url=item.hub_url,
                command="leech",
                user_id=user_id,
                rss_chat_id=chat_id,
                rss_topic_id=topic_id,
                item_title=item.file_title,
                auto_leech=True,
                rename_mode="title",
                leech_by="bot",
                rss_upload_chat=dest_chat,
                task_user_dict=task_settings,
            )
            seen.add(key)
            queued += 1
        except Exception as error:
            LOGGER.error(f"4KHDHub: failed to queue '{item.file_title}': {error}", exc_info=True)

    if queued:
        await _save_seen(seen)

    return queued


async def fourkhdhub_monitor():
    """Daily monitor job scheduled to run once a day."""
    if not getattr(Config, "FOURKHDHUB_AUTO_LEECH", False):
        return

    from ..helper.ext_utils.hstream_maintenance import hstream_maintenance

    if hstream_maintenance.pauses_feeds():
        LOGGER.info("4KHDHub monitor paused for owner maintenance.")
        return

    LOGGER.info("4KHDHub: running daily scheduled crawl...")
    try:
        candidates = await FourKHDHubResolver.get_tamil_candidates(max_pages=2)
        if candidates:
            queued = await queue_fourkhd_candidates(candidates)
            LOGGER.info(f"4KHDHub daily monitor queued {queued} new release(s).")
    except Exception as err:
        LOGGER.error(f"4KHDHub daily monitor failed: {err}", exc_info=True)


@new_task
async def fourkhdhub_handler(_, message):
    """Telegram command handler for /4khd or /fourkhd."""
    parts = (message.text or "").split()
    if len(parts) < 2:
        text = (
            "🎬 <b>4KHDHub Commands:</b>\n"
            "• <code>/4khd URL [-up CHAT_ID|TOPIC_ID]</code> - Leech specific 4KHDHub movie/series URL\n"
            "• <code>/4khd query [-up CHAT_ID|TOPIC_ID]</code> - Search movie title & leech 1080p Tamil\n"
            "• <code>/4khd check [pages] [-up CHAT_ID]</code> - Check latest releases & leech\n"
            "• <code>/4khd status</code> - View configuration status\n"
            "• <code>/4khd clear</code> - Reset deduplication seen items cache"
        )
        await send_message(message, text)
        return

    dest_chat = None
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

    if clean_parts and (clean_parts[0].startswith("http://") or clean_parts[0].startswith("https://")):
        target_url = clean_parts[0]
        notice = await send_message(message, f"🔎 <b>4KHDHub:</b> Resolving release URL: <code>{target_url}</code>...")
        try:
            candidates = await FourKHDHubResolver.get_candidates_for_url(target_url)
            if not candidates:
                await edit_message(notice, f"❌ No downloadable candidates found for: <code>{target_url}</code>")
                return
            queued = await queue_fourkhd_candidates(candidates, owner_id=message.from_user.id, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>4KHDHub:</b> Resolved {len(candidates)} candidate(s), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"4KHDHub URL resolve failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>4KHDHub error:</b> <code>{err}</code>")

    elif subcmd == "check":
        pages = int(clean_parts[1]) if len(clean_parts) > 1 and clean_parts[1].isdigit() else 1
        notice = await send_message(message, f"🔎 <b>4KHDHub:</b> Crawling Tamil audio releases (pages: {pages})...")
        try:
            candidates = await FourKHDHubResolver.get_tamil_candidates(max_pages=pages)
            if not candidates:
                await edit_message(notice, "No 1080p HEVC/AV1 Tamil audio releases found on 4KHDHub.")
                return
            queued = await queue_fourkhd_candidates(candidates, owner_id=message.from_user.id, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>4KHDHub:</b> Found {len(candidates)} matching candidate(s), queued <b>{queued}</b> new release(s).",
            )
        except Exception as err:
            LOGGER.error(f"4KHDHub check failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>4KHDHub check error:</b> <code>{err}</code>")

    elif subcmd == "status":
        hw = Config.FOURKHDHUB_HOLLYWOOD_CHANNEL_ID or getattr(Config, "HDHUB_HOLLYWOOD_CHANNEL_ID", "Not set")
        ws = Config.FOURKHDHUB_WEBSERIES_CHANNEL_ID or getattr(Config, "HDHUB_WEBSERIES_CHANNEL_ID", "Not set")
        auto = getattr(Config, "FOURKHDHUB_AUTO_LEECH", False)
        seen = _seen_items()
        text = (
            "🎬 <b>4KHDHub Leech Status:</b>\n\n"
            f"<b>Auto Leech (Background):</b> <code>{'Enabled' if auto else 'Disabled'}</code>\n"
            f"<b>Hollywood Channel:</b> <code>{hw}</code>\n"
            f"<b>Webseries Channel:</b> <code>{ws}</code>\n"
            f"<b>Filter:</b> <code>Tamil audio only (1080p HEVC, 1080p AV1)</code>\n"
            f"<b>Seen Releases:</b> <code>{len(seen)} cached</code>\n\n"
            "<b>Interactive Usage:</b>\n"
            "• <code>/4khd URL [-up CHAT_ID]</code> - Leech specific post\n"
            "• <code>/4khd query [-up CHAT_ID]</code> - Search & leech\n"
            "• <code>/4khd check [pages]</code> - Check latest releases\n"
            "• <code>/4khd clear</code> - Clear seen cache"
        )
        await send_message(message, text)

    elif subcmd == "clear":
        await _save_seen(set())
        await send_message(message, "🗑️ <b>4KHDHub seen items cache cleared.</b>")

    else:
        # Search query on demand
        query = " ".join(clean_parts)
        LOGGER.info(f"4KHDHub: user {message.from_user.id} requested search for: '{query}'")
        notice = await send_message(message, f"🔎 <b>4KHDHub:</b> Searching for <code>{query}</code>...")
        try:
            candidates = await FourKHDHubResolver.search_candidates(query)
            if not candidates:
                await edit_message(notice, f"❌ No matching releases found on 4KHDHub for: <code>{query}</code>")
                return
            queued = await queue_fourkhd_candidates(candidates, owner_id=message.from_user.id, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>4KHDHub:</b> Found {len(candidates)} match(es), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"4KHDHub search failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>4KHDHub search error:</b> <code>{err}</code>")


def start_fourkhdhub_scheduler():
    """Initializes and schedules the daily 4KHDHub monitor ONLY if explicitly enabled."""
    if not getattr(Config, "FOURKHDHUB_AUTO_LEECH", False):
        LOGGER.info("4KHDHub background scheduler disabled (on-demand mode active).")
        return

    try:
        from apscheduler.triggers.interval import IntervalTrigger
        from .. import scheduler

        hours = int(getattr(Config, "FOURKHDHUB_CHECK_INTERVAL_HOURS", 24) or 24)
        if scheduler.get_job("fourkhdhub") is None:
            scheduler.add_job(
                fourkhdhub_monitor,
                trigger=IntervalTrigger(hours=hours),
                id="fourkhdhub",
                name="4KHDHub Daily Leech",
                misfire_grace_time=600,
                max_instances=1,
                replace_existing=True,
            )
            LOGGER.info(f"4KHDHub daily scheduler registered (every {hours} hours).")
            if not scheduler.running:
                scheduler.start()
    except Exception as err:
        LOGGER.warning(f"Could not register 4KHDHub daily scheduler: {err}")

