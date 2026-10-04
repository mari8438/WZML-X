import re
from html.parser import HTMLParser
from os import path as ospath
from json import load as json_load
from urllib.parse import urljoin

from httpx import AsyncClient


class _HiddenFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.forms = []
        self._form = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._form = attrs
            self.forms.append(attrs)
        elif tag == "input" and self._form is not None:
            if attrs.get("type", "").lower() == "hidden" and attrs.get("name"):
                self.fields[attrs["name"]] = attrs.get("value", "")


class HappyFappyError(RuntimeError):
    pass


def _cookie_file(path):
    cookies = {}
    if not path or not ospath.isfile(path):
        return cookies
    try:
        with open(path, encoding="utf-8", errors="ignore") as cookie_file:
            for raw_line in cookie_file:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain, name, value = parts[0].lower(), parts[5], parts[6]
                    if "happyfappy.net" in domain:
                        cookies[name] = value
                elif line.startswith("{"):
                    cookie_file.seek(0)
                    payload = json_load(cookie_file)
                    for item in payload if isinstance(payload, list) else []:
                        domain = str(item.get("domain", "")).lower()
                        if "happyfappy.net" in domain and item.get("name"):
                            cookies[item["name"]] = item.get("value", "")
                    break
    except (OSError, TypeError, ValueError):
        return {}
    return cookies


class HappyFappyClient:
    def __init__(self, base_url, username, password, cookie_file="", timeout=60):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            cookies=_cookie_file(cookie_file),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"},
        )

    async def close(self):
        await self.client.aclose()

    async def _get_form(self, url):
        response = await self.client.get(url)
        response.raise_for_status()
        parser = _HiddenFields()
        parser.feed(response.text)
        return response, parser

    async def login(self, login_path="/login"):
        upload_response, _ = await self._get_form(f"{self.base_url}/upload.php")
        if "/login" not in str(upload_response.url) and 'name="username"' not in upload_response.text:
            return
        login_url = urljoin(f"{self.base_url}/", login_path.lstrip("/"))
        response, parser = await self._get_form(login_url)
        if "/logout" in response.text:
            return
        data = dict(parser.fields)
        data.update(
            {
                "username": self.username,
                "password": self.password,
                "cinfo": "auth",
                "submit": "login",
            }
        )
        response = await self.client.post(login_url, data=data)
        response.raise_for_status()
        upload_response, _ = await self._get_form(f"{self.base_url}/upload.php")
        if "/login" in str(upload_response.url) or 'name="username"' in upload_response.text:
            raise HappyFappyError("HappyFappy login failed or requires CAPTCHA/2FA")

    async def _upload_form(self):
        response, parser = await self._get_form(f"{self.base_url}/upload.php")
        if "/login" in str(response.url) or 'name="username"' in response.text:
            raise HappyFappyError("HappyFappy session is not authenticated")
        self._upload_html = response.text
        return parser.fields

    def _category_value(self, category):
        value = str(category or "").strip()
        if not value or value.isdigit():
            return value
        match = re.search(r'<select[^>]+id=["\']category["\'][^>]*>(.*?)</select>', self._upload_html, re.I | re.S)
        if match:
            options = re.findall(
                r'<option[^>]+value=["\']([^"\']+)["\'][^>]*>(.*?)</option>',
                match.group(1),
                re.I | re.S,
            )
            normalized = re.sub(r"\s+", " ", value).strip().casefold()
            for option_value, label in options:
                if re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", label)).strip().casefold() == normalized:
                    return option_value
        raise HappyFappyError(f"Unknown HappyFappy category: {value}")

    async def check_dupe(self, torrent_path):
        fields = await self._upload_form()
        fields.update({"checkonly": "Check for dupes"})
        with open(torrent_path, "rb") as torrent:
            response = await self.client.post(
                f"{self.base_url}/upload.php",
                data=fields,
                files={"file_input": (ospath.basename(torrent_path), torrent, "application/x-bittorrent")},
            )
        response.raise_for_status()
        text = response.text
        positive = bool(re.search(r"dupe|duplicate|already exists|match", text, re.I))
        return positive, text

    async def submit(
        self,
        torrent_path,
        title,
        tags,
        image_url,
        description,
        category="",
        anonymous=False,
    ):
        fields = await self._upload_form()
        fields.update(
            {
                "submit": "true",
                "category": self._category_value(category),
                "title": title,
                "taglist": tags,
                "image": image_url,
                "desc": description,
                "anonymous": "1" if anonymous else "0",
            }
        )
        with open(torrent_path, "rb") as torrent:
            response = await self.client.post(
                f"{self.base_url}/upload.php",
                data=fields,
                files={"file_input": (ospath.basename(torrent_path), torrent, "application/x-bittorrent")},
            )
        response.raise_for_status()
        success_marker = bool(
            re.search(
                r"(?:your\s+)?torrent\s+(?:has\s+been\s+)?(?:uploaded|added)|"
                r"upload\s+successful|thanks\s+for\s+uploading",
                response.text,
                re.I,
            )
            or re.search(r"class=[\"'][^\"']*(?:success|alert-success|successbox)", response.text, re.I)
        )
        if "/torrents.php" not in str(response.url) and not success_marker:
            visible = re.sub(r"<[^>]+>", " ", response.text)
            visible = re.sub(r"\s+", " ", visible).strip()
            hints = re.findall(
                r"[^.]{0,80}(?:error|invalid|must|required|not allowed|rejected|failed|warning)[^.]{0,180}",
                visible,
                re.I,
            )
            detail = " | ".join(dict.fromkeys(item.strip() for item in hints))[:700]
            suffix = f": {detail}" if detail else ""
            raise HappyFappyError(
                f"HappyFappy did not confirm the torrent upload "
                f"(HTTP {response.status_code}, final URL {response.url}){suffix}"
            )
        return str(response.url)
