import asyncio
from json import dumps, loads
from os import path as ospath
from typing import Optional

from pyrogram.filters import regex
from pyrogram.handlers import CallbackQueryHandler

from .. import LOGGER, user_data
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.xdmovies_resolver import (
    XDMoviesCandidate,
    XDMoviesResolver,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)
from .rss import _parse_chat_value, _start_rss_download


def _seen_items() -> set[str]:
    """Retrieves seen deduplication keys from Config/database."""
    try:
        raw = getattr(Config, "XDMOVIES_SEEN_ITEMS", "") or "[]"
        data = loads(str(raw))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


async def _save_seen(seen: set[str]):
    """Persists seen keys to Config and MongoDB."""
    value = dumps(sorted(seen)[-3000:], separators=(",", ":"))
    Config.set("XDMOVIES_SEEN_ITEMS", value)
    try:
        await database.update_config({"XDMOVIES_SEEN_ITEMS": value})
    except Exception as err:
        LOGGER.warning(f"XDMovies: failed to persist seen items to DB: {err}")


def _get_target_channel(is_series: bool) -> str:
    """Returns the configured channel/topic ID based on category."""
    if is_series:
        val = (
            getattr(Config, "XDMOVIES_WEBSERIES_CHANNEL_ID", 0)
            or getattr(Config, "XDM_WEBSERIES_CHANNEL_ID", 0)
            or getattr(Config, "PROJECT1_SERIES_TARGET", 0)
        )
    else:
        val = (
            getattr(Config, "XDMOVIES_MOVIES_CHANNEL_ID", 0)
            or getattr(Config, "XDM_MOVIES_CHANNEL_ID", 0)
            or getattr(Config, "PROJECT1_MOVIES_TARGET", 0)
        )
    return str(val).strip() if val else ""


def _get_xdmovies_task_settings(user_id: int, is_series: bool) -> dict:
    """Returns task-specific settings for XDMovies without polluting global user_data."""
    seed_settings = {}
    seed_path = f"configs/user_settings_{user_id}.json"
    if not ospath.isfile(seed_path):
        seed_path = "configs/user_settings_8814651149.json"
    if ospath.isfile(seed_path):
        try:
            with open(seed_path, "r", encoding="utf-8") as f:
                data = loads(f.read())
                seed_settings = data.get("settings", {})
        except Exception:
            pass

    settings = dict(user_data.get(user_id) or seed_settings)

    # 1. Audio stream removal settings: strictly keep Tamil only
    settings["AUTO_REMOVE_STREAMS"] = True
    settings["AUTO_KEEP_AUDIO_LANGS"] = "Tamil, tam"

    # 2. Archive unpacking & smart merging up to 4GB
    settings["AUTO_UNZIP"] = True
    settings["AUTO_MERGE"] = True
    settings["AUTO_PROCESS"] = True

    # 3. Auto-Rename configuration
    base_rename = (
        settings.get("lremname_auto")
        or getattr(Config, "LEECH_FILENAME_REMNAME_AUTO", "")
        or "{name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~[@Anime_Starfall🥰]"
    )
    clean_base = base_rename.replace("[S{season}E{episode}] ", "").replace("[S{season}E{episode}]", "").strip()

    settings["AUTORENAME"] = True
    settings["RENAME_METHOD"] = "auto"

    # 4. Series vs Movie Presentation Segregation (Poster and Description ONLY for series)
    if is_series:
        settings["AUTO_POSTER_ENABLED"] = True
        settings["AUTO_POSTER_SEND_MESSAGE"] = True
        settings["AUTO_POSTER_USE_AS_THUMBNAIL"] = False
        settings["lremname_auto"] = f"[S{{season}}E{{episode}}] {clean_base}"
    else:
        settings["AUTO_POSTER_ENABLED"] = False
        settings["AUTO_POSTER_SEND_MESSAGE"] = False
        settings["AUTO_POSTER_USE_AS_THUMBNAIL"] = False
        settings["lremname_auto"] = clean_base

    # 5. TMDb Landscape Thumbnail
    settings["AUTO_THUMBNAIL"] = True
    settings["THUMBNAIL_MODE"] = "automatic"
    settings["AS_DOCUMENT"] = True

    # 6. Starfall metadata branding
    settings["AUTO_METADATA"] = True
    settings["METADATA_TITLE"] = "[@Anime_Starfall🥰]"

    return settings


async def queue_xdmovies_candidates(
    candidates: list[XDMoviesCandidate],
    owner_id: Optional[int] = None,
    seen: Optional[set[str]] = None,
    target_chat: Optional[str] = None,
) -> int:
    """Queues eligible candidates into WZML-X leech engine with proper channel routing and user settings."""
    seen = seen if seen is not None else _seen_items()
    user_id = int(owner_id or getattr(Config, "XDMOVIES_USER_ID", 8814651149) or 8814651149)
    queued = 0

    for item in candidates:
        if not item.download_url:
            continue
        key = item.dedupe_key
        if key in seen:
            continue

        dest_chat = target_chat or _get_target_channel(item.is_series)
        if not dest_chat or dest_chat == "0":
            LOGGER.warning(
                f"XDMovies: skipping '{item.file_title}' because destination channel for "
                f"{'Webseries' if item.is_series else 'Movies'} is not configured."
            )
            continue

        chat_id, topic_id = _parse_chat_value(dest_chat)
        if not chat_id:
            LOGGER.warning(f"XDMovies: invalid chat ID: {dest_chat}")
            continue

        task_settings = _get_xdmovies_task_settings(user_id, item.is_series)

        LOGGER.info(
            f"XDMovies: queuing '{item.file_title}' ({item.quality}) -> "
            f"{'Webseries' if item.is_series else 'Movies'} ({dest_chat}) via: {item.download_url}"
        )

        try:
            await _start_rss_download(
                url=item.download_url,
                command="leech",
                user_id=user_id,
                rss_chat_id=chat_id,
                rss_topic_id=topic_id,
                item_title=item.file_title,
                auto_leech=True,
                rename_mode="title",
                leech_by="bot",
                rss_upload_chat=dest_chat,
                is_season_pack=item.is_season_pack,
                task_user_dict=task_settings,
            )
            seen.add(key)
            queued += 1
            await asyncio.sleep(1.5)
        except Exception as error:
            LOGGER.error(f"XDMovies: failed to queue '{item.file_title}': {error}", exc_info=True)

    if queued:
        await _save_seen(seen)

    return queued


def _build_dashboard_buttons(user_id: int) -> ButtonMaker:
    """Constructs interactive /userset style inline buttons for XDMovies."""
    buttons = ButtonMaker()
    buttons.data_button("🎯 Movies Target", f"xdmset {user_id} set_movies_target")
    buttons.data_button("🎯 Series Target", f"xdmset {user_id} set_series_target")
    buttons.data_button("🗓️ Leech Last Week", f"xdmset {user_id} lastweek")
    buttons.data_button("🔍 Search & Leech", f"xdmset {user_id} prompt_search")
    buttons.data_button("🔄 Check Latest", f"xdmset {user_id} check_latest")
    buttons.data_button("⏱️ Backlog (14d)", f"xdmset {user_id} backlog_14")
    buttons.data_button("⚙️ Audio: Tamil Only", f"xdmset {user_id} toggle_audio")
    buttons.data_button("🧩 Codec: AV1+HEVC", f"xdmset {user_id} toggle_codec")
    buttons.data_button("📦 Auto Merge (4GB)", f"xdmset {user_id} toggle_merge")
    buttons.data_button("🗜️ Auto Unzip", f"xdmset {user_id} toggle_unzip")
    buttons.data_button("🖼️ TMDb Thumbnails", f"xdmset {user_id} toggle_thumb")
    buttons.data_button("🎨 Series Poster", f"xdmset {user_id} toggle_poster")
    auto_status = "ON" if getattr(Config, "XDMOVIES_AUTO_LEECH", False) else "OFF"
    buttons.data_button(f"🤖 Auto Leech: {auto_status}", f"xdmset {user_id} toggle_autoleech")
    buttons.data_button("🗑️ Clear Cache", f"xdmset {user_id} clear_cache")
    buttons.data_button("🧪 Test Dry-Run", f"xdmset {user_id} prompt_dryrun")
    buttons.data_button("❌ Close", f"xdmset {user_id} close")
    return buttons


def _build_dashboard_text() -> str:
    """Builds formatted dashboard text displaying active site settings."""
    mov_ch = _get_target_channel(is_series=False) or "Not Configured"
    ser_ch = _get_target_channel(is_series=True) or "Not Configured"
    auto_leech = "Enabled" if getattr(Config, "XDMOVIES_AUTO_LEECH", False) else "Disabled"
    seen = _seen_items()

    return (
        "🎬 <b>STARFALLX • XDMOVIES SETTINGS DASHBOARD</b>\n"
        "<i>Site: https://huh.xdmovies.wtf</i>\n\n"
        f"• <b>Movies Target</b>     : <code>{mov_ch}</code>\n"
        f"• <b>Series Target</b>     : <code>{ser_ch}</code>\n"
        "• <b>Resolution Filter</b> : <code>1080p Only</code> [✅ Active]\n"
        "• <b>Codec Filter</b>      : <code>AV1 + H.265 / HEVC</code> [✅ Active]\n"
        "• <b>Audio Filter</b>      : <code>Tamil Audio (Strip others)</code> [✅ Active]\n"
        "• <b>Auto Merge 4GB</b>    : <code>Enabled (Max 4.0 GiB)</code> [✅ Active]\n"
        "• <b>Auto Unzip</b>        : <code>Enabled</code> [✅ Active]\n"
        "• <b>TMDb Thumbnails</b>   : <code>16:9 Landscape Backdrops</code> [✅ Active]\n"
        "• <b>AutoRename Series</b> : <code>[S{season}E{episode}] {name}...</code>\n"
        "• <b>AutoRename Movies</b> : <code>{name} {year}...</code>\n"
        "• <b>Series Poster Card</b>: <code>Enabled</code> (Movies: Disabled)\n"
        f"• <b>Background Leech</b>  : <code>{auto_leech}</code> (Every 24h)\n"
        f"• <b>Seen Releases</b>     : <code>{len(seen)} cached</code>\n\n"
        "<i>Tap a button below to update destination channels or trigger crawling:</i>"
    )


@new_task
async def xdmovies_settings_handler(_, message):
    """Sends the interactive settings dashboard for XDMovies."""
    buttons = _build_dashboard_buttons(message.from_user.id)
    text = _build_dashboard_text()
    await send_message(message, text, buttons.build_menu(2))


@new_task
async def xdmovies_callback(client, query):
    """Handles callback queries from the interactive XDMovies settings menu."""
    data = query.data.split()
    user_id = int(data[1])
    action = data[2]

    if query.from_user.id != user_id and not await CustomFilters.sudo(client, query):
        await query.answer("You are not authorized to edit these settings.", show_alert=True)
        return

    if action == "close":
        await query.answer()
        await delete_message(query.message)
        return

    elif action == "set_movies_target":
        await query.answer()
        prompt = await send_message(
            query.message,
            "🎯 <b>Send the Channel ID or Topic ID for Movies</b>\n"
            "<i>Format: <code>-100xxxxxxxxxx</code> or <code>-100xxxxxxxxxx:1234</code></i>\n"
            "<i>Send /cancel to abort.</i>",
        )
        try:
            response = await client.listen.Message(
                filters=regex(r"^(-\d+|/cancel)"),
                id=query.from_user.id,
                timeout=60,
            )
            txt = response.text.strip()
            if txt != "/cancel":
                Config.set("XDMOVIES_MOVIES_CHANNEL_ID", txt)
                try:
                    await database.update_config({"XDMOVIES_MOVIES_CHANNEL_ID": txt})
                except Exception:
                    pass
                await send_message(query.message, f"✅ <b>Movies Target Channel set to:</b> <code>{txt}</code>")
            await delete_message(prompt)
            await delete_message(response)
        except Exception:
            await delete_message(prompt)

        buttons = _build_dashboard_buttons(user_id)
        await edit_message(query.message, _build_dashboard_text(), buttons.build_menu(2))
        return

    elif action == "set_series_target":
        await query.answer()
        prompt = await send_message(
            query.message,
            "🎯 <b>Send the Channel ID or Topic ID for Web Series</b>\n"
            "<i>Format: <code>-100xxxxxxxxxx</code> or <code>-100xxxxxxxxxx:1234</code></i>\n"
            "<i>Send /cancel to abort.</i>",
        )
        try:
            response = await client.listen.Message(
                filters=regex(r"^(-\d+|/cancel)"),
                id=query.from_user.id,
                timeout=60,
            )
            txt = response.text.strip()
            if txt != "/cancel":
                Config.set("XDMOVIES_WEBSERIES_CHANNEL_ID", txt)
                try:
                    await database.update_config({"XDMOVIES_WEBSERIES_CHANNEL_ID": txt})
                except Exception:
                    pass
                await send_message(query.message, f"✅ <b>Web Series Target Channel set to:</b> <code>{txt}</code>")
            await delete_message(prompt)
            await delete_message(response)
        except Exception:
            await delete_message(prompt)

        buttons = _build_dashboard_buttons(user_id)
        await edit_message(query.message, _build_dashboard_text(), buttons.build_menu(2))
        return

    elif action == "lastweek":
        await query.answer("🗓️ Crawling releases from past 7 days...", show_alert=False)
        notice = await send_message(query.message, "🔎 <b>XDMovies:</b> Crawling last 7 days 1080p Tamil releases...")
        try:
            seen = _seen_items()
            candidates = await XDMoviesResolver.get_tamil_candidates(max_pages=4, days=7, seen=seen)
            if not candidates:
                await edit_message(notice, "No new 1080p AV1/HEVC Tamil releases found from the past 7 days.")
                return
            queued = await queue_xdmovies_candidates(candidates, owner_id=user_id, seen=seen)
            await edit_message(
                notice,
                f"✅ <b>XDMovies Last Week:</b> Found {len(candidates)} candidate(s), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"XDMovies lastweek failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>Error:</b> <code>{err}</code>")
        return

    elif action == "check_latest":
        await query.answer("🔄 Checking latest updates...", show_alert=False)
        notice = await send_message(query.message, "🔎 <b>XDMovies:</b> Crawling latest releases (pages: 2)...")
        try:
            seen = _seen_items()
            candidates = await XDMoviesResolver.get_tamil_candidates(max_pages=2, seen=seen)
            if not candidates:
                await edit_message(notice, "No new 1080p AV1/HEVC Tamil releases found on XDMovies.")
                return
            queued = await queue_xdmovies_candidates(candidates, owner_id=user_id, seen=seen)
            await edit_message(
                notice,
                f"✅ <b>XDMovies:</b> Found {len(candidates)} match(es), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"XDMovies check failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>Error:</b> <code>{err}</code>")
        return

    elif action == "backlog_14":
        await query.answer("⏱️ Crawling past 14 days...", show_alert=False)
        notice = await send_message(query.message, "🔎 <b>XDMovies:</b> Crawling last 14 days 1080p Tamil releases...")
        try:
            seen = _seen_items()
            candidates = await XDMoviesResolver.get_tamil_candidates(max_pages=6, days=14, seen=seen)
            if not candidates:
                await edit_message(notice, "No new 1080p Tamil releases found in the last 14 days.")
                return
            queued = await queue_xdmovies_candidates(candidates, owner_id=user_id, seen=seen)
            await edit_message(
                notice,
                f"✅ <b>XDMovies Backlog:</b> Found {len(candidates)} candidate(s), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"XDMovies backlog failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>Error:</b> <code>{err}</code>")
        return

    elif action == "toggle_autoleech":
        cur = getattr(Config, "XDMOVIES_AUTO_LEECH", False)
        new_val = not cur
        Config.set("XDMOVIES_AUTO_LEECH", new_val)
        try:
            await database.update_config({"XDMOVIES_AUTO_LEECH": new_val})
        except Exception:
            pass
        await query.answer(f"Auto Leech set to: {'Enabled' if new_val else 'Disabled'}", show_alert=True)
        buttons = _build_dashboard_buttons(user_id)
        await edit_message(query.message, _build_dashboard_text(), buttons.build_menu(2))
        return

    elif action == "clear_cache":
        await _save_seen(set())
        await query.answer("🗑️ Deduplication cache cleared!", show_alert=True)
        buttons = _build_dashboard_buttons(user_id)
        await edit_message(query.message, _build_dashboard_text(), buttons.build_menu(2))
        return

    elif action == "prompt_search":
        await query.answer()
        prompt = await send_message(query.message, "🔍 <b>Send search query for XDMovies</b>\n<i>Send /cancel to abort.</i>")
        try:
            response = await client.listen.Message(id=query.from_user.id, timeout=60)
            txt = response.text.strip()
            if txt != "/cancel":
                notice = await send_message(query.message, f"🔎 <b>XDMovies:</b> Searching for <code>{txt}</code>...")
                cands = await XDMoviesResolver.search_candidates(txt)
                if not cands:
                    await edit_message(notice, f"❌ No matching 1080p Tamil releases found for <code>{txt}</code>.")
                else:
                    queued = await queue_xdmovies_candidates(cands, owner_id=user_id)
                    await edit_message(notice, f"✅ <b>XDMovies:</b> Found {len(cands)} release(s), queued <b>{queued}</b>.")
            await delete_message(prompt)
            await delete_message(response)
        except Exception:
            await delete_message(prompt)
        return

    elif action == "prompt_dryrun":
        await query.answer()
        prompt = await send_message(query.message, "🧪 <b>Send movie/series URL to test parsing without downloading:</b>\n<i>Send /cancel to abort.</i>")
        try:
            response = await client.listen.Message(id=query.from_user.id, timeout=60)
            target_url = response.text.strip()
            if target_url != "/cancel":
                cands = await XDMoviesResolver.get_candidates_for_url(target_url)
                if not cands:
                    await send_message(query.message, f"❌ <b>Dry-Run Result:</b> No candidates met 1080p AV1/HEVC Tamil criteria for <code>{target_url}</code>.")
                else:
                    msg = f"🧪 <b>Dry-Run Successful: Found {len(cands)} candidate(s):</b>\n\n"
                    for idx, c in enumerate(cands, 1):
                        msg += (
                            f"<b>{idx}. {c.file_title}</b>\n"
                            f"• Quality: <code>{c.quality}</code> | Tamil: <code>{c.has_tamil}</code>\n"
                            f"• Type: <code>{c.category.upper()}</code> | Size: <code>{c.file_size_str}</code>\n"
                            f"• Link: <code>{c.download_url}</code>\n\n"
                        )
                    await send_message(query.message, msg[:4000])
            await delete_message(prompt)
            await delete_message(response)
        except Exception:
            await delete_message(prompt)
        return

    else:
        await query.answer("Setting active.", show_alert=True)


@new_task
async def xdmovies_handler(_, message):
    """Telegram command handler for /xdm or /xdmovies."""
    parts = (message.text or "").split()
    if len(parts) < 2:
        # No arguments: launch interactive settings dashboard
        await xdmovies_settings_handler(_, message)
        return

    dest_chat = None
    clean_parts = []
    i = 1
    while i < len(parts):
        if parts[i] in ["-up", "-t"]:
            if i + 1 < len(parts):
                dest_chat = parts[i + 1].strip()
            i += 2
            continue
        clean_parts.append(parts[i])
        i += 1

    if not clean_parts:
        await xdmovies_settings_handler(_, message)
        return

    subcmd = clean_parts[0].lower()

    if clean_parts[0].startswith("http://") or clean_parts[0].startswith("https://"):
        target_url = clean_parts[0]
        notice = await send_message(message, f"🔎 <b>XDMovies:</b> Resolving release URL: <code>{target_url}</code>...")
        try:
            candidates = await XDMoviesResolver.get_candidates_for_url(target_url)
            if not candidates:
                await edit_message(notice, f"❌ No 1080p AV1/HEVC Tamil candidates found for: <code>{target_url}</code>")
                return
            queued = await queue_xdmovies_candidates(candidates, owner_id=message.from_user.id, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>XDMovies:</b> Resolved {len(candidates)} candidate(s), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"XDMovies URL resolve failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>XDMovies error:</b> <code>{err}</code>")

    elif subcmd in ["lastweek", "last_week", "week"]:
        notice = await send_message(message, "🔎 <b>XDMovies:</b> Crawling last 7 days 1080p Tamil releases...")
        try:
            seen = _seen_items()
            candidates = await XDMoviesResolver.get_tamil_candidates(max_pages=4, days=7, seen=seen)
            if not candidates:
                await edit_message(notice, "No new 1080p AV1/HEVC Tamil releases found from the past 7 days.")
                return
            queued = await queue_xdmovies_candidates(candidates, owner_id=message.from_user.id, seen=seen, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>XDMovies Last Week:</b> Found {len(candidates)} release(s), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"XDMovies lastweek failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>XDMovies error:</b> <code>{err}</code>")

    elif subcmd in ["backlog", "sync"]:
        days = int(clean_parts[1]) if len(clean_parts) > 1 and clean_parts[1].isdigit() else 7
        notice = await send_message(message, f"🔎 <b>XDMovies:</b> Crawling last {days} days 1080p Tamil releases...")
        try:
            seen = _seen_items()
            candidates = await XDMoviesResolver.get_tamil_candidates(max_pages=5, days=days, seen=seen)
            if not candidates:
                await edit_message(notice, f"No new 1080p Tamil releases found in the last {days} days.")
                return
            queued = await queue_xdmovies_candidates(candidates, owner_id=message.from_user.id, seen=seen, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>XDMovies Backlog:</b> Found {len(candidates)} release(s), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"XDMovies backlog failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>Error:</b> <code>{err}</code>")

    elif subcmd == "check":
        pages = int(clean_parts[1]) if len(clean_parts) > 1 and clean_parts[1].isdigit() else 2
        notice = await send_message(message, f"🔎 <b>XDMovies:</b> Checking latest releases (pages: {pages})...")
        try:
            seen = _seen_items()
            candidates = await XDMoviesResolver.get_tamil_candidates(max_pages=pages, seen=seen)
            if not candidates:
                await edit_message(notice, "No new 1080p AV1/HEVC Tamil audio releases found on XDMovies.")
                return
            queued = await queue_xdmovies_candidates(candidates, owner_id=message.from_user.id, seen=seen, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>XDMovies:</b> Found {len(candidates)} match(es), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"XDMovies check failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>Error:</b> <code>{err}</code>")

    elif subcmd in ["status", "settings"]:
        await xdmovies_settings_handler(_, message)

    elif subcmd == "clear":
        await _save_seen(set())
        await send_message(message, "🗑️ <b>XDMovies seen items cache cleared.</b>")

    else:
        # Search query
        query = " ".join(clean_parts)
        notice = await send_message(message, f"🔎 <b>XDMovies:</b> Searching for <code>{query}</code>...")
        try:
            candidates = await XDMoviesResolver.search_candidates(query)
            if not candidates:
                await edit_message(notice, f"❌ No matching 1080p Tamil releases found on XDMovies for: <code>{query}</code>")
                return
            queued = await queue_xdmovies_candidates(candidates, owner_id=message.from_user.id, target_chat=dest_chat)
            await edit_message(
                notice,
                f"✅ <b>XDMovies:</b> Found {len(candidates)} match(es), queued <b>{queued}</b> release(s).",
            )
        except Exception as err:
            LOGGER.error(f"XDMovies search failed: {err}", exc_info=True)
            await edit_message(notice, f"❌ <b>XDMovies search error:</b> <code>{err}</code>")
