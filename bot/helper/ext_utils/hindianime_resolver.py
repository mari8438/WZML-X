import json
from dataclasses import dataclass
from html import unescape
from logging import getLogger
from re import DOTALL, IGNORECASE, findall, search, sub
from typing import Optional
from urllib.parse import unquote, urljoin

try:
    from httpx import AsyncClient
except ImportError:
    AsyncClient = None

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

LOGGER = getLogger(__name__)

HINDIANIME_BASE_URL = "https://hindianimeszone.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class HindiAnimeCandidate:
    post_title: str
    post_url: str
    episode_title: str
    quality: str
    gateway_url: str
    gdflix_url: str
    season: Optional[int] = None
    episode: Optional[int] = None
    is_season_pack: bool = False
    file_title: str = ""

    @property
    def dedupe_key(self) -> str:
        """Unique key for deduplication."""
        if self.gdflix_url:
            return f"gdflix:{self.gdflix_url.strip()}"
        if self.gateway_url:
            return f"gateway:{self.gateway_url.strip()}"
        return f"haz:{self.post_url.strip()}_{self.episode_title}_{self.quality}"


class HindiAnimeResolver:
    """Crawler and link resolver for hindianimeszone.com releases."""

    @classmethod
    async def fetch_json(cls, url: str) -> Optional[list]:
        """Fetches JSON from WordPress REST API."""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        }
        if AsyncClient is not None:
            try:
                async with AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
                    res = await client.get(url)
                    if res.status_code == 200:
                        return res.json()
            except Exception as err:
                LOGGER.debug(f"AsyncClient fetch_json error for {url}: {err}")

        if cffi_requests is not None:
            try:
                with cffi_requests.Session(impersonate="chrome136", timeout=25) as session:
                    res = session.get(url, headers=headers, allow_redirects=True)
                    if res.status_code == 200:
                        return res.json()
            except Exception as err:
                LOGGER.debug(f"cffi_requests fetch_json error for {url}: {err}")

        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.loads(response.read().decode("utf-8", errors="ignore"))

    @classmethod
    async def fetch_html(cls, url: str) -> str:
        """Fetches HTML page content."""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if AsyncClient is not None:
            try:
                async with AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
                    res = await client.get(url)
                    if res.status_code == 200:
                        return res.text
            except Exception as err:
                LOGGER.debug(f"AsyncClient fetch_html error for {url}: {err}")

        if cffi_requests is not None:
            try:
                with cffi_requests.Session(impersonate="chrome136", timeout=25) as session:
                    res = session.get(url, headers=headers, allow_redirects=True)
                    if res.status_code == 200:
                        return res.text
            except Exception as err:
                LOGGER.debug(f"cffi_requests fetch_html error for {url}: {err}")

        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as response:
            return response.read().decode("utf-8", errors="ignore")

    @classmethod
    async def resolve_via_solver(cls, gateway_url: str, solver_url: str) -> Optional[dict]:
        """Calls the FlareSolverr/HAZ solver microservice to solve Turnstile and extract the GDFlix link."""
        payload = {"url": gateway_url}
        headers = {"Content-Type": "application/json"}

        # Try designated solver_url, fallback to localhost:8192
        candidates_urls = [solver_url]
        if "flaresolverr" in solver_url:
            candidates_urls.append("http://127.0.0.1:8192/solve")

        for s_url in candidates_urls:
            if not s_url:
                continue
            if AsyncClient is not None:
                try:
                    async with AsyncClient(timeout=45) as client:
                        res = await client.post(s_url, json=payload, headers=headers)
                        if res.status_code == 200:
                            data = res.json()
                            if data.get("status") == "success":
                                return data
                except Exception as err:
                    LOGGER.debug(f"Solver {s_url} failed: {err}")

            if cffi_requests is not None:
                try:
                    with cffi_requests.Session(timeout=45) as session:
                        res = session.post(s_url, json=payload, headers=headers)
                        if res.status_code == 200:
                            data = res.json()
                            if data.get("status") == "success":
                                return data
                except Exception as err:
                    LOGGER.debug(f"cffi solver {s_url} failed: {err}")

            import urllib.request
            try:
                req = urllib.request.Request(
                    s_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=45) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    if data.get("status") == "success":
                        return data
            except Exception as err:
                LOGGER.debug(f"urllib solver {s_url} failed: {err}")

        return None

    @staticmethod
    def is_tamil_audio(text: str) -> bool:
        """Validates if Tamil audio is available in the text."""
        if not text:
            return False
        return bool(search(r"\btamil\b", text, flags=IGNORECASE))

    @staticmethod
    def extract_season_episode(text: str) -> tuple[Optional[int], Optional[int]]:
        """Extracts season and episode numbers from post/episode text."""
        if not text:
            return None, None
        # Match S01E05 or S1E5 or 1x5
        match = search(r"\bS(\d{1,2})\s*E(\d{1,4})\b", text, flags=IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2))

        x_match = search(r"\b(\d{1,2})x(\d{1,4})\b", text, flags=IGNORECASE)
        if x_match:
            return int(x_match.group(1)), int(x_match.group(2))

        # Season only
        s_match = search(r"\b(?:Season|S)\s*(\d{1,2})\b", text, flags=IGNORECASE)
        s_val = int(s_match.group(1)) if s_match else None

        # Episode only
        e_match = search(r"\b(?:Episode|EP|E)\s*(\d{1,4})\b", text, flags=IGNORECASE)
        e_val = int(e_match.group(1)) if e_match else None

        return s_val, e_val

    @classmethod
    def parse_post_episodes(
        cls, html_content: str, post_title: str, post_url: str
    ) -> list[dict]:
        """Parses episode blocks and quality download links from post HTML content."""
        # Check language line
        lang_match = search(r"Language:</span></b>\s*<b>(.*?)</b>", html_content, flags=DOTALL | IGNORECASE)
        lang_text = lang_match.group(1) if lang_match else ""

        has_tamil = cls.is_tamil_audio(lang_text) or cls.is_tamil_audio(post_title) or cls.is_tamil_audio(html_content)
        if not has_tamil:
            return []

        post_season, _ = cls.extract_season_episode(post_title)
        episodes_raw = []

        # Find all episode containers
        ep_blocks = findall(r'<div class="episode">(.*?)(?=<div class="episode|\Z)', html_content, flags=DOTALL)
        for ep_html in ep_blocks:
            ep_title_match = search(r'<span class="episode-title">([^<]+)</span>', ep_html)
            if not ep_title_match:
                continue

            ep_title = ep_title_match.group(1).strip()
            if "how to download" in ep_title.lower():
                continue

            ep_s, ep_e = cls.extract_season_episode(ep_title)
            final_season = ep_s or post_season or 1

            # Find all quality download links inside this episode block
            buttons = findall(
                r'<a[^>]+href="([^"]*(?:download1\.php|gdflix)[^"]*)"[^>]*>([^<]+)</a>',
                ep_html,
            )
            for gateway_url, quality_label in buttons:
                gateway_url = unescape(gateway_url.strip())
                quality_clean = quality_label.strip()

                q_lower = quality_clean.lower()
                if any(bad in q_lower for bad in ["telegram", "join", "group", "how to download", "watch online", "stream"]):
                    continue

                is_pack = "batch" in quality_clean.lower() or "zip" in gateway_url.lower()

                episodes_raw.append({
                    "post_title": post_title,
                    "post_url": post_url,
                    "episode_title": ep_title,
                    "season": final_season,
                    "episode": ep_e,
                    "quality": quality_clean,
                    "gateway_url": gateway_url,
                    "is_season_pack": is_pack,
                })

        return episodes_raw

    @classmethod
    async def get_tamil_candidates(
        cls,
        max_pages: int = 1,
        solver_url: str = "http://flaresolverr:8192/solve",
    ) -> list[HindiAnimeCandidate]:
        """Scrapes Hindi Anime Zone Tamil category and resolves candidate GDFlix links."""
        resolved = []
        visited_posts = set()

        for page in range(1, max_pages + 1):
            rest_url = f"{HINDIANIME_BASE_URL}/wp-json/wp/v2/posts?categories=11&per_page=20&page={page}"
            try:
                posts_data = await cls.fetch_json(rest_url)
            except Exception as err:
                LOGGER.warning(f"HindiAnimeZone: Failed fetching REST API page {page}: {err}")
                posts_data = None

            posts = []
            if posts_data and isinstance(posts_data, list):
                for p in posts_data:
                    p_title = unescape(p.get("title", {}).get("rendered", "").strip())
                    p_url = p.get("link", "").strip()
                    p_content = p.get("content", {}).get("rendered", "")
                    posts.append((p_title, p_url, p_content))
            else:
                # Fallback to category HTML parsing
                cat_url = f"{HINDIANIME_BASE_URL}/category/tamil/page/{page}/" if page > 1 else f"{HINDIANIME_BASE_URL}/category/tamil/"
                try:
                    cat_html = await cls.fetch_html(cat_url)
                    cards = findall(
                        r'<a class="haz-card-art" href="([^"]+)"[^>]*alt="([^"]+)"',
                        cat_html,
                    )
                    for c_url, c_title in cards:
                        p_html = await cls.fetch_html(c_url)
                        posts.append((unescape(c_title.strip()), c_url, p_html))
                except Exception as err:
                    LOGGER.error(f"HindiAnimeZone: Failed scraping fallback category page {cat_url}: {err}")

            for p_title, p_url, p_content in posts:
                if p_url in visited_posts:
                    continue
                visited_posts.add(p_url)

                raw_items = cls.parse_post_episodes(p_content, p_title, p_url)
                for item in raw_items:
                    gateway_url = item["gateway_url"]
                    gdflix_url = ""

                    # Direct link if already a GDFlix URL
                    if "gdflix" in gateway_url.lower():
                        gdflix_url = gateway_url
                    else:
                        # Resolve Turnstile and gateway via solver service
                        solved = await cls.resolve_via_solver(gateway_url, solver_url)
                        if solved and solved.get("gdflix_url"):
                            gdflix_url = solved["gdflix_url"]
                            if solved.get("title") and not item.get("file_title"):
                                item["file_title"] = solved["title"]

                    if not gdflix_url:
                        LOGGER.warning(f"HindiAnimeZone: Could not resolve GDFlix link for {item['episode_title']} ({item['quality']})")
                        continue

                    # Construct clean file title
                    clean_post = sub(r"\[.*?\]|\(.*?\)", "", p_title).strip()
                    s_str = f"S{item['season']:02d}" if item['season'] else "S01"
                    e_str = f"E{item['episode']:02d}" if item['episode'] else ""
                    se_tag = f"[{s_str}{e_str}]" if e_str else f"[{s_str}]"
                    file_name = f"{se_tag} {clean_post} - {item['quality']} [Tamil]"

                    candidate = HindiAnimeCandidate(
                        post_title=p_title,
                        post_url=p_url,
                        episode_title=item["episode_title"],
                        quality=item["quality"],
                        gateway_url=gateway_url,
                        gdflix_url=gdflix_url,
                        season=item["season"],
                        episode=item["episode"],
                        is_season_pack=item["is_season_pack"],
                        file_title=item.get("file_title") or file_name,
                    )
                    resolved.append(candidate)

        return resolved
