import re
from html.parser import HTMLParser
from os import path as ospath
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


class HappyFappyClient:
    def __init__(self, base_url, username, password, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self):
        await self.client.aclose()

    async def _get_form(self, url):
        response = await self.client.get(url)
        response.raise_for_status()
        parser = _HiddenFields()
        parser.feed(response.text)
        return response, parser

    async def login(self, login_path="/login.php"):
        login_url = urljoin(f"{self.base_url}/", login_path.lstrip("/"))
        response, parser = await self._get_form(login_url)
        if "/logout" in response.text:
            return
        data = dict(parser.fields)
        data.update({"username": self.username, "password": self.password})
        data.setdefault("login", "Log in")
        response = await self.client.post(login_url, data=data)
        response.raise_for_status()
        if "/logout" not in response.text:
            raise HappyFappyError("HappyFappy login failed or requires CAPTCHA/2FA")

    async def _upload_form(self):
        response, parser = await self._get_form(f"{self.base_url}/upload.php")
        if "/logout" not in response.text:
            raise HappyFappyError("HappyFappy session is not authenticated")
        return parser.fields

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
                "submit": "Upload torrent",
                "category": category,
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
        if "/torrents.php" not in str(response.url):
            raise HappyFappyError("HappyFappy did not confirm the torrent upload")
        return str(response.url)
