from pyrogram.enums import ButtonStyle

from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import COMMAND_USAGE, new_task
from ..helper.ext_utils.help_messages import (
    CATEGORY_HELP_DICT,
    CLONE_HELP_DICT,
    MIRROR_HELP_DICT,
    YT_HELP_DICT,
    get_full_telegraph_help_html,
    get_help_string,
    help_string,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)

_cached_help_url = None


async def _get_help_url():
    global _cached_help_url
    if not _cached_help_url:
        try:
            from ..helper.telegram_helper.tg_ui_formatter import publish_telegraph_article

            _cached_help_url = await publish_telegraph_article(
                title="StarfallX v1.2 — User Guide & Commands",
                html_content=get_full_telegraph_help_html(),
            )
        except Exception:
            _cached_help_url = None
    return _cached_help_url


def _build_help_buttons(guide_url=None, category=None):
    buttons = ButtonMaker()
    bot_username = getattr(TgClient, "BNAME", "") or ""
    group_url = (
        f"https://t.me/{bot_username}?startgroup=true"
        if bot_username
        else (getattr(Config, "AUTHOR_URL", "") or "https://t.me/Anime_Starfall")
    )
    manual_url = (
        guide_url
        or getattr(Config, "AUTHOR_URL", "")
        or "https://t.me/Anime_Starfall"
    )
    if category is None:
        buttons.data_button("📥 Leech", "help cat leech", "header", style=ButtonStyle.PRIMARY)
        buttons.data_button("☁️ Mirror", "help cat mirror", "header", style=ButtonStyle.PRIMARY)
        buttons.data_button("🎬 Video Studio", "help cat videotools", "header", style=ButtonStyle.PRIMARY)
        buttons.data_button("⚙️ Settings", "help cat settings", "header", style=ButtonStyle.PRIMARY)
    else:
        buttons.data_button("🔙 All Commands", "help home", "header", style=ButtonStyle.PRIMARY)

    buttons.url_button("👥 USE BOT IN GROUP", group_url, style=ButtonStyle.SUCCESS)
    buttons.url_button("📖 TELEGRAPH MANUAL ↗", manual_url, style=ButtonStyle.PRIMARY)
    buttons.data_button("❌ Close", "help close", "footer", style=ButtonStyle.DANGER)
    return buttons.build_menu(b_cols=2, h_cols=2, f_cols=1)


@new_task
async def arg_usage(_, query):
    data = query.data.split()
    message = query.message
    await query.answer()
    if data[1] == "close":
        return await delete_message(message, message.reply_to_message)
    if data[1] == "cat":
        cat_key = data[2]
        cat_text = CATEGORY_HELP_DICT.get(cat_key, get_help_string())
        guide_url = await _get_help_url()
        button = _build_help_buttons(guide_url, category=cat_key)
        return await edit_message(message, cat_text, button)
    if data[1] == "home":
        guide_url = await _get_help_url()
        button = _build_help_buttons(guide_url)
        return await edit_message(message, get_help_string(), button)
    pg_no = int(data[3])
    if data[1] == "nex":
        if data[2] == "mirror":
            await edit_message(
                message, COMMAND_USAGE["mirror"][0], COMMAND_USAGE["mirror"][pg_no + 1]
            )
        elif data[2] == "yt":
            await edit_message(
                message, COMMAND_USAGE["yt"][0], COMMAND_USAGE["yt"][pg_no + 1]
            )
        elif data[2] == "clone":
            await edit_message(
                message, COMMAND_USAGE["clone"][0], COMMAND_USAGE["clone"][pg_no + 1]
            )
    elif data[1] == "pre":
        if data[2] == "mirror":
            await edit_message(
                message, COMMAND_USAGE["mirror"][0], COMMAND_USAGE["mirror"][pg_no - 1]
            )
        elif data[2] == "yt":
            await edit_message(
                message, COMMAND_USAGE["yt"][0], COMMAND_USAGE["yt"][pg_no - 1]
            )
        elif data[2] == "clone":
            await edit_message(
                message, COMMAND_USAGE["clone"][0], COMMAND_USAGE["clone"][pg_no - 1]
            )
    elif data[1] == "back":
        if data[2] == "m":
            await edit_message(
                message, COMMAND_USAGE["mirror"][0], COMMAND_USAGE["mirror"][pg_no + 1]
            )
        elif data[2] == "y":
            await edit_message(
                message, COMMAND_USAGE["yt"][0], COMMAND_USAGE["yt"][pg_no + 1]
            )
        elif data[2] == "c":
            await edit_message(
                message, COMMAND_USAGE["clone"][0], COMMAND_USAGE["clone"][pg_no + 1]
            )
    elif data[1] == "mirror":
        buttons = ButtonMaker()
        buttons.data_button("Back", f"help back m {pg_no}")
        button = buttons.build_menu()
        await edit_message(message, MIRROR_HELP_DICT[data[2]], button)
    elif data[1] == "yt":
        buttons = ButtonMaker()
        buttons.data_button("Back", f"help back y {pg_no}")
        button = buttons.build_menu()
        await edit_message(message, YT_HELP_DICT[data[2]], button)
    elif data[1] == "clone":
        buttons = ButtonMaker()
        buttons.data_button("Back", f"help back c {pg_no}")
        button = buttons.build_menu()
        await edit_message(message, CLONE_HELP_DICT[data[2]], button)


@new_task
async def bot_help(_, message):
    guide_url = await _get_help_url()
    button = _build_help_buttons(guide_url)
    await send_message(message, get_help_string(), button, photo="IMAGES")
