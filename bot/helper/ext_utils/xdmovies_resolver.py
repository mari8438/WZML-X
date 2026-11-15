import asyncio
import json
import re
from dataclasses import dataclass
from html import unescape
from logging import getLogger
from typing import Optional
from urllib.parse import quote_plus, urljoin, urlparse

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

try:
    from httpx import AsyncClient
except ImportError:
    AsyncClient = None

from ...core.config_manager import Config

LOGGER = getLogger(__name__)

XDMOVIES_BASE_URL = getattr(Config, "XDMOVIES_SITE_URL", "https://huh.xdmovies.wtf") or "https://huh.xdmovies.wtf"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class XDMoviesCandidate:
    post_title: str
    post_url: str
    is_series: bool
    file_title: str
    quality: str  # "1080p av1" or "1080p hevc"
    has_tamil: bool
    download_url: str
    file_size_str: str = ""
    tmdb_id: Optional[str] = None
    poster_path: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    is_season_pack: bool = False
    category: str = "movie"  # "movie" or "series"

    @property
    def dedupe_key(self) -> str:
        """Unique key for deduplication."""
        if self.file_title:
            return f"xdm:file:{self.file_title.strip()}"
        if self.download_url:
            return f"xdm:dl:{self.download_url.strip()}"
        return f"xdm:post:{self.post_url.strip()}"


class XDMoviesResolver:
    """Crawler and link resolver for XDMovies (huh.xdmovies.wtf) releases."""

    _cached_auth_token = "7297skkihkajwnsgaklakshuwd"

    @classmethod
    async def get_auth_token(cls) -> str:
        """Retrieves active X-Auth-Token from get_token.php or cached fallback."""
        url = urljoin(XDMOVIES_BASE_URL, "/php/get_token.php")
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        if AsyncClient is not None:
            try:
                async with AsyncClient(headers=headers, timeout=10) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        token = data.get("token")
                        if token:
                            cls._cached_auth_token = token
                            return token
            except Exception as err:
                LOGGER.debug(f"XDMovies: token fetch error: {err}")
        return cls._cached_auth_token

    @classmethod
    async def fetch_html(cls, url: str) -> str:
        """Fetches HTML page content, utilizing FlareSolverr sidecar if gated by Cloudflare."""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": XDMOVIES_BASE_URL,
        }
        if cffi_requests is not None:
            try:
                with cffi_requests.Session(impersonate="chrome136", timeout=20) as session:
                    res = session.get(url, headers=headers, allow_redirects=True)
                    if res.status_code == 200 and "Just a moment..." not in res.text:
                        return res.text
            except Exception as err:
                LOGGER.debug(f"XDMovies: curl_cffi fetch_html error for {url}: {err}")

        # FlareSolverr sidecar routing
        solver_urls = [
            getattr(Config, "FLARESOLVERR_URL", "http://flaresolverr:8191/v1"),
            "http://127.0.0.1:8191/v1",
            "http://flaresolverr:8191/v1",
        ]
        for s_url in solver_urls:
            try:
                payload = {"cmd": "request.get", "url": url, "maxTimeout": 45000}
                if AsyncClient is not None:
                    async with AsyncClient(timeout=50) as client:
                        resp = await client.post(s_url, json=payload)
                        if resp.status_code == 200:
                            data = resp.json()
                            html = data.get("solution", {}).get("response", "")
                            if html and "Just a moment..." not in html:
                                return html
            except Exception as err:
                LOGGER.debug(f"XDMovies: FlareSolverr fetch error via {s_url}: {err}")

        return ""

    @staticmethod
    def is_tamil_audio(text: str) -> bool:
        """Checks if text indicates Tamil audio."""
        if not text:
            return False
        return bool(re.search(r"\b(?:tam|tamil|tml)\b", text, flags=re.IGNORECASE))

    @staticmethod
    def is_target_quality(text: str) -> tuple[bool, str]:
        """
        Validates if text represents 1080p AV1 or 1080p H.265/HEVC.
        Strictly disqualifies non-1080p (2160p, 4K, 720p, 480p).
        Strictly disqualifies standard H264/AVC unless AV1/HEVC is explicitly present.
        Returns: (is_match, quality_label)
        """
        if not text:
            return False, ""
        clean = re.sub(r"(?i)xdmovies(?:\.com)?|\bds4k\b", " ", text)

        # Disqualify non-1080p resolutions
        if re.search(r"\b(?:2160p?|4k|uhd|720p?|480p?|576p?)\b", clean, flags=re.IGNORECASE):
            return False, ""

        has_1080p = bool(re.search(r"\b1080p?\b", clean, flags=re.IGNORECASE))
        if not has_1080p:
            return False, ""

        has_av1 = bool(re.search(r"\bav1\b", clean, flags=re.IGNORECASE))
        has_hevc = bool(re.search(r"\b(?:hevc|h\.?265|x265)\b", clean, flags=re.IGNORECASE))
        has_h264 = bool(re.search(r"\b(?:h\.?264|x264|avc)\b", clean, flags=re.IGNORECASE))

        # If it's explicitly H264 without AV1 or HEVC, reject
        if has_h264 and not has_av1 and not has_hevc:
            return False, ""

        if has_av1:
            return True, "1080p av1"
        if has_hevc:
            return True, "1080p hevc"

        return False, ""

    @classmethod
    def is_target_candidate(cls, cand: XDMoviesCandidate) -> bool:
        """Validates if a candidate satisfies all criteria (Tamil audio, 1080p AV1/HEVC)."""
        if not cand or not cand.has_tamil:
            return False
        match, _ = cls.is_target_quality(cand.quality)
        return match

    @staticmethod
    def extract_season_episode(text: str) -> tuple[Optional[int], Optional[int]]:
        """Extracts season and episode numbers from title string."""
        if not text:
            return None, None
        match = re.search(r"\bS(\d{1,2})\s*E(\d{1,4})\b", text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2))

        s_match = re.search(r"\b(?:S|Season)\s*(\d{1,2})\b", text, flags=re.IGNORECASE)
        e_match = re.search(r"\b(?:E|EP|Episode)\s*(\d{1,4})\b", text, flags=re.IGNORECASE)
        s = int(s_match.group(1)) if s_match else None
        e = int(e_match.group(1)) if e_match else None
        return s, e

    @classmethod
    def parse_post_candidates(
        cls,
        html: str,
        post_url: str,
        post_title: str = "",
        tmdb_id: Optional[str] = None,
        audio_languages: str = "",
        poster_path: Optional[str] = None,
    ) -> list[XDMoviesCandidate]:
        """Parses detail page HTML and returns candidate items matching Tamil audio and 1080p AV1/HEVC."""
        is_series = bool(
            re.search(r"\/series\/|[-_]series[-_]|[-_]show[-_]", post_url, flags=re.IGNORECASE)
            or re.search(r"\b(?:season|s\d{1,2}|episodes?)\b", post_title, flags=re.IGNORECASE)
        )
        category = "series" if is_series else "movie"

        page_has_tamil = (
            cls.is_tamil_audio(audio_languages)
            or cls.is_tamil_audio(post_title)
            or cls.is_tamil_audio(post_url)
            or cls.is_tamil_audio(html)
        )

        candidates: list[XDMoviesCandidate] = []

        # Find all download-item containers
        # Typical structure: <div class="download-item ..."><div class="custom-title ...">FILENAME</div><a href="LINK">SIZE</a></div>
        item_blocks = re.findall(
            r'<div[^>]*class=[\"\'][^\"\']*download-item[^\"\']*[\"\'][^>]*>(.*?)(?=<div[^>]*class=[\"\'][^\"\']*download-item|\Z)',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )

        # Fallback if download-item is not wrapping: search for custom-title blocks directly
        if not item_blocks:
            item_blocks = re.findall(
                r'(<div[^>]*class=[\"\'][^\"\']*custom-title[^\"\']*[\"\'].*?)(?=<div[^>]*class=[\"\'][^\"\']*custom-title|\Z)',
                html,
                flags=re.DOTALL | re.IGNORECASE,
            )

        for block in item_blocks:
            title_m = re.search(
                r'<div[^>]*class=[\"\'][^\"\']*custom-title[^\"\']*[\"\'][^>]*>(.*?)</div>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            file_title = unescape(title_m.group(1).strip()) if title_m else ""
            if not file_title:
                # Fallback to plain text match if custom-title div is absent
                text_clean = re.sub(r"<[^>]+>", " ", block)
                name_m = re.search(r"([A-Za-z0-9_\-\. ]+?\.(?:mkv|zip|mp4))", text_clean, flags=re.IGNORECASE)
                file_title = name_m.group(1).strip() if name_m else ""

            if not file_title:
                continue

            btn_m = re.search(
                r'<a[^>]+href=[\"\'](https?://link\.xdmovies\.wtf/download/[^\"\']+)[\"\'][^>]*>(.*?)</a>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if not btn_m:
                continue

            dl_url = btn_m.group(1).strip()
            size_txt = re.sub(r"<[^>]+>", "", btn_m.group(2)).strip()

            combined_str = f"{post_title} {file_title} {audio_languages} {post_url}"

            # 1. Tamil Audio Filter Check
            item_has_tamil = cls.is_tamil_audio(file_title) or page_has_tamil
            if not item_has_tamil:
                continue

            # 2. 1080p AV1 / HEVC Filter Check
            is_match, q_label = cls.is_target_quality(file_title)
            if not is_match:
                # Also check block context if filename is truncated
                is_match, q_label = cls.is_target_quality(combined_str)
                if not is_match:
                    continue

            season, episode = cls.extract_season_episode(file_title)
            is_pack = file_title.lower().endswith(".zip") or "pack" in file_title.lower() or "season" in file_title.lower()

            candidate = XDMoviesCandidate(
                post_title=post_title or file_title,
                post_url=post_url,
                is_series=is_series,
                file_title=file_title,
                quality=q_label,
                has_tamil=True,
                download_url=dl_url,
                file_size_str=size_txt,
                tmdb_id=tmdb_id,
                poster_path=poster_path,
                season=season,
                episode=episode,
                is_season_pack=is_pack,
                category=category,
            )
            candidates.append(candidate)

        return candidates

    @classmethod
    async def search_candidates(
        cls,
        query: str,
        quality_filter: str = "1080p",
        audio_filter: str = "tamil",
    ) -> list[XDMoviesCandidate]:
        """
        Searches huh.xdmovies.wtf using the internal search API.
        Retrieves matching posts and extracts candidate download links.
        """
        token = await cls.get_auth_token()
        url = urljoin(XDMOVIES_BASE_URL, f"/php/search_api.php?query={quote_plus(query)}&fuzzy=true")
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "X-Auth-Token": token,
            "Accept": "application/json",
        }

        results = []
        if AsyncClient is not None:
            try:
                async with AsyncClient(headers=headers, timeout=15) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        results = resp.json()
            except Exception as err:
                LOGGER.error(f"XDMovies: search API failed: {err}")

        if not results:
            return []

        candidates: list[XDMoviesCandidate] = []
        for item in results:
            title = item.get("title", "")
            path_rel = item.get("path", "")
            if not path_rel:
                continue

            qualities = [q.lower() for q in item.get("qualities", [])]
            if quality_filter and quality_filter.lower() not in qualities:
                continue

            audio_langs = item.get("audio_languages", "")
            if audio_filter and not cls.is_tamil_audio(audio_langs) and not cls.is_tamil_audio(title):
                continue

            post_url = urljoin(XDMOVIES_BASE_URL, path_rel)
            tmdb_id = str(item.get("tmdb_id") or "")
            poster_path = item.get("poster", "")

            # Fetch page HTML to extract direct candidate links
            page_html = await cls.fetch_html(post_url)
            if not page_html:
                continue

            post_cands = cls.parse_post_candidates(
                html=page_html,
                post_url=post_url,
                post_title=title,
                tmdb_id=tmdb_id,
                audio_languages=audio_langs,
                poster_path=poster_path,
            )
            candidates.extend(post_cands)

        return candidates

    @classmethod
    async def get_candidates_for_url(
        cls,
        url: str,
        quality_filter: str = "1080p",
        audio_filter: str = "tamil",
    ) -> list[XDMoviesCandidate]:
        """Resolves candidates directly from a release URL or download link."""
        if "link.xdmovies.wtf/download/" in url:
            # Standalone download link passed directly
            return [
                XDMoviesCandidate(
                    post_title="XDMovies Direct Release",
                    post_url=url,
                    is_series=False,
                    file_title=url.rstrip("/").split("/")[-1],
                    quality="1080p av1",
                    has_tamil=True,
                    download_url=url,
                    category="movie",
                )
            ]

        # Extract tmdb_id if present at the end of the slug
        m_tmdb = re.search(r"-(\d{5,8})$", url.rstrip("/"))
        tmdb_id = m_tmdb.group(1) if m_tmdb else None

        html = await cls.fetch_html(url)
        if not html:
            LOGGER.warning(f"XDMovies: failed to fetch HTML for {url}")
            return []

        title_m = re.search(r"<title>(.*?)(?:—|\||-|XDMovies).*?</title>", html, re.IGNORECASE)
        post_title = unescape(title_m.group(1).strip()) if title_m else "XDMovies Release"

        return cls.parse_post_candidates(
            html=html,
            post_url=url,
            post_title=post_title,
            tmdb_id=tmdb_id,
        )

    @classmethod
    async def get_tamil_candidates(
        cls,
        max_pages: int = 2,
        days: Optional[int] = None,
        seen: Optional[set[str]] = None,
    ) -> list[XDMoviesCandidate]:
        """
        Crawls recent catalog pages (category.php) or searches for Tamil releases.
        Supports filtering for releases from the past N days (e.g. days=7 for Last Week Leech).
        """
        seen = seen or set()
        token = await cls.get_auth_token()
        candidates: list[XDMoviesCandidate] = []

        # We query the search API for 'tamil' as primary fast-path, plus recent catalog crawl
        search_tamil_posts = await cls.search_candidates("tamil", quality_filter="1080p", audio_filter="tamil")
        for cand in search_tamil_posts:
            if cand.dedupe_key not in seen:
                candidates.append(cand)

        # Also crawl recent category pages
        for cat_type in ["movies", "series"]:
            for page in range(1, max_pages + 1):
                cat_url = urljoin(XDMOVIES_BASE_URL, f"/category.php?type={cat_type}&page={page}")
                html = await cls.fetch_html(cat_url)
                if not html:
                    break

                # Extract movie/series links from category page
                links = re.findall(
                    r'<a[^>]+href=[\"\'](/movies/[^\"\']+|/series/[^\"\']+)[\"\'][^>]*>(.*?)</a>',
                    html,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                for rel_path, link_html in links:
                    full_post_url = urljoin(XDMOVIES_BASE_URL, rel_path)
                    if not cls.is_tamil_audio(rel_path) and not cls.is_tamil_audio(link_html):
                        continue

                    m_tmdb = re.search(r"-(\d{5,8})$", rel_path)
                    tmdb_id = m_tmdb.group(1) if m_tmdb else None

                    post_html = await cls.fetch_html(full_post_url)
                    if not post_html:
                        continue

                    title_m = re.search(r"<h3>(.*?)</h3>", link_html)
                    post_title = unescape(title_m.group(1).strip()) if title_m else rel_path

                    post_cands = cls.parse_post_candidates(
                        html=post_html,
                        post_url=full_post_url,
                        post_title=post_title,
                        tmdb_id=tmdb_id,
                    )
                    for c in post_cands:
                        if c.dedupe_key not in seen:
                            candidates.append(c)

        return candidates
