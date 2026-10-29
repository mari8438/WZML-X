# ruff: noqa: E402
"""
Anime Starfall • Telegram `/web` Command (`bot/modules/web_auth.py`)
Generates the user's private one-click magic link and provides interactive
Dual-Mode (`telegram` vs `web`) switching.
"""

from pyrogram.enums import ButtonStyle

from .. import user_data
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task, update_user_ldata
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.token_manager import generate_user_token, resolve_base_url
from ..helper.ext_utils.web_bridge import export_users_snapshot
from ..helper.mirror_leech import is_web_task_mode
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import edit_message, send_message


def get_user_task_mode(user_id: int) -> str:
    """Return 'web' or 'telegram' for `user_id`."""
    return "web" if is_web_task_mode(user_id) else "telegram"


async def set_user_task_mode(user_id: int, mode: str) -> str:
    """Persist `user_data[user_id]['TASK_MODE']` in RAM, snapshot, and MongoDB."""
    normalized = "web" if str(mode).strip().lower() == "web" else "telegram"
    update_user_ldata(user_id, "TASK_MODE", normalized)
    export_users_snapshot(user_data, getattr(Config, "LEECH_DUMP_CHAT", None))
    if Config.DATABASE_URL:
        await database.update_user_data(user_id)
    return normalized


def build_web_auth_card(user_id: int):
    """Build the Starfall Web Studio magic link card and inline button menu."""
    token = generate_user_token(user_id, expiry_days=7)
    base_url = resolve_base_url()
    studio_url = f"{base_url}/app/user?token={token}"
    dashboard_url = f"{base_url}/app/dashboard?token={token}"

    current_mode = get_user_task_mode(user_id)
    next_mode = "telegram" if current_mode == "web" else "web"
    toggle_text = (
        "📲 Switch to Telegram Mode"
        if current_mode == "web"
        else "🌐 Switch to Web Studio Mode"
    )

    text = (
        "✨ <b>Anime Starfall • Web Studio</b>\n\n"
        "Your private, one-click access link is ready:\n\n"
        f"🎛 <b>Current Mode:</b> <code>{current_mode.upper()}</code>\n"
        "⚠️ Never share this link with anyone else."
    )

    buttons = ButtonMaker()
    buttons.url_button(
        "🌐 Open Starfall Studio",
        studio_url,
        position="header",
        style=ButtonStyle.PRIMARY,
    )
    buttons.url_button(
        "📊 Manage in Studio Dashboard",
        dashboard_url,
        position="default",
    )
    buttons.data_button(
        toggle_text,
        f"webstudio {user_id} mode {next_mode}",
        position="default",
        style=ButtonStyle.SUCCESS if next_mode == "web" else ButtonStyle.PRIMARY,
    )
    buttons.data_button(
        "🔄 Refresh Link",
        f"webstudio {user_id} refresh",
        position="footer",
    )
    buttons.data_button(
        "✖ Close",
        f"webstudio {user_id} close",
        position="footer",
        style=ButtonStyle.DANGER,
    )
    return text, buttons.build_menu(1)


@new_task
async def web_command(_, message):
    """Handle `/web` command."""
    user = message.from_user or message.sender_chat
    if not user:
        return
    text, reply_markup = build_web_auth_card(user.id)
    await send_message(message, text, reply_markup)


@new_task
async def web_studio_callback(_, query):
    """Handle inline buttons on the `/web` card."""
    user_id = query.from_user.id
    parts = query.data.split()
    if len(parts) < 3 or int(parts[1]) != user_id:
        await query.answer("This Web Studio link belongs to another user!", show_alert=True)
        return

    action = parts[2]
    if action == "close":
        await query.answer("Closed")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "mode" and len(parts) >= 4:
        new_mode = await set_user_task_mode(user_id, parts[3])
        await query.answer(f"Task Mode switched to {new_mode.upper()}!", show_alert=True)
    elif action == "refresh":
        await query.answer("Generated a fresh 7-day magic link!")
    else:
        await query.answer()

    text, reply_markup = build_web_auth_card(user_id)
    await edit_message(query.message, text, reply_markup)
