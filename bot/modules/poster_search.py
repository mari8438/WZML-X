from asyncio import Event, TimeoutError, sleep, wait_for
from html import escape
from time import time

from aiofiles.os import remove
from aiofiles.os import path as aiopath
from pyrogram.types import InputMediaPhoto

from .. import LOGGER, user_data
from ..helper.ext_utils.bot_utils import new_task, update_user_ldata
from ..helper.ext_utils.db_handler import database
from ..core.config_manager import Config
from ..helper.ext_utils.media_utils import (
    choose_media_title_seed,
    create_thumb,
    format_clean_poster_title,
    get_video_thumbnail,
)
from ..helper.poster_engine import (
    POSTER_TEMPLATE_COUNT,
    render_poster_option,
    save_artwork_url,
    search_poster_metadata,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)

POSTER_SEARCH_CACHE = {}
PENDING_THUMB_UPLOADS = {}
PICKER_TTL = 900


def _query_from_message(message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        return parts[1].strip()
    reply = getattr(message, "reply_to_message", None)
    if reply:
        if getattr(reply, "text", None):
            return reply.text.strip()
        for media in ("video", "document", "audio"):
            item = getattr(reply, media, None)
            if item and getattr(item, "file_name", None):
                return item.file_name
    return ""


def _artwork(metadata, category):
    artwork = metadata.get("artwork") or {}
    return list(artwork.get(category) or [])


def _summary(session):
    metadata = session["metadata"]
    counts = {
        key: len(_artwork(metadata, key))
        for key in ("landscape", "portrait", "clean")
    }
    title = escape(str(metadata.get('title') or metadata.get('name') or 'Unknown'))
    category = escape(str(metadata.get('category') or 'movie'))
    lines = [
        f"📺 <b>Thumbnail & Poster Studio</b>",
        f"<b>Title:</b> <code>{title}</code>",
        "",
        "<blockquote>",
        f"• <b>Type:</b> {category}",
    ]
    if metadata.get("season"):
        lines.append(f"• <b>Season:</b> {escape(str(metadata['season']))}")
    if metadata.get("episode"):
        lines.append(f"• <b>Episode:</b> {escape(str(metadata['episode']))}")
    if metadata.get("year"):
        lines.append(f"• <b>Year:</b> <code>{escape(str(metadata['year']))}</code>")
    if metadata.get("provider"):
        lines.append(f"• <b>Provider:</b> {escape(str(metadata['provider']))}")
    lines.extend([
        f"• <b>Available Art:</b> Landscape ({counts['landscape']}) • Portrait ({counts['portrait']}) • Clean ({counts['clean']})",
        "</blockquote>",
    ])
    overview = metadata.get("overview") or metadata.get("description") or ""
    if overview:
        short_syn = overview[:480].rsplit(" ", 1)[0] + "..." if len(overview) > 480 else overview
        lines.append(f"\n<blockquote expandable><b>💎 𝗦𝗬𝗡𝗢𝗣𝗦𝗜𝗦 :</b>\n{escape(short_syn)}</blockquote>")
    lines.append("\n<i>Choose an artwork type below or send your own thumbnail.</i>")
    return "\n".join(lines)


def _menu_buttons(user_id, task_picker=False):
    buttons = ButtonMaker()
    buttons.data_button("Landscape", f"psel {user_id} 0 cat landscape")
    buttons.data_button("Portrait", f"psel {user_id} 0 cat portrait")
    buttons.data_button("Clean Landscape", f"psel {user_id} 0 cat clean")
    buttons.data_button("Poster Styles", f"psel {user_id} 0 styles")
    buttons.data_button("Send Thumbnail", f"psel {user_id} 0 upload")
    buttons.data_button("Back", f"psel {user_id} 0 back", "footer")
    buttons.data_button(
        "Skip" if task_picker else "Close",
        f"psel {user_id} 0 close",
        "footer",
    )
    return buttons.build_menu(2)


def _style_buttons(user_id):
    buttons = ButtonMaker()
    for style in range(1, POSTER_TEMPLATE_COUNT + 1):
        buttons.data_button(f"Style {style}", f"psel {user_id} 0 style {style}")
    buttons.data_button("Back", f"psel {user_id} 0 menu", "footer")
    return buttons.build_menu(3)


def _pager_buttons(user_id, page, total):
    buttons = ButtonMaker()
    buttons.data_button("<<", f"psel {user_id} 0 page 0")
    buttons.data_button("<", f"psel {user_id} 0 page {max(0, page - 1)}")
    buttons.data_button(f"{page + 1}/{total}", f"psel {user_id} 0 noop")
    buttons.data_button(">", f"psel {user_id} 0 page {min(total - 1, page + 1)}")
    buttons.data_button(">>", f"psel {user_id} 0 page {total - 1}")
    buttons.data_button("Back", f"psel {user_id} 0 menu", "footer")
    buttons.data_button("Choose", f"psel {user_id} 0 choose", "footer")
    return buttons.build_menu(5, f_cols=2)


def _art_caption(session, item, page, total):
    metadata = session["metadata"]
    lines = [
        f"<b>{escape(str(metadata.get('title') or 'Thumbnail'))}</b>",
    ]
    if metadata.get("season"):
        lines.append(f"Season: <b>{escape(str(metadata['season']))}</b>")
    if item.get("episode"):
        lines.append(f"Episode: <b>{item['episode']}</b>")
    if metadata.get("year"):
        lines.append(f"Year: <b>{escape(str(metadata['year']))}</b>")
    lines.extend(
        [
            f"Source: <b>{escape(str(item.get('source') or metadata.get('provider') or 'Provider'))}</b>",
            f"Type: <b>{session['category'].replace('_', ' ').title()}</b>",
        ]
    )
    if item.get("width") and item.get("height"):
        lines.append(f"Size: <code>{item['width']} × {item['height']}</code>")
    lines.append(f"Result: <b>{page + 1}/{total}</b>")
    return "\n".join(lines)


async def _edit_photo(message, media, caption, reply_markup):
    await message.edit_media(
        InputMediaPhoto(media=media, caption=caption),
        reply_markup=reply_markup,
    )


async def start_thumbnail_picker(
    message,
    query_text,
    entry="poster",
    task_listener=None,
    media_path=None,
):
    query_text = str(query_text or "").strip()
    if not query_text:
        await send_message(message, "Send a title such as <code>Naruto S02</code>.")
        return None
    wait = await send_message(
        message, f"Searching thumbnails for <code>{escape(query_text)}</code>..."
    )
    user_id = message.from_user.id
    now = time()
    for key, old_session in list(POSTER_SEARCH_CACHE.items()):
        if key[0] == user_id and now - old_session.get("created", now) > PICKER_TTL:
            POSTER_SEARCH_CACHE.pop(key, None)
    user_dict = user_data.get(user_id, {})
    fallback_frame = ""
    try:
        metadata = await search_poster_metadata(query_text, user_dict, media_path)
        if not metadata:
            await edit_message(wait, "No metadata or artwork found.")
            return None
        artwork = metadata.setdefault("artwork", {})
        if task_listener is not None and media_path and not artwork.get("landscape"):
            frame = await get_video_thumbnail(media_path, None)
            if frame:
                fallback_frame = frame
                artwork["landscape"] = [
                    {
                        "url": frame,
                        "kind": "landscape",
                        "source": "Video Frame Fallback",
                        "language": None,
                        "width": 0,
                        "height": 0,
                        "episode": metadata.get("episode") or "",
                    }
                ]
                metadata["landscape_url"] = frame
        preview = (
            metadata.get("landscape_url")
            or metadata.get("portrait_url")
            or await render_poster_option(metadata, user_id, user_dict, "1")
        )
        session = {
            "query": query_text,
            "title": metadata.get("title") or metadata.get("name") or query_text,
            "season": metadata.get("season") or "",
            "episode": metadata.get("episode") or "",
            "artwork": metadata.get("artwork") or {},
            "metadata": metadata,
            "category": "",
            "page": 0,
            "entry": entry,
            "task_listener": task_listener,
            "done_event": Event() if task_listener is not None else None,
            "created": time(),
            "preview": preview,
            "fallback_frame": fallback_frame,
        }
        await delete_message(wait)
        sent = await send_message(
            message,
            _summary(session),
            _menu_buttons(user_id, task_listener is not None),
            photo=preview,
        )
        if not getattr(sent, "id", None):
            LOGGER.warning(f"Thumbnail picker delivery failed: {sent}")
            return None
        session["message"] = sent
        POSTER_SEARCH_CACHE[(user_id, sent.id)] = session
        return sent
    except Exception as error:
        LOGGER.error(f"Thumbnail search failed: {error}", exc_info=True)
        if fallback_frame and await aiopath.exists(fallback_frame):
            await remove(fallback_frame)
        await edit_message(
            wait, f"Thumbnail search failed: <code>{escape(str(error)[:500])}</code>"
        )
        return None


def _finish_task_session(session, path=None):
    listener = session.get("task_listener")
    if listener is not None and path:
        listener.thumb = path
    done_event = session.get("done_event")
    if done_event is not None and not done_event.is_set():
        done_event.set()


async def open_task_thumbnail_picker(listener, media_path):
    """Open the shared picker after download and pause this task until selection."""
    if getattr(listener, "_manual_thumbnail_prompted", False):
        return getattr(listener, "thumb", None)
    listener._manual_thumbnail_prompted = True
    raw_query = choose_media_title_seed(
        getattr(listener, "name", ""),
        first_file=getattr(listener, "file_details", {}).get("first_file", ""),
        file_caption=getattr(listener, "file_details", {}).get("caption", ""),
        custom_name=getattr(listener, "custom_name", ""),
        link=getattr(listener, "source_url", ""),
        merge_source_name=getattr(listener, "merge_source_name", ""),
        prefer_filename=True,
    )
    rename_regex = (
        getattr(listener, "user_dict", {}).get("lremname_regex")
        or Config.LEECH_FILENAME_REMNAME_REGEX
    )
    title, season, year = format_clean_poster_title(raw_query, rename_regex)
    query_parts = [title]
    if season:
        query_parts.append(season)
    if year:
        query_parts.append(str(year))
    query_text = " ".join(part for part in query_parts if part).strip() or raw_query
    sent = await start_thumbnail_picker(
        listener.message,
        query_text,
        "task",
        task_listener=listener,
        media_path=media_path,
    )
    if not sent:
        return None
    key = (listener.user_id, sent.id)
    session = POSTER_SEARCH_CACHE.get(key)
    if not session:
        return None
    deadline = time() + PICKER_TTL
    timed_out = False
    while not session["done_event"].is_set():
        if getattr(listener, "is_cancelled", False):
            POSTER_SEARCH_CACHE.pop(key, None)
            fallback_frame = session.get("fallback_frame")
            if fallback_frame and await aiopath.exists(fallback_frame):
                await remove(fallback_frame)
            try:
                await delete_message(sent)
            except Exception:
                pass
            return None
        remaining = deadline - time()
        if remaining <= 0:
            timed_out = True
            break
        try:
            await wait_for(
                session["done_event"].wait(),
                timeout=min(2, remaining),
            )
        except TimeoutError:
            continue
    if timed_out:
        POSTER_SEARCH_CACHE.pop(key, None)
        try:
            await sent.edit_caption(
                caption="<b>Manual thumbnail selection timed out.</b> Continuing upload.",
                reply_markup=None,
            )
        except Exception:
            pass
    fallback_frame = session.get("fallback_frame")
    if (
        fallback_frame
        and fallback_frame != getattr(listener, "thumb", None)
        and await aiopath.exists(fallback_frame)
    ):
        await remove(fallback_frame)
    return getattr(listener, "thumb", None)


@new_task
async def poster_search(_, message):
    query = _query_from_message(message)
    if not query:
        await send_message(
            message, "Send a title with /poster or reply to a file/message."
        )
        return
    await start_thumbnail_picker(message, query, "poster")


@new_task
async def _expire_upload(user_id, created):
    await sleep(60)
    pending = PENDING_THUMB_UPLOADS.get(user_id)
    if pending and pending.get("created") == created:
        PENDING_THUMB_UPLOADS.pop(user_id, None)


async def pending_thumbnail_upload_filter(_, __, message):
    user = message.from_user or message.sender_chat
    pending = PENDING_THUMB_UPLOADS.get(getattr(user, "id", 0))
    return bool(
        pending
        and pending["chat_id"] == message.chat.id
        and (message.photo or message.document)
    )


@new_task
async def receive_thumbnail_upload(_, message):
    user_id = message.from_user.id
    pending = PENDING_THUMB_UPLOADS.pop(user_id, None)
    if not pending:
        return
    try:
        session_key = pending.get("session_key")
        session = POSTER_SEARCH_CACHE.get(session_key) if session_key else None
        task_listener = session.get("task_listener") if session else None
        thumb_id = (
            f"task_{task_listener.mid}"
            if task_listener is not None
            else user_id
        )
        path = await create_thumb(message, thumb_id)
        if task_listener is not None:
            _finish_task_session(session, path)
            POSTER_SEARCH_CACHE.pop(session_key, None)
            picker_message = session.get("message")
            if picker_message:
                try:
                    await picker_message.edit_caption(
                        caption="<b>Custom thumbnail selected for this task.</b>",
                        reply_markup=None,
                    )
                except Exception:
                    pass
            await send_message(message, "<b>Custom thumbnail selected for this task.</b>")
            return
        update_user_ldata(user_id, "THUMBNAIL", path)
        update_user_ldata(user_id, "THUMBNAIL_MODE", "manual")
        await database.update_user_doc(user_id, "THUMBNAIL", path)
        await database.update_user_data(user_id)
        await send_message(message, "✅ <b>Custom thumbnail saved in Manual mode.</b>")
    except Exception as error:
        await send_message(
            message,
            f"Unable to save that thumbnail: <code>{escape(str(error)[:300])}</code>",
        )


@new_task
async def poster_select(_, query):
    data = query.data.split()
    user_id = query.from_user.id
    if len(data) < 4 or data[1] != str(user_id):
        await query.answer("Not yours.", show_alert=True)
        return
    session = POSTER_SEARCH_CACHE.get((user_id, query.message.id))
    action = data[3]
    if action == "close":
        await query.answer()
        if session:
            _finish_task_session(session)
        POSTER_SEARCH_CACHE.pop((user_id, query.message.id), None)
        await delete_message(query.message)
        return
    if not session:
        await query.answer("Search expired. Run /poster again.", show_alert=True)
        return
    if time() - session.get("created", time()) > PICKER_TTL:
        POSTER_SEARCH_CACHE.pop((user_id, query.message.id), None)
        await query.answer("Search expired. Run /poster again.", show_alert=True)
        return
    metadata = session["metadata"]
    user_dict = user_data.get(user_id, {})
    try:
        if action in {"menu", "back"}:
            await query.answer()
            await _edit_photo(
                query.message,
                session["preview"],
                _summary(session),
                _menu_buttons(user_id, session.get("task_listener") is not None),
            )
        elif action == "cat":
            category = data[4] if len(data) > 4 else ""
            items = _artwork(metadata, category)
            if not items:
                await query.answer("No artwork is available in this category.", show_alert=True)
                return
            session.update(category=category, page=0)
            await query.answer()
            await _edit_photo(
                query.message,
                items[0]["url"],
                _art_caption(session, items[0], 0, len(items)),
                _pager_buttons(user_id, 0, len(items)),
            )
        elif action == "page":
            items = _artwork(metadata, session.get("category"))
            if not items:
                await query.answer("Artwork list expired.", show_alert=True)
                return
            page = max(0, min(int(data[4]), len(items) - 1))
            session["page"] = page
            await query.answer()
            await _edit_photo(
                query.message,
                items[page]["url"],
                _art_caption(session, items[page], page, len(items)),
                _pager_buttons(user_id, page, len(items)),
            )
        elif action == "choose":
            category = session.get("category")
            items = _artwork(metadata, category)
            page = int(session.get("page", 0))
            if not items or page >= len(items):
                await query.answer("Artwork selection expired.", show_alert=True)
                return
            kind = "poster" if category == "portrait" else "landscape"
            task_listener = session.get("task_listener")
            if task_listener is not None:
                if items[page].get("source") == "Video Frame Fallback":
                    path = items[page]["url"]
                else:
                    path = await save_artwork_url(
                        items[page]["url"],
                        f"task_{task_listener.mid}",
                        kind,
                    )
                _finish_task_session(session, path)
                await query.answer("Thumbnail selected for this task.", show_alert=True)
                await query.message.edit_caption(
                    caption="<b>Manual thumbnail selected for this task.</b>",
                    reply_markup=None,
                )
                POSTER_SEARCH_CACHE.pop((user_id, query.message.id), None)
                return
            storage_key = f"THUMBNAIL_{kind.upper()}"
            update_user_ldata(user_id, storage_key, path)
            update_user_ldata(user_id, "THUMBNAIL_MODE", "manual")
            await database.update_user_doc(user_id, storage_key, path)
            await database.update_user_data(user_id)
            await query.answer(f"Saved {kind} thumbnail.", show_alert=True)
            await query.message.edit_caption(
                caption=f"✅ <b>Manual {kind} thumbnail selected.</b>\n<code>{escape(path)}</code>",
                reply_markup=None,
            )
            POSTER_SEARCH_CACHE.pop((user_id, query.message.id), None)
        elif action == "styles":
            await query.answer()
            await _edit_photo(
                query.message,
                session["preview"],
                "<b>Poster Styles</b>\n\nChoose a branded poster style.",
                _style_buttons(user_id),
            )
        elif action == "style":
            style = data[4] if len(data) > 4 else "1"
            if style not in {str(value) for value in range(1, POSTER_TEMPLATE_COUNT + 1)}:
                style = "1"
            task_listener = session.get("task_listener")
            path = await render_poster_option(
                metadata,
                user_id,
                user_dict,
                style,
                save_thumbnail=task_listener is None,
            )
            if task_listener is not None:
                _finish_task_session(session, path)
                await query.answer(
                    f"Poster style {style} selected for this task.",
                    show_alert=True,
                )
                await query.message.edit_caption(
                    caption=f"<b>Poster style {style} selected for this task.</b>",
                    reply_markup=None,
                )
                POSTER_SEARCH_CACHE.pop((user_id, query.message.id), None)
                return
            update_user_ldata(user_id, "THUMBNAIL", path)
            update_user_ldata(user_id, "THUMBNAIL_MODE", "manual")
            await database.update_user_doc(user_id, "THUMBNAIL", path)
            await database.update_user_data(user_id)
            await query.answer(f"Saved poster style {style}.", show_alert=True)
            await query.message.edit_caption(
                caption=f"✅ <b>Poster style {style} saved as the custom thumbnail.</b>\n<code>{escape(path)}</code>",
                reply_markup=None,
            )
            POSTER_SEARCH_CACHE.pop((user_id, query.message.id), None)
        elif action == "upload":
            created = time()
            PENDING_THUMB_UPLOADS[user_id] = {
                "chat_id": query.message.chat.id,
                "created": created,
                "session_key": (user_id, query.message.id),
            }
            await _expire_upload(user_id, created)
            await query.answer("Send a photo or image document within 60 seconds.", show_alert=True)
            await query.message.reply_text(
                "Send the custom thumbnail now. It will be saved as the generic Manual thumbnail."
            )
        elif action == "noop":
            await query.answer()
    except Exception as error:
        LOGGER.error(f"Thumbnail picker action failed: {error}", exc_info=True)
        await query.answer("Thumbnail action failed. Try again.", show_alert=True)
