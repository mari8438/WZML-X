import base64
import codecs
import json
from dataclasses import dataclass
from html import unescape
from logging import getLogger
from re import IGNORECASE, DOTALL, findall, search, sub
from typing import Optional
from urllib.parse import urljoin, urlparse

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

try:
    from httpx import AsyncClient
except ImportError:
    AsyncClient = None

LOGGER = getLogger(__name__)

FOURKHDHUB_BASE_URL = "https://4khdhub.one"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class FourKHDHubCandidate:
    post_title: str
    post_url: str
    is_series: bool
    file_title: str
    quality: str  # "1080p hevc" or "1080p av1"
    has_tamil: bool
    hub_url: str
    mediator_url: str
    season: Optional[int] = None
    episode: Optional[int] = None
    is_season_pack: bool = False
    category: str = "movie"  # "movie" or "series"

    @property
    def dedupe_key(self) -> str:
        """Unique key for deduplication."""
        if self.file_title:
            return f"file:{self.file_title.strip()}"
        if self.hub_url:
            return f"hub:{self.hub_url.strip()}"
        return f"med:{self.mediator_url.strip()}"


class FourKHDHubResolver:
    """Crawler and link resolver for 4KHDHub.one releases."""

    @staticmethod
    def decode_mediator_payload(encoded_str: str) -> str:
        """Decodes the 3-step base64/rot13 payload used by 4khdhub mediator pages."""
        d1 = base64.b64decode(encoded_str).decode("ascii")
        d2 = base64.b64decode(d1).decode("ascii")
        rot = codecs.decode(d2, "rot_13")
        d3 = base64.b64decode(rot).decode("utf-8")
        data = json.loads(d3)
        hub_url = base64.b64decode(data["o"]).decode("utf-8")
        return hub_url

    @classmethod
    async def fetch_html(cls, url: str) -> str:
        """Fetches page HTML with Chrome impersonation or standard client."""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cffi_requests is not None:
            try:
                with cffi_requests.Session(impersonate="chrome136", timeout=30) as session:
                    res = session.get(url, headers=headers, allow_redirects=True)
                    if res.status_code == 200:
                        return res.text
            except Exception as err:
                LOGGER.debug(f"curl_cffi fetch failed for {url}: {err}")

        if AsyncClient is not None:
            try:
                async with AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
                    res = await client.get(url)
                    if res.status_code == 200:
                        return res.text
            except Exception as err:
                LOGGER.debug(f"httpx fetch failed for {url}: {err}")

        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode("utf-8", errors="ignore")

    @classmethod
    async def resolve_intermediate_hublink(cls, mediator_url: str) -> str:
        """Resolves mediator links (e.g. greenmotors.club / greenmotors.cc) to HubCloud/HubHold URL."""
        parsed = urlparse(mediator_url)
        domain = parsed.hostname or ""
        if "hubcloud" in domain or "hubhold" in domain:
            return mediator_url

        try:
            html = await cls.fetch_html(mediator_url)
            match = search(
                r"s\s*\(\s*['\"]o['\"]\s*,\s*['\"]([A-Za-z0-9+/=]+)['\"]",
                html,
            )
            if match:
                payload = match.group(1)
                hub_url = cls.decode_mediator_payload(payload)
                if hub_url:
                    return hub_url
        except Exception as err:
            LOGGER.warning(f"Failed to resolve intermediate mediator URL {mediator_url}: {err}")

        return mediator_url

    @staticmethod
    def is_tamil_audio(text: str) -> bool:
        """Checks if text indicates Tamil audio."""
        if not text:
            return False
        return bool(search(r"\btamil\b", text, flags=IGNORECASE))

    @staticmethod
    def is_target_quality(text: str) -> tuple[bool, str]:
        """Validates if text represents 1080p HEVC or 1080p AV1.

        Must NOT be 2160p, 720p, or 480p.
        Must NOT be standard x264/AVC unless HEVC or AV1 is explicitly present.
        Returns: (is_match, quality_label)
        """
        if not text:
            return False, ""
        # Strip site brand and "ds4k" (downscaled from 4k source to 1080p)
        cleaned = sub(r"(?i)4khdhub(?:\.com)?|\bds4k\b", " ", text)

        # Disqualify non-1080p resolutions
        if search(r"\b(?:2160p?|4k|uhd|720p?|480p?)\b", cleaned, flags=IGNORECASE):
            return False, ""

        has_1080p = bool(search(r"\b1080p?\b", cleaned, flags=IGNORECASE))
        if not has_1080p:
            return False, ""

        has_av1 = bool(search(r"\bav1\b", cleaned, flags=IGNORECASE))
        has_hevc = bool(search(r"\b(?:hevc|h\.?265|x265)\b", cleaned, flags=IGNORECASE))

        if has_av1:
            return True, "1080p av1"
        if has_hevc:
            return True, "1080p hevc"

        return False, ""

    @staticmethod
    def extract_season_episode(text: str) -> tuple[Optional[int], Optional[int]]:
        """Extracts season and episode numbers from title string."""
        if not text:
            return None, None
        # Match S01E05 or S1E5
        match = search(r"\bS(\d{1,2})\s*E(\d{1,4})\b", text, flags=IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2))
        # Match S01 or Season 1
        s_match = search(r"\b(?:S|Season)\s*(\d{1,2})\b", text, flags=IGNORECASE)
        if s_match:
            return int(s_match.group(1)), None
        # Match Episode 05 or EP05
        e_match = search(r"\b(?:E|EP|Episode)\s*(\d{1,4})\b", text, flags=IGNORECASE)
        if e_match:
            return None, int(e_match.group(1))
        return None, None

    @classmethod
    def scrape_catalog_cards(cls, html: str, base_url: str = FOURKHDHUB_BASE_URL) -> list[tuple[str, str, bool]]:
        """Extracts (post_url, title, is_series) from catalog HTML."""
        cards = []
        # Pattern for movie cards
        card_matches = findall(
            r'<a href="([^"]+)" class="movie-card"[^>]*>.*?<h3 class="movie-card-title">([^<]+)</h3>',
            html,
            flags=DOTALL,
        )
        for rel_url, title in card_matches:
            full_url = urljoin(base_url, rel_url)
            is_series = bool(search(r"[-_]series[-_]|\/category\/(?:series|[a-z]+-series)\/", rel_url, flags=IGNORECASE))
            cards.append((full_url, unescape(title.strip()), is_series))
        return cards

    @classmethod
    def parse_post_candidates(
        cls, html: str, post_url: str, post_title: str
    ) -> list[dict]:
        """Parses post HTML and returns candidate items matching Tamil audio and 1080p HEVC/AV1."""
        is_series = bool(search(r"[-_]series[-_]|\/category\/(?:series|[a-z]+-series)\/", post_url, flags=IGNORECASE))
        post_audios_match = search(
            r'<span class="metadata-label">Audios:</span>\s*<span class="metadata-value"[^>]*>(.*?)</span>',
            html,
            flags=DOTALL,
        )
        post_audios = post_audios_match.group(1) if post_audios_match else ""
        post_has_tamil = cls.is_tamil_audio(post_audios) or cls.is_tamil_audio(html)

        candidates = []
        # Find all download-item blocks
        items = findall(r'<div class="download-item[^"]*"(.*?)(?=<div class="download-item|\Z)', html, flags=DOTALL)

        for item_html in items:
            file_title_match = search(r'<div class="file-title">(.*?)</div>', item_html, flags=DOTALL)
            header_match = search(r'<div class="download-header[^>]*>(.*?)</div>', item_html, flags=DOTALL)

            header_text = sub(r"<[^>]+>", " ", header_match.group(1)) if header_match else ""
            file_title = unescape(file_title_match.group(1).strip()) if file_title_match else " ".join(header_text.split())

            badges = findall(r'<span class="badge"[^>]*>([^<]+)</span>', item_html)
            combined_info = f"{file_title} {' '.join(badges)} {header_text}"

            item_has_tamil = cls.is_tamil_audio(combined_info) or (post_has_tamil and "tamil" in combined_info.lower())
            if not item_has_tamil:
                continue

            is_match, quality_label = cls.is_target_quality(combined_info)
            if not is_match:
                continue

            # Check if this item is a Season Pack (.zip)
            is_pack = file_title.lower().endswith(".zip") or "pack" in file_title.lower() or "pack" in header_text.lower()
            season, episode = cls.extract_season_episode(file_title)

            # Check for download links inside this item (HubCloud / HubDrive / HubHold)
            hub_links = findall(
                r'<a[^>]+href="([^"]+)"[^>]*>\s*(?:<span[^>]*>)?(Download\s+Hub[A-Za-z]+)',
                item_html,
            )

            if hub_links:
                # Use the first HubCloud/HubHold link for the season pack or movie
                med_url, _ = hub_links[0]
                candidates.append({
                    "post_title": post_title,
                    "post_url": post_url,
                    "is_series": is_series,
                    "file_title": file_title,
                    "quality": quality_label,
                    "has_tamil": True,
                    "mediator_url": med_url,
                    "season": season,
                    "episode": episode,
                    "is_season_pack": is_pack,
                    "category": "series" if is_series else "movie",
                })

            # For series, check if there are individual episode items
            if is_series and not is_pack:
                ep_items = findall(
                    r'<div class="episode-download-item">(.*?)(?=<div class="episode-download-item|\Z)',
                    item_html,
                    flags=DOTALL,
                )
                for ep_html in ep_items:
                    ep_title_match = search(r'<div class="episode-file-title">(.*?)</div>', ep_html, flags=DOTALL)
                    if not ep_title_match:
                        continue
                    ep_file_title = unescape(ep_title_match.group(1).strip())
                    ep_has_tamil = cls.is_tamil_audio(ep_file_title) or post_has_tamil
                    ep_match, ep_quality = cls.is_target_quality(ep_file_title)
                    if not (ep_has_tamil and ep_match):
                        continue

                    ep_links = findall(
                        r'<a[^>]+href="([^"]+)"[^>]*>\s*(?:<span[^>]*>)?(Download\s+Hub[A-Za-z]+)',
                        ep_html,
                    )
                    if not ep_links:
                        continue

                    ep_s, ep_e = cls.extract_season_episode(ep_file_title)
                    candidates.append({
                        "post_title": post_title,
                        "post_url": post_url,
                        "is_series": True,
                        "file_title": ep_file_title,
                        "quality": ep_quality,
                        "has_tamil": True,
                        "mediator_url": ep_links[0][0],
                        "season": ep_s or season,
                        "episode": ep_e or episode,
                        "is_season_pack": False,
                        "category": "series",
                    })

        return candidates

    @classmethod
    async def get_tamil_candidates(cls, max_pages: int = 1) -> list[FourKHDHubCandidate]:
        """Scrapes Tamil catalog and resolves mediator links into FourKHDHubCandidate instances."""
        resolved_candidates = []
        visited_posts = set()

        for page in range(1, max_pages + 1):
            if page == 1:
                cat_url = f"{FOURKHDHUB_BASE_URL}/category/tamil/"
            else:
                cat_url = f"{FOURKHDHUB_BASE_URL}/category/tamil/page/{page}/"
            try:
                cat_html = await cls.fetch_html(cat_url)
                cards = cls.scrape_catalog_cards(cat_html)
                if not cards and page == 1:
                    # Fallback to homepage if category page is unavailable
                    cat_html = await cls.fetch_html(FOURKHDHUB_BASE_URL)
                    cards = cls.scrape_catalog_cards(cat_html)
            except Exception as err:
                LOGGER.error(f"Failed to fetch 4khdhub page {cat_url}: {err}")
                continue

            for post_url, post_title, _ in cards:
                if post_url in visited_posts:
                    continue
                visited_posts.add(post_url)

                try:
                    post_html = await cls.fetch_html(post_url)
                    raw_items = cls.parse_post_candidates(post_html, post_url, post_title)
                    for item in raw_items:
                        hub_url = await cls.resolve_intermediate_hublink(item["mediator_url"])
                        candidate = FourKHDHubCandidate(
                            post_title=item["post_title"],
                            post_url=item["post_url"],
                            is_series=item["is_series"],
                            file_title=item["file_title"],
                            quality=item["quality"],
                            has_tamil=item["has_tamil"],
                            hub_url=hub_url,
                            mediator_url=item["mediator_url"],
                            season=item["season"],
                            episode=item["episode"],
                            is_season_pack=item["is_season_pack"],
                            category=item["category"],
                        )
                        resolved_candidates.append(candidate)
                except Exception as err:
                    LOGGER.warning(f"Failed parsing 4khdhub post {post_url}: {err}")

        return resolved_candidates
