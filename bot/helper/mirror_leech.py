# ruff: noqa: E402
"""
Anime Starfall • Dual-Mode Leech Controller (`bot/helper/mirror_leech.py`)
Implements the Dual-Mode (`TASK_MODE == "web"` vs `"telegram"`) messaging and
silent dump channel routing contract:
  1. Start:  `🚀 Task Initiated: {name}\n[ 🌐 Manage in Studio ]({BASE_URL}/app/dashboard?token={TOKEN})`
  2. Finish: Silently uploads completed media directly to the user's destination dump channel,
             followed by `✅ Finished: {name}` in user chat.
"""

from html import escape

from .. import LOGGER, bot_loop, user_data
from ..core.config_manager import Config
from .ext_utils.token_manager import generate_user_token, resolve_base_url
from .ext_utils.web_bridge import read_user_snapshot
from .telegram_helper.bot_commands import BotCommands
from .telegram_helper.button_build import ButtonMaker
from .telegram_helper.message_utils import send_message


def is_web_task_mode(user_id: int, message=None) -> bool:
    """
    Determine whether `user_id` (or `message`) has Web Studio Mode (`TASK_MODE == "web"`) active.
    Checks both in-memory `user_data` and the shared cross-process snapshot.
    """
    if message is not None and getattr(message, "_web_task_mode", False):
        return True
    u_dict = user_data.get(user_id, {})
    if "TASK_MODE" in u_dict:
        return str(u_dict["TASK_MODE"]).strip().lower() == "web"
    snap = read_user_snapshot(user_id)
    if "mode" in snap:
        return str(snap["mode"]).strip().lower() == "web"
    return False


def build_web_dashboard_url(user_id: int) -> str:
    """Return `{BASE_URL}/app/dashboard?token={TOKEN}` for `user_id`."""
    token = generate_user_token(user_id, expiry_days=7)
    return f"{resolve_base_url()}/app/dashboard?token={token}"


async def send_web_task_initiated_message(message, user_id: int, task_name: str):
    """
    Post the compact Web Mode Start message:
    `🚀 Task Initiated: {name}\n[ 🌐 Manage in Studio ]({BASE_URL}/app/dashboard?token={TOKEN})`
    """
    clean_name = (task_name or "Media Task").strip()
    dashboard_url = build_web_dashboard_url(user_id)
    text = (
        f"🚀 Task Initiated: <code>{escape(clean_name)}</code>\n"
        f"<a href='{dashboard_url}'>[ 🌐 Manage in Studio ]</a>"
    )
    buttons = ButtonMaker()
    buttons.url_button("🌐 Manage in Studio", dashboard_url)
    return await send_message(message, text, buttons.build_menu(1))


async def send_web_task_finished_message(message, task_name: str):
    """
    Post the compact Web Mode Finish message in the user's chat after silent dump upload:
    `✅ Finished: {name}`
    """
    clean_name = (task_name or "Media Task").strip()
    return await send_message(message, f"✅ Finished: {escape(clean_name)}")


async def start_web_leech_task(
    user_id: int,
    url: str,
    mode: str = "leech",
    engine: str = "auto",
    custom_name: str = "",
    select: bool = False,
    extract: bool = False,
    compress: bool = False,
    up_dest: str = "",
):
    """
    Dispatch a direct leech or mirror task from `/api/leech/start` into the bot's
    `Mirror` engine under the authenticated user's session.
    """
    from ..core.tg_client import TgClient
    from ..modules.mirror_leech import Mirror

    if TgClient.bot is None:
        raise RuntimeError("TgClient.bot is not initialized")

    is_leech = str(mode).lower() != "mirror"
    eng = str(engine).lower()
    is_qbit = eng in {"qbit", "qbittorrent"}
    is_nzb = eng in {"nzb", "sabnzbd"}
    is_jd = eng in {"jd", "jdownloader"}

    base_cmd = (
        BotCommands.LeechCommand[0]
        if isinstance(BotCommands.LeechCommand, list)
        else BotCommands.LeechCommand
    ) if is_leech else (
        BotCommands.MirrorCommand[0]
        if isinstance(BotCommands.MirrorCommand, list)
        else BotCommands.MirrorCommand
    )

    cmd_parts = [f"/{base_cmd}", url.strip()]
    if custom_name:
        cmd_parts.extend(["-n", custom_name.strip()])
    if select:
        cmd_parts.append("-s")
    if extract:
        cmd_parts.append("-e")
    if compress:
        cmd_parts.append("-z")
    if up_dest:
        cmd_parts.extend(["-up", up_dest.strip()])

    cmd_text = " ".join(cmd_parts)
    user_obj = await TgClient.bot.get_users(user_id)

    # Create a real Telegram message in the user's PM to anchor TaskListener
    task_label = custom_name.strip() or url.strip().split("/")[-1][:64] or "Web Studio Task"
    dashboard_url = build_web_dashboard_url(user_id)
    start_btns = ButtonMaker()
    start_btns.url_button("🌐 Manage in Studio", dashboard_url)

    anchor_msg = await TgClient.bot.send_message(
        chat_id=user_id,
        text=(
            f"🚀 Task Initiated: <code>{escape(task_label)}</code>\n"
            f"<a href='{dashboard_url}'>[ 🌐 Manage in Studio ]</a>"
        ),
        reply_markup=start_btns.build_menu(1),
        disable_web_page_preview=True,
    )
    anchor_msg.text = cmd_text
    anchor_msg.from_user = user_obj
    setattr(anchor_msg, "_web_task_mode", True)
    setattr(anchor_msg, "_web_start_card_sent", True)

    listener = Mirror(
        TgClient.bot,
        anchor_msg,
        is_qbit=is_qbit,
        is_leech=is_leech,
        is_jd=is_jd,
        is_nzb=is_nzb,
    )
    bot_loop.create_task(listener.new_event())
    LOGGER.info(f"Started Web Studio {mode} task for user {user_id}: {url}")
    return {
        "mid": anchor_msg.id,
        "user_id": user_id,
        "command": cmd_text,
        "mode": "leech" if is_leech else "mirror",
    }
