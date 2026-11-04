import aiohttp
import html

from .. import LOGGER
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.message_utils import send_message, edit_message

AGY_DAEMON_URL = "http://172.18.0.1:18888/execute"


@new_task
async def handle_agy_prompt(_, message):
    args = message.text.split(maxsplit=1)
    if len(args) == 1:
        await send_message(
            message,
            "<blockquote><b>⚡ STARFALLX AGY PROMPT</b>\n"
            "<i>No prompt provided!</i>\n\n"
            "<b>Usage:</b> <code>/prompt &lt;instructions for AGY to execute&gt;</code>\n"
            "<b>Alias:</b> <code>/agy &lt;instructions&gt;</code>\n"
            "<b>Example:</b> <code>/prompt add direct link support for toonworld and xdmovies</code></blockquote>",
        )
        return

    prompt_text = args[1].strip()
    safe_prompt = html.escape(prompt_text)

    status_msg = await send_message(
        message,
        f"<blockquote><b>⚡ STARFALLX AGY • TASK DISPATCHED</b>\n"
        f"<b>Task:</b> <code>{safe_prompt[:200]}</code>\n"
        f"<b>Engine:</b> Antigravity CLI (Host Agent)\n"
        f"<b>Status:</b> ⏳ <i>Sending to VPS background daemon...</i></blockquote>",
    )

    user_name = "User"
    user_id = 0
    if message.from_user:
        user_name = message.from_user.first_name or message.from_user.username or "User"
        user_id = message.from_user.id

    payload = {
        "prompt": prompt_text,
        "chat_id": message.chat.id,
        "message_id": status_msg.id if status_msg else None,
        "reply_to_message_id": message.id,
        "user_id": user_id,
        "user_name": user_name,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                AGY_DAEMON_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    LOGGER.info(f"AGY task successfully handed off to host daemon for chat {message.chat.id}")
                else:
                    err_txt = await resp.text()
                    await edit_message(
                        status_msg,
                        f"<blockquote><b>⚠️ AGY DISPATCH FAILED</b>\n"
                        f"<b>Status:</b> HTTP {resp.status}\n"
                        f"<code>{html.escape(err_txt[:300])}</code></blockquote>",
                    )
    except Exception as e:
        LOGGER.error(f"Failed to connect to host AGY daemon: {e}")
        await edit_message(
            status_msg,
            f"<blockquote><b>⚠️ AGY DAEMON OFFLINE</b>\n"
            f"Could not connect to host daemon at <code>{AGY_DAEMON_URL}</code>.\n"
            f"<b>Error:</b> <code>{html.escape(str(e))}</code></blockquote>",
        )
