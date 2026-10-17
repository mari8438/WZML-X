import re
import json
from html import unescape
from html.parser import HTMLParser
from os import path as ospath
from json import load as json_load, loads as json_loads
from urllib.parse import unquote, urljoin, urlsplit

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


def _parse_cookies(source):
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
    # Check JSON
    if text.startswith(("{", "[")):
        try:
            payload = json_loads(text)
            if isinstance(payload, dict):
                return {str(k): str(v) for k, v in payload.items()}
            elif isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and item.get("name"):
                        domain = str(item.get("domain", "")).lower()
                        if not domain or "happyfappy.net" in domain:
                            cookies[str(item["name"])] = str(item.get("value", ""))
                return cookies
        except Exception:
            pass

    # Check Netscape format (tab-separated lines)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domain, name, value = parts[0].lower(), parts[5], parts[6]
            if not domain or "happyfappy.net" in domain:
                cookies[name] = value

    if cookies:
        return cookies

    # Check key=value format (semicolon, newline, or space separated, e.g. "sid=XYZ; cid=ABC")
    for match in re.finditer(r"([a-zA-Z0-9_\-\.]+)\s*=\s*([^;\r\n]+)", text):
        name = match.group(1).strip()
        value = match.group(2).strip()
        if name and value:
            cookies[name] = value

    return cookies


_cookie_file = _parse_cookies


class HappyFappyClient:
    def __init__(self, base_url, username, password, cookie_file="", timeout=60):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            cookies=_parse_cookies(cookie_file),
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"},
        )
        self.uploaded_torrent_url = ""
        self.upload_details = {
            "accepted": False,
            "torrent_id": "",
            "download_url": "",
            "warnings": [],
            "validation_errors": [],
        }

    async def close(self):
        await self.client.aclose()

    def _flash_message(self):
        try:
            raw = self.client.cookies.get("flashes", "")
            payload = json_loads(unquote(raw))
            messages = [str(item.get("message", "")).strip() for item in payload if isinstance(item, dict)]
            return " | ".join(item for item in messages if item)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""

    async def _get_form(self, url):
        response = await self.client.get(url)
        response.raise_for_status()
        visible_response = re.sub(r"<[^>]+>", " ", unescape(response.text))
        visible_response = re.sub(r"\s+", " ", visible_response).strip()
        self.upload_details["warnings"] = re.findall(
            r"[^.]{0,100}\bwarning\b[^.]{0,240}", visible_response, re.I
        )[:10]
        self.upload_details["validation_errors"] = re.findall(
            r"[^.]{0,100}\b(?:invalid|required|must|not allowed|rejected|failed)\b[^.]{0,240}",
            visible_response,
            re.I,
        )[:10]
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
        try:
            upload_response, _ = await self._get_form(f"{self.base_url}/upload.php")
        except Exception as error:
            flash = self._flash_message()
            if flash:
                raise HappyFappyError(f"HappyFappy login rejected: {flash}") from error
            raise HappyFappyError(
                "HappyFappy login could not be completed; the site rejected this client or requires browser verification"
            ) from error
        if "/login" in str(upload_response.url) or 'name="username"' in upload_response.text:
            flash = self._flash_message()
            detail = f": {flash}" if flash else " or requires CAPTCHA/2FA"
            raise HappyFappyError(f"HappyFappy login failed{detail}")

    async def _upload_form(self):
        response, parser = await self._get_form(f"{self.base_url}/upload.php")
        if "/login" in str(response.url) or 'name="username"' in response.text:
            raise HappyFappyError("HappyFappy session is not authenticated")
        self._upload_html = response.text
        return parser.fields

    def _capture_download_link(self, page, title="", torrent_id="", require_title=False):
        page = str(page or "").replace(r"\/", "/")
        matches = list(
            re.finditer(
                r"/?(?:torrents\.php\?[^\"'<>\s]+|download\.php\?[^\"'<>\s]+)",
                page,
                re.I,
            )
        )
        normalized_title = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
        title_tokens = [w for w in re.split(r"\W+", normalized_title) if len(w) >= 3]
        for match in matches:
            candidate = urljoin(f"{self.base_url}/", unescape(match.group(0)))
            parsed = urlsplit(candidate)
            is_dl = (
                parsed.scheme in ("http", "https")
                and parsed.netloc.lower().endswith("happyfappy.net")
                and (
                    re.search(r"(?:^|&)action=download(?:&|$)", parsed.query, re.I)
                    or parsed.path.lower().endswith("download.php")
                )
            )
            if not is_dl:
                continue
            if torrent_id and not re.search(rf"(?:^|[?&])id={re.escape(str(torrent_id))}(?:&|$)", candidate):
                continue
            if require_title and normalized_title:
                window = page[max(0, match.start() - 5000) : match.end() + 5000]
                plain = re.sub(r"<[^>]+>", " ", unescape(window))
                plain = re.sub(r"\s+", " ", plain).casefold()
                if normalized_title not in plain and not any(t in plain for t in title_tokens):
                    continue
            self.uploaded_torrent_url = candidate
            return True
        return False

    def _find_torrent_detail_url(self, page, title=""):
        page = str(page or "").replace(r"\/", "/")
        normalized_title = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
        title_tokens = [w for w in re.split(r"\W+", normalized_title) if len(w) >= 3]
        for match in re.finditer(r"/?torrents\.php\?[^\"'<>\s]*\bid=\d+[^\"'<>\s]*", page, re.I):
            window = page[max(0, match.start() - 5000) : match.end() + 5000]
            plain = re.sub(r"<[^>]+>", " ", unescape(window))
            plain = re.sub(r"\s+", " ", plain).casefold()
            if normalized_title and normalized_title not in plain and not any(t in plain for t in title_tokens):
                continue
            candidate = urljoin(f"{self.base_url}/", unescape(match.group(0)))
            parsed = urlsplit(candidate)
            if (
                parsed.scheme in ("http", "https")
                and parsed.netloc.lower().endswith("happyfappy.net")
                and not re.search(r"(?:^|&)action=download(?:&|$)", parsed.query, re.I)
            ):
                return candidate
        return ""

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
        recent_text = ""
        success_marker = bool(
            re.search(
                r"(?:your\s+)?torrent\s+(?:has\s+been\s+)?(?:uploaded|added)|"
                r"upload\s+successful|thanks\s+for\s+uploading",
                response.text,
                re.I,
            )
            or re.search(r"class=[\"'][^\"']*(?:success|alert-success|successbox)", response.text, re.I)
        )
        if not success_marker:
            recent = await self.client.get(f"{self.base_url}/torrents.php")
            recent.raise_for_status()
            recent_text = recent.text
            recent_plain = re.sub(r"<[^>]+>", " ", unescape(recent_text))
            recent_plain = re.sub(r"\s+", " ", recent_plain).casefold()
            success_marker = bool(
                str(title or "").strip().casefold() in recent_plain
                and re.search(r"torrents\.php\?[^\"'<>\s]*\bid=\d+", recent_text, re.I)
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
        self.upload_details["accepted"] = True
        torrent_id = ""
        id_match = re.search(r"[?&]id=(\d+)", str(response.url))
        if not id_match:
            id_match = re.search(r"torrents\.php\?[^\"'<>\s]*\bid=(\d+)", response.text, re.I)
        if id_match:
            torrent_id = id_match.group(1)
            self.upload_details["torrent_id"] = torrent_id

        # 1. Try upload response page directly
        self._capture_download_link(response.text, title, torrent_id=torrent_id, require_title=False)

        # 2. Try user's uploaded page (shows newest upload at the top)
        if not self.uploaded_torrent_url:
            try:
                uploaded = await self.client.get(f"{self.base_url}/torrents.php?type=uploaded")
                if uploaded.is_success:
                    self._capture_download_link(uploaded.text, title, torrent_id=torrent_id, require_title=False)
            except Exception:
                pass

        # 3. Try torrent details page directly if torrent_id is known
        if not self.uploaded_torrent_url and torrent_id:
            try:
                detail = await self.client.get(f"{self.base_url}/torrents.php?id={torrent_id}")
                if detail.is_success:
                    self._capture_download_link(detail.text, title, torrent_id=torrent_id, require_title=False)
            except Exception:
                pass

        # 4. Try recent torrents list
        if not self.uploaded_torrent_url:
            if not recent_text:
                try:
                    recent = await self.client.get(f"{self.base_url}/torrents.php")
                    if recent.is_success:
                        recent_text = recent.text
                except Exception:
                    pass
            if recent_text:
                self._capture_download_link(recent_text, title, torrent_id=torrent_id, require_title=True)
                if not self.uploaded_torrent_url:
                    detail_url = self._find_torrent_detail_url(recent_text, title)
                    if detail_url:
                        try:
                            detail = await self.client.get(detail_url)
                            if detail.is_success:
                                self._capture_download_link(detail.text, title, require_title=False)
                        except Exception:
                            pass

        self.upload_details["download_url"] = self.uploaded_torrent_url
        if not self.upload_details.get("torrent_id") and self.uploaded_torrent_url:
            id_match = re.search(r"[?&]id=(\d+)", self.uploaded_torrent_url)
            if id_match:
                self.upload_details["torrent_id"] = id_match.group(1)
        return str(response.url)

    async def download_uploaded_torrent(self, destination):
        if not self.uploaded_torrent_url:
            raise HappyFappyError("HappyFappy accepted the upload but did not provide its download link")
        response = await self.client.get(self.uploaded_torrent_url)
        response.raise_for_status()
        content = response.content
        if len(content) < 32 or b"announce" not in content[:4096]:
            raise HappyFappyError("HappyFappy download link did not return a torrent file")
        with open(destination, "wb") as torrent:
            torrent.write(content)
