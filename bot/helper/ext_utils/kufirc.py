import os
import re
from html import unescape
from html.parser import HTMLParser
from os import path as ospath
from json import loads as json_loads
from urllib.parse import unquote, urljoin, urlsplit

from httpx import AsyncClient

try:
    from ... import LOGGER
    from ...core.config_manager import Config
except (ImportError, ValueError):
    import logging
    LOGGER = logging.getLogger(__name__)
    class Config:
        KUFIRC_URL = "https://kufirc.com"
        KUFIRC_ANNOUNCE_URL = "http://kufirc.com:7456/pcscvrlpzrlko1pgjnunto103sg0abn6/announce"
        KUFIRC_COOKIE_FILE = ""


class KufircError(RuntimeError):
    pass


class _HiddenFields(HTMLParser):
    def __init__(self, target_form_id=None):
        super().__init__()
        self.fields = {}
        self._target_form_id = target_form_id
        self._in_target = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "form":
            form_id = attrs_dict.get("id", "")
            form_enctype = attrs_dict.get("enctype", "").lower()
            if self._target_form_id:
                self._in_target = (form_id == self._target_form_id)
            else:
                self._in_target = ("multipart/form-data" in form_enctype or form_id == "upload_table")
        elif tag == "input":
            if attrs_dict.get("type", "").lower() == "hidden" and attrs_dict.get("name"):
                name = attrs_dict["name"]
                value = attrs_dict.get("value", "")
                if self._target_form_id:
                    if self._in_target:
                        self.fields[name] = value
                elif self._in_target or not self.fields:
                    self.fields[name] = value

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_target = False


def _parse_kufirc_cookies(source):
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
            name, value = parts[5], parts[6]
            cookies[name] = value
    return cookies


class KufircClient:
    def __init__(self, base_url=None, cookie_file=None, announce_url=None):
        self.base_url = (base_url or getattr(Config, "KUFIRC_URL", "") or "https://kufirc.com").rstrip("/")
        self.announce_url = announce_url or getattr(Config, "KUFIRC_ANNOUNCE_URL", "") or "http://kufirc.com:7456/pcscvrlpzrlko1pgjnunto103sg0abn6/announce"
        self.cookie_file = cookie_file or getattr(Config, "KUFIRC_COOKIE_FILE", "") or "/usr/src/app/secrets/kufirc_cookies.txt"
        self.cookies = _parse_kufirc_cookies(self.cookie_file)
        self.authkey = ""
        self.passkey = ""
        self.client = None

    async def __aenter__(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
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
        upload_url = f"{self.base_url}/upload.php"
        resp = await self.client.get(upload_url)
        resp.raise_for_status()

        if "/login" in str(resp.url) or 'name="username"' in resp.text:
            raise KufircError("Kufirc session is not authenticated. Please provide valid cookies.")

        html = resp.text
        # Extract authkey and passkey from page links
        authkey_m = re.search(r"authkey=([a-f0-9]{32,64})", html)
        if authkey_m:
            self.authkey = authkey_m.group(1)
        passkey_m = re.search(r"passkey=([a-z0-9]{20,40})", html) or re.search(r"torrent_pass=([a-z0-9]{20,40})", html)
        if passkey_m:
            self.passkey = passkey_m.group(1)

        parser = _HiddenFields(target_form_id="upload_table")
        parser.feed(html)
        fields = dict(parser.fields)
        return fields, html

    async def upload_torrent(
        self,
        torrent_path,
        title,
        category="13",
        tags="",
        image="",
        desc="",
        anonymous=False,
    ):
        if not ospath.isfile(torrent_path):
            raise KufircError(f"Torrent file not found: {torrent_path}")

        fields, form_html = await self._get_upload_form()
        auth_token = fields.get("auth")
        if not auth_token:
            auth_token_m = re.search(r'name=[\'"]auth[\'"]\s+value=[\'"]([a-f0-9]+)[\'"]', form_html)
            auth_token = auth_token_m.group(1) if auth_token_m else ""

        if not auth_token:
            raise KufircError("Could not extract auth token from Kufirc upload form")

        cat_val = str(category or "13").strip()
        data = {
            "submit": "true",
            "auth": auth_token,
            "MAX_FILE_SIZE": "2097152",
            "category": cat_val,
            "title": str(title).strip(),
            "taglist": str(tags).strip().lower(),
            "image": str(image).strip(),
            "desc": str(desc).strip(),
            "anonymous": "1" if anonymous else "0",
        }

        filename = ospath.basename(torrent_path)
        with open(torrent_path, "rb") as f:
            torrent_bytes = f.read()

        files = {
            "file_input": (filename, torrent_bytes, "application/x-bittorrent"),
        }

        upload_url = f"{self.base_url}/upload.php"
        LOGGER.info("Uploading torrent to Kufirc: title=%r, category=%s, size=%d bytes", title, cat_val, len(torrent_bytes))

        resp = await self.client.post(
            upload_url,
            data=data,
            files=files,
            headers={"Referer": upload_url},
        )
        resp_text = resp.text
        resp_url = str(resp.url)

        # Check for duplicate
        if "The exact same torrent file already exists" in resp_text:
            dupe_m = re.search(r'href=[\'"](?:https?://kufirc\.com)?/(torrents\.php\?id=\d+)', resp_text)
            dupe_url = urljoin(f"{self.base_url}/", dupe_m.group(1)) if dupe_m else ""
            raise KufircError(f"Duplicate torrent on Kufirc: {dupe_url or 'already exists'}")

        # Check for errors in HTML
        err_m = re.search(r'<p[^>]+style=[\'"][^\'"]*color:\s*red[^\'"]*[\'"][^>]*>(.*?)</p>', resp_text, re.DOTALL | re.I)
        if not err_m:
            err_m = re.search(r'<div[^>]+class=[\'"][^\'"]*messagebar\s+error[^\'"]*[\'"][^>]*>(.*?)</div>', resp_text, re.DOTALL | re.I)
        if err_m:
            err_clean = re.sub(r"<[^>]+>", " ", unescape(err_m.group(1))).strip()
            raise KufircError(f"Kufirc rejected upload: {err_clean}")

        # Check if redirected to torrent detail page
        torrent_id = None
        id_m = re.search(r"torrents\.php\?id=(\d+)", resp_url)
        if id_m:
            torrent_id = id_m.group(1)
        else:
            id_m2 = re.search(r"torrents\.php\?id=(\d+)", resp_text)
            if id_m2:
                torrent_id = id_m2.group(1)

        if not torrent_id:
            raise KufircError(f"Upload completed but could not find torrent ID in response (URL: {resp_url})")

        detail_url = f"{self.base_url}/torrents.php?id={torrent_id}"
        LOGGER.info("Kufirc upload succeeded: ID=%s -> %s", torrent_id, detail_url)

        # Download accepted torrent file
        accepted_torrent_bytes = None
        if self.authkey and self.passkey:
            dl_url = f"{self.base_url}/torrents.php?action=download&id={torrent_id}&authkey={self.authkey}&torrent_pass={self.passkey}"
            try:
                dl_resp = await self.client.get(dl_url)
                if dl_resp.status_code == 200 and len(dl_resp.content) > 100:
                    accepted_torrent_bytes = dl_resp.content
                    LOGGER.info("Downloaded accepted torrent from Kufirc (%d bytes)", len(accepted_torrent_bytes))
            except Exception as dl_err:
                LOGGER.warning("Could not download accepted torrent: %s", dl_err)

        return {
            "torrent_id": torrent_id,
            "detail_url": detail_url,
            "accepted_bytes": accepted_torrent_bytes,
            "authkey": self.authkey,
            "passkey": self.passkey,
        }
