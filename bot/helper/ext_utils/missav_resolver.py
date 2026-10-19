from dataclasses import dataclass, field
from html import unescape
from re import DOTALL, IGNORECASE, findall, finditer, search, sub
from urllib.parse import quote, urljoin, urlsplit

from httpx import AsyncClient
from lxml import html

try:
    from curl_cffi.requests import AsyncSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

from ... import LOGGER

MISSAV_HOSTS = {
    "missav.ai",
    "missav.live",
    "missav.ws",
    "missav123.com",
    "missav.media",
}
MISSAV_BASE = "https://missav.live"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
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
    """The public page is temporarily protected; retry or use another mirror."""


@dataclass(slots=True)
class MissAVQuality:
    label: str
    url: str
    height: int = 0


@dataclass(slots=True)
class MissAVTitle:
    url: str
    title: str
    description: str = ""
    thumbnail: str = ""
    duration: int = 0
    date: str = ""
    views: int = 0
    actors: list[str] = field(default_factory=list)
    director: str = ""
    genres: list[str] = field(default_factory=list)
    master_url: str = ""
    qualities: list[MissAVQuality] = field(default_factory=list)
    sample_urls: list[str] = field(default_factory=list)


def is_fc2_title(text: str) -> bool:
    """FC2 filtering disabled: allows all releases."""
    return False


def is_english_subtitled(item: MissAVTitle | str) -> bool:
    """Detect if MissAV title or genre belongs to the English Subtitle category."""
    if isinstance(item, str):
        candidates = [item]
    else:
        candidates = [
            getattr(item, "title", ""),
            getattr(item, "url", ""),
            getattr(item, "description", ""),
            *getattr(item, "genres", []),
        ]
    pattern = r"(?:english[\s_-]*sub(?:title)?s?|eng[\s_-]*sub)"
    for text in candidates:
        if text and search(pattern, str(text), IGNORECASE):
            return True
    return False


def _valid_missav_url(url):
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (
        host in MISSAV_HOSTS or any(host.endswith(f".{item}") for item in MISSAV_HOSTS)
    )


def _valid_stream_url(url):
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (
        host == "surrit.com" or host.endswith(".surrit.com") or host == "sixst.net" or host.endswith(".sixst.net")
    )


def _unpack_player_scripts(source):
    unpacked = []
    pattern = (
        r"eval\(function\(p,a,c,k,e,d\).*?\}\("
        r"'(?P<p>(?:\\.|[^'])*)',(?P<a>\d+),(?P<c>\d+),"
        r"'(?P<k>(?:\\.|[^'])*)'\.split\('\|'\)"
    )
    for match in finditer(pattern, source, DOTALL):
        try:
            radix = int(match.group("a"))
            count = int(match.group("c"))
            if radix > 36 or count > 1000:
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
        except Exception as error:
            LOGGER.debug(f"Failed unpacking a script chunk: {error}")
    return unpacked


def _player_urls(source):
    candidates = []
    for text in (source, *_unpack_player_scripts(source)):
        text = text.replace(r"\/", "/")
        for match in finditer(r"https?://[^'\"\\\s]+", text):
            url = match.group(0).rstrip(";,)")
            if _valid_stream_url(url) and url not in candidates:
                candidates.append(url)
    masters = [url for url in candidates if url.endswith(".m3u8") and "playlist" in url]
    return masters or [url for url in candidates if url.endswith(".m3u8")]


def _meta(tree, property_name):
    values = tree.xpath(f'//meta[@property="{property_name}"]/@content')
    return unescape(values[0]).strip() if values else ""


def _extract_views(source, tree):
    # Try meta or structured view count first
    views_match = search(r'([\d,]+)\s*(?:views|次播放|回視聴)', source, IGNORECASE)
    if views_match:
        try:
            return int(views_match.group(1).replace(",", "").strip())
        except ValueError:
            pass
    for text in tree.xpath('//span[contains(text(), "views") or contains(text(), "次播放")]/text()'):
        digits = search(r'([\d,]+)', text)
        if digits:
            try:
                return int(digits.group(1).replace(",", "").strip())
            except ValueError:
                pass
    return 0


def _extract_genres(tree):
    genres = []
    for text in tree.xpath('//a[contains(@href, "/genres/") or contains(@href, "/tags/") or contains(@href, "/categories/") or contains(@href, "english-subtitle")]/text()'):
        cleaned = text.strip()
        if cleaned and cleaned not in genres and len(cleaned) < 50:
            genres.append(cleaned)
    return genres


def _extract_sample_urls(source):
    cleaned = source.replace(r"\/", "/")
    urls = findall(r'https?://[^\s"\',]+\/seek\/_\d+\.jpg', cleaned)
    seen = set()
    ordered = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _catalog_links(source, page_url):
    tree = html.fromstring(source)
    links = []
    ignored_slugs = {
        "en", "ja", "ko", "cn", "ms", "th", "de", "fr", "vi", "id", "fil", "pt",
        "genres", "tags", "categories", "makers", "actresses", "search", "new", "release"
    }
    for href in tree.xpath('//div[contains(@class,"thumbnail")]//a/@href | //div[contains(@class,"aspect-w-16")]//a/@href'):
        url = urljoin(page_url, href)
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        if not _valid_missav_url(url) or not parts:
            continue
        # Check standard languages or single video path
        if parts[0].lower() in ignored_slugs:
            if len(parts) < 2:
                continue
            video_slug = parts[1]
        elif parts[0].lower().startswith("dm"):
            if len(parts) >= 3:
                video_slug = parts[-1]
            else:
                continue
        else:
            video_slug = parts[0]
        if video_slug.lower() in ignored_slugs:
            continue
        if url not in links:
            links.append(url)
    return links


class MissAVResolver:
    def __init__(self):
        if _HAS_CURL_CFFI:
            self.client = AsyncSession(
                impersonate="chrome124",
                timeout=40,
                headers=PAGE_HEADERS,
            )
        else:
            self.client = AsyncClient(
                follow_redirects=True,
                timeout=40,
                headers=PAGE_HEADERS,
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        if _HAS_CURL_CFFI:
            await self.client.close()
        else:
            await self.client.aclose()

    async def _get(self, url, *, stream=False, referer=""):
        if stream and not _valid_stream_url(url):
            raise ValueError(f"Rejected non-Surrit stream URL: {url}")
        if not stream and not _valid_missav_url(url):
            raise ValueError(f"Only public HTTPS MissAV URLs are supported: {url}")

        parsed = urlsplit(url)
        urls_to_try = []
        if not stream and parsed.hostname in {"missav.ws", "www.missav.ws", "missav.ai", "www.missav.ai"}:
            urls_to_try.append(url.replace(parsed.hostname, "missav.live"))
            urls_to_try.append(url.replace(parsed.hostname, "missav123.com"))
            urls_to_try.append(url)
        else:
            urls_to_try.append(url)
            if not stream and parsed.hostname not in {"missav.live", "missav123.com"}:
                urls_to_try.append(url.replace(parsed.hostname, "missav.live"))

        last_error = None
        for target_url in urls_to_try:
            headers = dict(STREAM_HEADERS if stream else PAGE_HEADERS)
            if referer:
                ref_parsed = urlsplit(referer)
                headers.update(
                    {
                        "Referer": referer,
                        "Origin": f"{ref_parsed.scheme}://{ref_parsed.netloc}",
                        "Sec-Fetch-Site": "cross-site",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Dest": "empty",
                    }
                )
            try:
                response = await self.client.get(target_url, headers=headers)
                if response.status_code in {401, 403}:
                    text_lower = response.text[:5000].lower() if hasattr(response, "text") else ""
                    cf_mitigated = response.headers.get("cf-mitigated", "").lower()
                    if "captcha" in text_lower or "challenge" in cf_mitigated or "just a moment" in text_lower:
                        last_error = MissAVProtectedPageError(
                            f"MissAV returned a Cloudflare protected page for {target_url}."
                        )
                        continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not stream and "text/html" not in content_type:
                    raise RuntimeError(f"MissAV did not return a public HTML page for {target_url}")
                return response.text
            except Exception as err:
                last_error = err
                continue

        if last_error:
            raise last_error
        raise RuntimeError(f"Unable to fetch URL {url}")

    async def discover(self, url):
        is_sub_category = bool(search(r"english[\s_-]*subtitle", url, IGNORECASE))
        source = await self._get(url)
        if _player_urls(source):
            title_item = await self.resolve_title(url, source)
            if is_sub_category and "English Subtitles" not in title_item.genres:
                title_item.genres.append("English Subtitles")
            return [title_item]

        links = _catalog_links(source, url)
        items = []
        for link in links:
            try:
                item = await self.resolve_title(link)
                if is_sub_category and "English Subtitles" not in item.genres:
                    item.genres.append("English Subtitles")
                items.append(item)
            except Exception as error:
                LOGGER.warning(f"Failed resolving card {link}: {error}")
                continue
        items.sort(key=lambda x: x.views, reverse=True)
        return items

    async def discover_query(self, query):
        clean_query = str(query or "").strip()
        has_sub_filter = bool(search(r"(?:english[\s_-]*sub(?:title)?s?|eng[\s_-]*sub)", clean_query, IGNORECASE))
        keyword = sub(r"(?i)\b(?:english[\s_-]*sub(?:title)?s?|eng[\s_-]*sub)\b", "", clean_query).strip()
        encoded = quote(keyword or clean_query)
        params = ["sort=views"]
        if has_sub_filter:
            params.append("filters=english-subtitle")
        page_url = f"{MISSAV_BASE}/en/search/{encoded}?{'&'.join(params)}"
        LOGGER.info(f"MissAV search URL: {page_url} (english-subtitle filter: {has_sub_filter})")
        source = await self._get(page_url)
        items = []
        for link in _catalog_links(source, page_url):
            try:
                item = await self.resolve_title(link)
                if has_sub_filter and "English Subtitles" not in item.genres:
                    item.genres.append("English Subtitles")
                items.append(item)
            except Exception:
                continue
        items.sort(key=lambda x: x.views, reverse=True)
        return items

    async def discover_letter(self, letter):
        letter = str(letter or "").strip().upper()
        if len(letter) != 1 or not "A" <= letter <= "Z":
            raise ValueError("MissAV letter must be A-Z")
        page_url = f"{MISSAV_BASE}/en/search/{quote(letter)}?sort=views"
        source = await self._get(page_url)
        items = []
        for link in _catalog_links(source, page_url):
            try:
                item = await self.resolve_title(link)
                items.append(item)
            except Exception:
                continue
        items.sort(key=lambda x: x.views, reverse=True)
        return items

    async def resolve_title(self, url, source=None):
        source = source or await self._get(url)
        tree = html.fromstring(source)
        master_urls = _player_urls(source)
        if not master_urls:
            raise RuntimeError(f"MissAV public stream playlist is unavailable for {url}")

        duration_raw = _meta(tree, "og:video:duration")
        raw_title = _meta(tree, "og:title") or " ".join(tree.xpath("//h1//text()"))
        title = sub(r"\s+", " ", unescape(raw_title)).strip() or "MissAV"
        description = _meta(tree, "og:description")
        thumbnail = _meta(tree, "og:image")
        date = _meta(tree, "og:video:release_date")
        views = _extract_views(source, tree)
        actors = [value.strip() for value in tree.xpath('//meta[@property="og:video:actor"]/@content') if value.strip()]
        director = _meta(tree, "og:video:director")
        genres = _extract_genres(tree)
        has_sub_badge = (
            bool(search(r"(?:english[\s_-]*sub(?:title)?s?|eng[\s_-]*sub)", url, IGNORECASE))
            or bool(tree.xpath('//a[contains(@href, "english-subtitle") or contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "english subtitle")] | //span[contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "english subtitle")]'))
        )
        if has_sub_badge and "English Subtitles" not in genres:
            genres.append("English Subtitles")
        sample_urls = _extract_sample_urls(source)

        item = MissAVTitle(
            url=url,
            title=title,
            description=description,
            thumbnail=thumbnail,
            duration=int(duration_raw) if duration_raw.isdigit() else 0,
            date=date,
            views=views,
            actors=actors,
            director=director,
            genres=genres,
            master_url=master_urls[0],
            sample_urls=sample_urls,
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
        return sorted(found, key=lambda quality: quality.height, reverse=True)

    @staticmethod
    def request_headers(referer=""):
        origin = "https://missav.live"
        return {
            "User-Agent": USER_AGENT,
            "Referer": f"{origin}/",
            "Origin": origin,
        }
