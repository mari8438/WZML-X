import os
import re
from html import unescape
from os import path as ospath
from json import loads as json_loads
from urllib.parse import urljoin

from httpx import AsyncClient

try:
    from ... import LOGGER
    from ...core.config_manager import Config
except (ImportError, ValueError):
    import logging
    LOGGER = logging.getLogger(__name__)
    class Config:
        BITPORN_URL = "https://bitporn.eu"
        BITPORN_ANNOUNCE_URL = "https://bitporn.eu/announce/21f0b107f9c57d8b4e080e8ffcb47199"
        BITPORN_COOKIE_FILE = ""


class BitpornError(RuntimeError):
    pass


def parse_netscape_cookies(source):
    cookies = {}
    if not source:
        return cookies
    text = ""
    if isinstance(source, dict):
        return {str(k): str(v) for k, v in source.items()}
    if isinstance(source, str):
        source_str = source.strip()
        if ospath.isfile(source_str):
            try:
                with open(source_str, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                return {}
        else:
            text = source_str
    else:
        return {}

    if not text:
        return cookies

    text = text.strip()
    if text.startswith(("{", "[")):
        try:
            payload = json_loads(text)
            if isinstance(payload, dict):
                return {str(k): str(v) for k, v in payload.items()}
            elif isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and item.get("name"):
                        cookies[str(item["name"])] = str(item.get("value", ""))
                return cookies
        except Exception:
            pass

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
        elif len(parts) == 2:
            cookies[parts[0]] = parts[1]
    return cookies


class BitpornClient:
    def __init__(self, base_url=None, cookie_file=None, announce_url=None):
        self.base_url = (base_url or getattr(Config, "BITPORN_URL", "https://bitporn.eu")).rstrip("/")
        self.cookie_file = cookie_file or getattr(Config, "BITPORN_COOKIE_FILE", "/usr/src/app/secrets/bitporn_cookies.txt")
        self.announce_url = announce_url or getattr(Config, "BITPORN_ANNOUNCE_URL", "")
        self.cookies = parse_netscape_cookies(self.cookie_file)
        self.client = None
        self._csrf_token = None

    async def __aenter__(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        self.client = AsyncClient(
            headers=headers,
            cookies=self.cookies,
            timeout=90,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _get_upload_form(self):
        upload_url = f"{self.base_url}/torrents/create"
        resp = await self.client.get(upload_url)
        resp.raise_for_status()

        if "/login" in str(resp.url):
            raise BitpornError("BitPorn session is not authenticated. Please check your cookies.")

        html = resp.text
        # Extract CSRF token
        token_m = re.search(r'<input[^>]+name=[\'"]_token[\'"][^>]+value=[\'"]([^\'"]+)[\'"]', html, re.I)
        if not token_m:
            token_m = re.search(r'name=[\'"]csrf-token[\'"]\s+content=[\'"]([^\'"]+)[\'"]', html, re.I)
        if not token_m:
            token_m = re.search(r'value=[\'"]([a-zA-Z0-9]{30,50})[\'"][^>]+name=[\'"]_token[\'"]', html, re.I)

        if token_m:
            self._csrf_token = token_m.group(1)

        # Extract announce URL
        ann_m = re.search(r'(https?://[^\s\'"<>]+/announce/[a-f0-9]{20,40})', html, re.I)
        if ann_m:
            self.announce_url = ann_m.group(1)

        return self._csrf_token, html

    async def upload_torrent(
        self,
        torrent_path,
        title,
        category_id="15",
        resolution_id="13",
        cover_url="",
        desc="",
        keywords="",
        anon=False,
    ):
        if not ospath.isfile(torrent_path):
            raise BitpornError(f"Torrent file not found: {torrent_path}")

        token, form_html = await self._get_upload_form()
        if not token:
            raise BitpornError("Could not extract CSRF token (_token) from BitPorn upload form")

        filename = ospath.basename(torrent_path)
        with open(torrent_path, "rb") as f:
            torrent_bytes = f.read()

        data = {
            "_token": token,
            "name": str(title).strip(),
            "category_id": str(category_id or "15").strip(),
            "type_id": "1",
            "resolution_id": str(resolution_id or "13").strip(),
            "cover_url": str(cover_url or "").strip(),
            "banner_url": "",
            "keywords": str(keywords or "").strip(),
            "anon": "1" if anon else "0",
            "personal_release": "0",
            "description": str(desc or "").strip(),
        }

        files = {
            "torrent": (filename, torrent_bytes, "application/x-bittorrent"),
        }

        upload_url = f"{self.base_url}/torrents"
        LOGGER.info("Uploading torrent to BitPorn: title=%r, category=%s, size=%d bytes", title, category_id, len(torrent_bytes))

        headers = {
            "Referer": f"{self.base_url}/torrents/create",
            "Origin": self.base_url,
        }

        resp = await self.client.post(
            upload_url,
            data=data,
            files=files,
            headers=headers,
        )
        resp_text = resp.text
        resp_url = str(resp.url)

        # Check for duplicate
        if "already been uploaded" in resp_text.lower() or "duplicate" in resp_text.lower():
            dupe_m = re.search(r'href=[\'"](?:https?://bitporn\.eu)?/(torrents/\d+)[\'"]', resp_text)
            dupe_url = urljoin(f"{self.base_url}/", dupe_m.group(1)) if dupe_m else ""
            raise BitpornError(f"Duplicate torrent on BitPorn: {dupe_url or 'already exists'}")

        # Check for validation errors
        err_m = re.search(r'<div[^>]+class=[\'"][^\'"]*alert alert-danger[^\'"]*[\'"][^>]*>(.*?)</div>', resp_text, re.DOTALL | re.I)
        if not err_m:
            err_m = re.search(r'<span[^>]+class=[\'"][^\'"]*invalid-feedback[^\'"]*[\'"][^>]*>(.*?)</span>', resp_text, re.DOTALL | re.I)
        if err_m:
            err_clean = re.sub(r"<[^>]+>", " ", unescape(err_m.group(1))).strip()
            raise BitpornError(f"BitPorn rejected upload: {err_clean}")

        # Extract torrent ID from redirected URL or page content
        torrent_id = None
        id_m = re.search(r"/torrents/(\d+)", resp_url)
        if id_m:
            torrent_id = id_m.group(1)
        else:
            id_m2 = re.search(r"/torrents/(\d+)", resp_text)
            if id_m2:
                torrent_id = id_m2.group(1)

        if not torrent_id:
            raise BitpornError(f"Upload completed but could not extract torrent ID (URL: {resp_url})")

        detail_url = f"{self.base_url}/torrents/{torrent_id}"
        LOGGER.info("BitPorn upload succeeded: ID=%s -> %s", torrent_id, detail_url)

        # Download accepted torrent file
        accepted_torrent_bytes = None
        dl_url = f"{self.base_url}/torrents/download/{torrent_id}"
        try:
            dl_resp = await self.client.get(dl_url)
            if dl_resp.status_code == 200 and len(dl_resp.content) > 100:
                accepted_torrent_bytes = dl_resp.content
                LOGGER.info("Downloaded accepted BitPorn torrent (%d bytes) from %s", len(accepted_torrent_bytes), dl_url)
        except Exception as dl_err:
            LOGGER.warning("Could not download accepted torrent from BitPorn: %s", dl_err)

        return {
            "torrent_id": torrent_id,
            "detail_url": detail_url,
            "accepted_bytes": accepted_torrent_bytes,
            "announce_url": self.announce_url,
        }
