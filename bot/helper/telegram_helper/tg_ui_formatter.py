import logging

_LOGGER = logging.getLogger(__name__)

PREMIUM_MOVIE = "5287575882108522821"
PREMIUM_PACKAGE = "5463172695132745432"
PREMIUM_TIMER = "5402406965252989103"
PREMIUM_BOLT = "5201873494798776013"
PREMIUM_USER = "5384085485171778811"
PREMIUM_GEAR = "5267334530171169409"
PREMIUM_STATS = "5231200819986047254"
PREMIUM_DOC = "5235630047959727475"
PREMIUM_CHECK = "5244837092042750681"
PREMIUM_SCREEN = "5375099322666859339"
PREMIUM_PUZZLE = "5400044690290522089"
PREMIUM_TRAFFIC = "5249381781622247862"
PREMIUM_STAR = "5208801655004350721"
PREMIUM_INBOX = "5463172695132745432"
PREMIUM_CLOUD = "5474171462132714933"


def is_premium_session(client=None) -> bool:
    """
    Check if the current sender has Telegram Premium privileges.
    Standard bot sessions cannot render custom <tg-emoji> without errors/fallback,
    so this strictly verifies active Premium user privileges.
    """
    if client is not None:
        me = getattr(client, "me", None)
        if me is not None:
            if getattr(me, "is_bot", False):
                return False
            return bool(getattr(me, "is_premium", False))
        return bool(getattr(client, "is_premium", False))

    try:
        from ...core.tg_client import TgClient

        if getattr(TgClient, "IS_PREMIUM_USER", False):
            return True
        user_client = getattr(TgClient, "user", None)
        if user_client and getattr(getattr(user_client, "me", None), "is_premium", False):
            return True
    except Exception:
        pass
    return False


def safe_emoji(premium_emoji_id, fallback_unicode, client=None) -> str:
    """
    Output <tg-emoji id="{premium_emoji_id}">{fallback_unicode}</tg-emoji>
    if client is verified Premium, otherwise strictly output fallback_unicode.
    """
    if premium_emoji_id and is_premium_session(client):
        return f'<tg-emoji id="{premium_emoji_id}">{fallback_unicode}</tg-emoji>'
    return str(fallback_unicode)


def format_accordion_block(title, subtitle=None, items=None, curved=True) -> str:
    """
    Format a Telegram Article expandable accordion block.
    The first line MUST be the drawer title so the title remains visible
    next to the dropdown arrow (⌄) when collapsed.
    """
    drawer = f"<b>{title}</b>"
    if subtitle:
        drawer += f" · {subtitle}"

    lines = [drawer]
    items_list = list(items.items()) if isinstance(items, dict) else list(items or [])
    total = len(items_list)
    for i, item in enumerate(items_list):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            k, v = item
            if total == 1:
                prefix = "•"
            elif i == 0:
                prefix = "╭" if curved else "┌"
            elif i == total - 1:
                prefix = "╰" if curved else "└"
            else:
                prefix = "├"
            lines.append(f"{prefix} <b>{k}:</b> {v}")
        else:
            lines.append(str(item))

    body = "\n".join(lines)
    return f"<blockquote expandable>{body}</blockquote>"


def format_card(header=None, title=None, fields_dict=None, footer=None, expandable=True) -> str:
    """
    Render a clean, modern card using Telegram's <blockquote expandable>.
    """
    tag_open = "<blockquote expandable>" if expandable else "<blockquote>"
    tag_close = "</blockquote>"

    lines = []
    if title:
        lines.append(f"<b>{title}</b>")

    items = list(fields_dict.items()) if isinstance(fields_dict, dict) else list(fields_dict or [])
    if items:
        if title:
            lines.append("")
        total = len(items)
        for i, (k, v) in enumerate(items):
            if total == 1:
                prefix = "•"
            elif i == 0:
                prefix = "┌"
            elif i == total - 1:
                prefix = "└"
            else:
                prefix = "├"
            lines.append(f"{prefix} <b>{k}</b> : {v}")

    if footer:
        if lines:
            lines.append("")
        lines.append(f"<i>{footer}</i>")

    card_body = "\n".join(lines)
    card_html = f"{tag_open}\n{card_body}\n{tag_close}"

    if header:
        return f"<b>{header}</b>\n\n{card_html}"
    return card_html


async def publish_telegraph_article(title, html_content) -> str | None:
    """
    Publish an Instant View article to graph.org / telegra.ph.
    Returns the public page URL or None on error.
    """
    try:
        try:
            from ..ext_utils.telegraph_helper import telegraph
        except (ImportError, ValueError):
            from bot.helper.ext_utils.telegraph_helper import telegraph

        if not getattr(telegraph._telegraph, "access_token", None):
            await telegraph.create_account()
        page = await telegraph.create_page(title=title, content=html_content)
        if isinstance(page, dict):
            return page.get("url") or f"https://graph.org/{page.get('path')}"
        return None
    except Exception as e:
        _LOGGER.error(f"Failed to publish telegraph article '{title}': {e}")
        return None
