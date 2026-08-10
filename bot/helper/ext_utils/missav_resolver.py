from dataclasses import dataclass, field
from html import unescape
from re import DOTALL, finditer, search, sub
from urllib.parse import quote, urljoin, urlsplit

from httpx import AsyncClient
from lxml import html


MISSAV_HOSTS = {
    "missav.ai",
    "missav.live",
    "missav.ws",
}
MISSAV_BASE = "https://missav.live"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}
STREAM_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*;q=0.8",
}


class MissAVProtectedPageError(RuntimeError):
    """The public page is temporarily protected; no bypass is attempted."""


@dataclass
class MissAVQuality:
    label: str
    url: str
    height: int = 0


@dataclass
class MissAVTitle:
    url: str
    title: str
    description: str = ""
    thumbnail: str = ""
    duration: int = 0
    date: str = ""
    actors: list[str] = field(default_factory=list)
    director: str = ""
    master_url: str = ""
    qualities: list[MissAVQuality] = field(default_factory=list)


def _valid_missav_url(url):
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host in MISSAV_HOSTS or any(host.endswith(f".{item}") for item in MISSAV_HOSTS)
    )


def _valid_stream_url(url):
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "surrit.com" or host.endswith(".surrit.com"))


def _unpack_player_scripts(source):
    unpacked = []
    pattern = (
        r"eval\(function\(p,a,c,k,e,d\).*?\}\("
        r"'(?P<p>(?:\\.|[^'])*)',(?P<a>\d+),(?P<c>\d+),"
        r"'(?P<k>(?:\\.|[^'])*)'\.split\('\|'\)"
    )
    for match in finditer(pattern, source, DOTALL):
        radix = int(match.group("a"))
        count = int(match.group("c"))
        if radix > 36 or count > 500:
            continue
        payload = match.group("p").replace(r"\'", "'").replace(r"\\", "\\")
        keys = match.group("k").split("|")
        for index in range(count - 1, -1, -1):
            if index >= len(keys) or not keys[index]:
                continue
            token = "0" if index == 0 else ""
            number = index
            while number:
                token = "0123456789abcdefghijklmnopqrstuvwxyz"[number % radix] + token
                number //= radix
            payload = sub(rf"\b{token}\b", keys[index], payload)
        unpacked.append(payload)
    return unpacked


def _player_urls(source):
    candidates = []
    for text in (source, *_unpack_player_scripts(source)):
        text = text.replace(r"\/", "/")
        for match in finditer(r"https://[^'\"\\\s]+", text):
            url = match.group(0).rstrip(";,)")
            if _valid_stream_url(url) and url not in candidates:
                candidates.append(url)
    masters = [url for url in candidates if url.endswith(".m3u8") and "playlist" in url]
    return masters or [url for url in candidates if url.endswith(".m3u8")]


def _meta(tree, property_name):
    values = tree.xpath(f'//meta[@property="{property_name}"]/@content')
    return unescape(values[0]).strip() if values else ""


def _catalog_links(source, page_url):
    tree = html.fromstring(source)
    links = []
    for href in tree.xpath('//div[contains(@class,"thumbnail")]//a/@href'):
        url = urljoin(page_url, href)
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        if not _valid_missav_url(url) or len(parts) != 2:
            continue
        if parts[0].lower() not in {"en", "ja", "ko", "cn", "ms", "th", "de", "fr", "vi", "id", "fil", "pt"}:
            continue
        if url not in links:
            links.append(url)
    return links


class MissAVResolver:
    def __init__(self):
        self.client = AsyncClient(
            follow_redirects=True,
            timeout=40,
            headers=PAGE_HEADERS,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    async def _get(self, url, *, stream=False, referer=""):
        if stream and not _valid_stream_url(url):
            raise ValueError("Rejected non-Surrit stream URL")
        if not stream and not _valid_missav_url(url):
            raise ValueError("Only public HTTPS MissAV URLs are supported")
        headers = dict(STREAM_HEADERS if stream else PAGE_HEADERS)
        if referer:
            parsed = urlsplit(referer)
            headers.update(
                {
                    "Referer": referer,
                    "Origin": f"{parsed.scheme}://{parsed.netloc}",
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                }
            )
        response = await self.client.get(url, headers=headers)
        if response.status_code in {401, 403}:
            raise MissAVProtectedPageError(
                "MissAV temporarily returned a protected/Cloudflare page. "
                "No bypass was attempted; retry later or use a public catalog URL."
            )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "captcha" in response.text[:5000].lower():
            raise MissAVProtectedPageError(
                "MissAV returned a protected/Cloudflare page. No bypass was attempted."
            )
        if not stream and "text/html" not in content_type:
            raise RuntimeError("MissAV did not return a public HTML page")
        return response.text

    async def discover(self, url):
        source = await self._get(url)
        if _player_urls(source):
            return [await self.resolve_title(url, source)]
        links = _catalog_links(source, url)
        items = []
        for link in links:
            try:
                items.append(await self.resolve_title(link))
            except Exception:
                # Catalog pages routinely retain stale/deleted cards. The caller
                # reports an empty result if every public card is unavailable.
                continue
        return items

    async def discover_letter(self, letter):
        letter = str(letter or "").strip().upper()
        if len(letter) != 1 or not "A" <= letter <= "Z":
            raise ValueError("MissAV letter must be A-Z")
        page_url = f"{MISSAV_BASE}/en/search/{quote(letter)}"
        source = await self._get(page_url)
        items = []
        # The public search route is the site's letter view. It does not promise
        # that every video code begins with the query, so retain the visible
        # result order instead of discarding legitimate matches by prefix.
        for link in _catalog_links(source, page_url):
            try:
                items.append(await self.resolve_title(link))
            except Exception:
                continue
        return items

    async def resolve_title(self, url, source=None):
        source = source or await self._get(url)
        tree = html.fromstring(source)
        master_urls = _player_urls(source)
        if not master_urls:
            raise RuntimeError("MissAV public Surrit playlist is unavailable")
        duration_raw = _meta(tree, "og:video:duration")
        title = _meta(tree, "og:title") or " ".join(tree.xpath("//h1//text()"))
        item = MissAVTitle(
            url=url,
            title=sub(r"\s+", " ", unescape(title)).strip() or "MissAV",
            description=_meta(tree, "og:description"),
            thumbnail=_meta(tree, "og:image"),
            duration=int(duration_raw) if duration_raw.isdigit() else 0,
            date=_meta(tree, "og:video:release_date"),
            actors=[value for value in tree.xpath('//meta[@property="og:video:actor"]/@content') if value],
            director=_meta(tree, "og:video:director"),
            master_url=master_urls[0],
        )
        item.qualities = await self.resolve_qualities(item.master_url, item.url)
        return item

    async def resolve_qualities(self, master_url, referer):
        source = await self._get(master_url, stream=True, referer=referer)
        lines = [line.strip() for line in source.splitlines() if line.strip()]
        found = []
        for index, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF") or index + 1 >= len(lines):
                continue
            height_match = search(r"RESOLUTION=\d+x(\d+)", line)
            height = int(height_match.group(1)) if height_match else 0
            url = urljoin(master_url, lines[index + 1])
            if not _valid_stream_url(url):
                continue
            label = f"{height}p" if height else f"quality-{len(found) + 1}"
            if all(existing.url != url for existing in found):
                found.append(MissAVQuality(label, url, height))
        if not found and _valid_stream_url(master_url):
            found.append(MissAVQuality("source", master_url))
        return sorted(found, key=lambda quality: quality.height)

    @staticmethod
    def request_headers(referer):
        parsed = urlsplit(referer)
        return {
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
        }
