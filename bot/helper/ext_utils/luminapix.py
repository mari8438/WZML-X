import os
from os import path as ospath
import re
from urllib.parse import urlsplit

from httpx import AsyncClient

try:
    from ... import LOGGER
    from ...core.config_manager import Config
except (ImportError, ValueError):
    import logging
    LOGGER = logging.getLogger(__name__)
    class Config:
        LUMINAPIX_API_KEY = ""


LUMINAPIX_API_URL = "https://luminapix.eu/api/1/upload/"
DEFAULT_API_KEY = "chv_3v0_c477470feba69a79b75b31ac33bd2456d5a4d29e0adc9713af2ed616af13eb6b_ab4a85a93de3f5658c3a24ef5037deea0655b772bfaf33780c84b0402a74506b"
MAX_IMAGE_BYTES = 50 * 1024 * 1024


class LuminapixError(RuntimeError):
    pass


def validate_luminapix_url(value):
    url_str = str(value or "").strip()
    parsed = urlsplit(url_str)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (host == "luminapix.eu" or host.endswith(".luminapix.eu"))
    ):
        raise LuminapixError(f"LuminaPix returned an unexpected URL host: {url_str}")
    return url_str


async def upload_image_details(path, client=None, api_key=None):
    if not ospath.isfile(path):
        raise LuminapixError(f"Image file does not exist: {path}")

    size = ospath.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise LuminapixError(f"Image exceeds the LuminaPix limit: {ospath.basename(path)} ({size} bytes)")

    api_key = api_key or getattr(Config, "LUMINAPIX_API_KEY", "") or DEFAULT_API_KEY
    if not api_key:
        raise LuminapixError("LuminaPix API key is not configured")

    owns_client = client is None
    client = client or AsyncClient(timeout=120)

    filename = ospath.basename(path)
    ext = ospath.splitext(path)[1].lower()
    mime_type = "image/webp" if ext == ".webp" else ("image/png" if ext == ".png" else "image/jpeg")

    upload_url = f"{LUMINAPIX_API_URL}?key={api_key}"

    try:
        with open(path, "rb") as image_file:
            files = {
                "source": (filename, image_file, mime_type),
            }
            data = {
                "format": "json",
            }
            response = await client.post(
                upload_url,
                files=files,
                data=data,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                },
            )

        if response.status_code != 200:
            try:
                err_payload = response.json()
                msg = err_payload.get("error", {}).get("message") or response.text[:200]
            except Exception:
                msg = response.text[:200]
            raise LuminapixError(f"LuminaPix upload error (HTTP {response.status_code}): {msg}")

        payload = response.json()
        img_data = payload.get("image", {})
        if not img_data:
            raise LuminapixError(f"LuminaPix returned unexpected JSON structure: {payload}")

        direct_url = img_data.get("display_url") or img_data.get("url")
        thumb_url = img_data.get("thumb", {}).get("url") or direct_url
        page_url = img_data.get("url_viewer") or img_data.get("url_short") or direct_url

        direct_url = validate_luminapix_url(direct_url)
        thumb_url = validate_luminapix_url(thumb_url)

        LOGGER.info("LuminaPix upload successful for %s -> %s", filename, direct_url)
        return {
            "thumbnail": thumb_url,
            "direct": direct_url,
            "page": page_url,
        }
    except LuminapixError:
        raise
    except Exception as exc:
        raise LuminapixError(f"LuminaPix upload failed for {filename}: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()


async def upload_image(path, client=None, api_key=None):
    details = await upload_image_details(path, client=client, api_key=api_key)
    return details["direct"]
