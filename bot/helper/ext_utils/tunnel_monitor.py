from ... import LOGGER
from ...core.config_manager import Config

STATIC_DEFAULT_URL = "http://172.105.52.160:8080"


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


async def apply_tunnel_url_once():
    # If BASE_URL is empty or an ephemeral trycloudflare domain, enforce direct host IP:8080
    if not Config.BASE_URL or "trycloudflare.com" in Config.BASE_URL:
        Config.BASE_URL = STATIC_DEFAULT_URL
        LOGGER.info(f"tunnel_monitor: enforced direct host BASE_URL = {Config.BASE_URL}")
        await _sync_base_url_to_db(Config.BASE_URL)
        return Config.BASE_URL

    LOGGER.info(f"tunnel_monitor: permanent BASE_URL in use = {Config.BASE_URL}")
    return Config.BASE_URL


def start_tunnel_monitor():
    LOGGER.info("tunnel_monitor: direct IP mode active, background tunnel monitor disabled")
