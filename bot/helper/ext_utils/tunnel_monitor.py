from asyncio import sleep
from os import environ

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath

from ... import LOGGER, bot_loop
from ...core.config_manager import Config


TUNNEL_URL_FILE = environ.get("TUNNEL_URL_FILE", "/data/tunnel_url.txt")


async def _read_tunnel_url():
    try:
        if not await aiopath.isfile(TUNNEL_URL_FILE):
            return None
        async with aiopen(TUNNEL_URL_FILE, "r") as f:
            url = (await f.read()).strip()
        return url or None
    except Exception as e:
        LOGGER.warning(f"tunnel_monitor: read failed: {e}")
        return None


def _is_ephemeral_or_empty_base_url():
    if not Config.BASE_URL:
        return True
    if "trycloudflare.com" in Config.BASE_URL:
        return True
    return False


async def _sync_base_url_to_db(url):
    try:
        from .db_handler import database
        if database and getattr(database, "db", None) is not None:
            await database.db.settings.config.update_many(
                {},
                {"$set": {"BASE_URL": url}},
                upsert=False,
            )
    except Exception as e:
        LOGGER.warning(f"tunnel_monitor: failed to update DB BASE_URL: {e}")


async def _tunnel_monitor_loop():
    LOGGER.info("tunnel_monitor: started")
    while True:
        try:
            url = await _read_tunnel_url()
            if url and Config.BASE_URL != url:
                if _is_ephemeral_or_empty_base_url() or not Config.BASE_URL:
                    Config.BASE_URL = url
                    LOGGER.info(f"tunnel_monitor: updated BASE_URL = {url}")
                    await _sync_base_url_to_db(url)
        except Exception as e:
            LOGGER.error(f"tunnel_monitor: {e}")
        await sleep(5)


async def apply_tunnel_url_once():
    url = await _read_tunnel_url()
    if url:
        if _is_ephemeral_or_empty_base_url() or Config.BASE_URL != url:
            Config.BASE_URL = url
            LOGGER.info(f"tunnel_monitor: active BASE_URL = {url}")
            await _sync_base_url_to_db(url)
            return url
    if Config.BASE_URL:
        LOGGER.info(f"tunnel_monitor: permanent custom BASE_URL in use = {Config.BASE_URL}")
        return Config.BASE_URL
    return None


def start_tunnel_monitor():
    if not _is_ephemeral_or_empty_base_url() and not environ.get("TUNNEL_URL_FILE"):
        LOGGER.info("tunnel_monitor: permanent custom BASE_URL already set, background monitor disabled")
        return
    bot_loop.create_task(_tunnel_monitor_loop())
    LOGGER.info("tunnel_monitor: background monitor started")
