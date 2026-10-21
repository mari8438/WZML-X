import json
from dataclasses import dataclass, field
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
    mirrors: dict[str, str] = field(default_factory=dict)
    fallback_urls: list[str] = field(default_factory=list)

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
        """Fetches JSON from WordPress REST API, using FlareSolverr fallback if Cloudflare blocks."""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        }
        if cffi_requests is not None:
            try:
                with cffi_requests.Session(impersonate="chrome", timeout=20) as session:
                    res = session.get(url, headers=headers, allow_redirects=True)
                    if res.status_code == 200:
                        return res.json()
            except Exception as err:
                LOGGER.debug(f"cffi_requests fetch_json error for {url}: {err}")

        # Fallback to FlareSolverr sidecar
        for s_url in ["http://flaresolverr:8191/v1", "http://127.0.0.1:8191/v1"]:
            try:
                payload = {"cmd": "request.get", "url": url, "maxTimeout": 30000}
                if AsyncClient is not None:
                    async with AsyncClient(timeout=35) as client:
                        resp = await client.post(s_url, json=payload)
                        if resp.status_code == 200:
                            data = resp.json()
                            raw_html = data.get("solution", {}).get("response", "")
                            match = search(r"<pre>(.*?)</pre>", raw_html, DOTALL)
                            raw_json = match.group(1) if match else raw_html
                            return json.loads(raw_json)
            except Exception as err:
                LOGGER.debug(f"FlareSolverr fetch_json error via {s_url}: {err}")

        return None

    @classmethod
    async def fetch_html(cls, url: str) -> str:
        """Fetches HTML page content, using FlareSolverr fallback if Cloudflare blocks."""
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if cffi_requests is not None:
            try:
                with cffi_requests.Session(impersonate="chrome", timeout=20) as session:
                    res = session.get(url, headers=headers, allow_redirects=True)
                    if res.status_code == 200:
                        return res.text
            except Exception as err:
                LOGGER.debug(f"cffi_requests fetch_html error for {url}: {err}")

        # Fallback to FlareSolverr sidecar
        for s_url in ["http://flaresolverr:8191/v1", "http://127.0.0.1:8191/v1"]:
            try:
                payload = {"cmd": "request.get", "url": url, "maxTimeout": 30000}
                if AsyncClient is not None:
                    async with AsyncClient(timeout=35) as client:
                        resp = await client.post(s_url, json=payload)
                        if resp.status_code == 200:
                            data = resp.json()
                            return data.get("solution", {}).get("response", "")
            except Exception as err:
                LOGGER.debug(f"FlareSolverr fetch_html error via {s_url}: {err}")

        return ""

    @classmethod
    async def resolve_via_solver(cls, gateway_url: str, solver_url: str) -> Optional[dict]:
        """Calls FlareSolverr or HAZ solver microservice to solve Turnstile and extract download mirrors."""
        candidates_urls = []
        if solver_url:
            candidates_urls.append(solver_url)
            if ":8192" in solver_url:
                candidates_urls.append(
                    solver_url.replace(":8192/solve", ":8191/v1").replace(":8192", ":8191/v1")
                )
        candidates_urls.extend(["http://flaresolverr:8191/v1", "http://127.0.0.1:8191/v1"])

        headers = {"Content-Type": "application/json"}

        for s_url in candidates_urls:
            if not s_url:
                continue

            # FlareSolverr /v1 API requires {"cmd": "request.get", "url": ...}
            is_flaresolverr = "8191" in s_url or "/v1" in s_url or "flaresolverr" in s_url
            if is_flaresolverr:
                payload = {
                    "cmd": "request.get",
                    "url": gateway_url,
                    "maxTimeout": 60000,
                }
            else:
                payload = {"url": gateway_url}

            data = None
            if AsyncClient is not None:
                try:
                    async with AsyncClient(timeout=65) as client:
                        res = await client.post(s_url, json=payload, headers=headers)
                        if res.status_code == 200:
                            data = res.json()
                except Exception as err:
                    LOGGER.debug(f"Solver {s_url} failed: {err}")

            if not data and cffi_requests is not None:
                try:
                    with cffi_requests.Session(timeout=65) as session:
                        res = session.post(s_url, json=payload, headers=headers)
                        if res.status_code == 200:
                            data = res.json()
                except Exception as err:
                    LOGGER.debug(f"cffi solver {s_url} failed: {err}")

            if not data:
                import urllib.request

                try:
                    req = urllib.request.Request(
                        s_url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers=headers,
                    )
                    with urllib.request.urlopen(req, timeout=65) as response:
                        data = json.loads(response.read().decode("utf-8"))
                except Exception as err:
                    LOGGER.debug(f"urllib solver {s_url} failed: {err}")

            if not data:
                continue

            # Case A: FlareSolverr standard response
            if data.get("status") == "ok" and "solution" in data:
                html_resp = data.get("solution", {}).get("response", "")
                mirrors = {}
                page_title = ""
                try:
                    from lxml.etree import HTML

                    tree = HTML(html_resp)
                    title_nodes = tree.xpath("//title/text()")
                    if title_nodes:
                        page_title = title_nodes[0].strip()
                    for a in tree.xpath("//a"):
                        h = unescape(a.get("href", "")).strip()
                        t = " ".join(a.xpath(".//text()")).strip().lower()
                        if not h or h.startswith("#") or "download1.php" in h:
                            continue
                        if "pixeldrain" in h or "pixeldrain" in t:
                            continue
                        elif "gdshare" in h or "gdshare" in t:
                            mirrors["gdshare"] = h
                        elif any(k in h for k in ["filepress", "filebee", "press"]):
                            mirrors["filepress"] = h
                        elif "gdflix" in h or "gdflix" in t:
                            mirrors["gdflix"] = h
                except Exception as parse_err:
                    LOGGER.debug(f"lxml parse error: {parse_err}")

                if not mirrors:
                    for h in findall(r'href=["\']([^"\']+)["\']', html_resp):
                        h = unescape(h).strip()
                        hl = h.lower()
                        if "download1.php" in hl or "pixeldrain" in hl:
                            continue
                        if "gdshare" in hl and "gdshare" not in mirrors:
                            mirrors["gdshare"] = h
                        elif ("filepress" in hl or "filebee" in hl) and "filepress" not in mirrors:
                            mirrors["filepress"] = h
                        elif "gdflix" in hl and "gdflix" not in mirrors:
                            mirrors["gdflix"] = h

                best_url = (
                    mirrors.get("gdshare")
                    or mirrors.get("filepress")
                    or mirrors.get("gdflix")
                )
                if not best_url and mirrors:
                    best_url = next(iter(mirrors.values()))

                if best_url:
                    LOGGER.info(
                        f"HindiAnimeZone: Resolved mirrors {list(mirrors.keys())}. "
                        f"Selected primary: {best_url}"
                    )
                    return {
                        "status": "success",
                        "gdflix_url": best_url,
                        "mirrors": mirrors,
                        "title": page_title,
                    }

            # Case B: Custom HAZ solver response
            elif data.get("status") == "success":
                raw_mirrors = data.get("mirrors") or {}
                mirrors = {
                    k: v for k, v in raw_mirrors.items()
                    if "pixeldrain" not in str(k).lower() and "pixeldrain" not in str(v).lower()
                }
                best_url = (
                    mirrors.get("gdshare")
                    or mirrors.get("filepress")
                    or mirrors.get("gdflix")
                )
                if not best_url:
                    for u in mirrors.values():
                        if any(h in u.lower() for h in ["gdshare", "gcloud", "filebee", "filepress", "gdflix"]):
                            best_url = u
                            break
                return {
                    "status": "success",
                    "gdflix_url": best_url,
                    "mirrors": mirrors,
                    "title": data.get("title", ""),
                }

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
        cls, html_content: str, post_title: str, post_url: str, quality_filter: Optional[str] = None
    ) -> list[dict]:
        """Parses episode blocks and quality download links from post HTML content.

        If a batch / season zip pack is found for the anime, yields the zip pack candidate
        and skips all single episodes for that anime.
        Otherwise, yields individual episodes in strict chronological order.
        """
        if not html_content:
            return []
        html_content = unescape(html_content)
        post_title = unescape(post_title)

        # Check language line
        lang_match = search(r"Language:</span></b>\s*<b>(.*?)</b>", html_content, flags=DOTALL | IGNORECASE)
        lang_text = lang_match.group(1) if lang_match else ""

        has_tamil = cls.is_tamil_audio(lang_text) or cls.is_tamil_audio(post_title) or cls.is_tamil_audio(html_content)
        if not has_tamil:
            return []

        post_season, _ = cls.extract_season_episode(post_title)
        final_post_season = post_season or 1

        # Check for batch zip links across the post
        all_buttons = findall(
            r'<a[^>]+href="([^"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^"]*)"[^>]*>([^<]+)</a>',
            html_content,
        )

        zip_candidates = []
        for gateway_url, label in all_buttons:
            gateway_url = unescape(gateway_url.strip())
            label_clean = label.strip()
            l_lower = label_clean.lower()
            url_lower = gateway_url.lower()

            if any(bad in l_lower for bad in ["telegram", "join", "group", "how to download", "watch online", "stream"]):
                continue

            is_zip_label = any(k in l_lower for k in ["batch", "zip", "complete"]) or "zip" in url_lower
            if is_zip_label:
                if quality_filter:
                    if quality_filter.lower() not in l_lower and quality_filter.lower() not in url_lower:
                        continue
                zip_candidates.append({
                    "post_title": post_title,
                    "post_url": post_url,
                    "episode_title": "Batch Zip",
                    "season": final_post_season,
                    "episode": None,
                    "quality": label_clean,
                    "gateway_url": gateway_url,
                    "is_season_pack": True,
                    "file_title": f"{post_title} [{label_clean}] [Batch Zip] [Tamil]",
                })

        # Also check if post title itself mentions [Zip Pack] / [Batch Zip] and has buttons
        if not zip_candidates:
            post_title_lower = post_title.lower()
            if any(w in post_title_lower for w in ["zip pack", "batch zip", "all episodes zip", "[zip]", "(zip)", "zip file"]):
                for gateway_url, label in all_buttons:
                    gateway_url = unescape(gateway_url.strip())
                    label_clean = label.strip()
                    l_lower = label_clean.lower()
                    if quality_filter and quality_filter.lower() not in l_lower:
                        continue
                    zip_candidates.append({
                        "post_title": post_title,
                        "post_url": post_url,
                        "episode_title": "Batch Zip",
                        "season": final_post_season,
                        "episode": None,
                        "quality": label_clean,
                        "gateway_url": gateway_url,
                        "is_season_pack": True,
                        "file_title": f"{post_title} [{label_clean}] [Batch Zip] [Tamil]",
                    })
                    break

        if zip_candidates:
            LOGGER.info(
                f"HindiAnimeZone: Found {len(zip_candidates)} batch zip link(s) for '{post_title}'. "
                "Selecting batch zip and skipping single episodes."
            )
            return zip_candidates

        # If post does not have individual episode blocks, return empty
        if '<div class="episode">' not in html_content:
            LOGGER.info(
                f"HindiAnimeZone: Skipping '{post_title}' - no individual episode blocks found."
            )
            return []

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

            # Find all quality download links inside this episode block (excluding pixeldrain)
            buttons = findall(
                r'<a[^>]+href="([^"]*(?:download1\.php|gdflix|gdshare|filebee|filepress)[^"]*)"[^>]*>([^<]+)</a>',
                ep_html,
            )
            for gateway_url, quality_label in buttons:
                gateway_url = unescape(gateway_url.strip())
                quality_clean = quality_label.strip()

                q_lower = quality_clean.lower()
                if any(bad in q_lower for bad in ["telegram", "join", "group", "how to download", "watch online", "stream"]):
                    continue

                # Filter strictly by quality if requested (e.g. 1080p only)
                if quality_filter:
                    q_target = quality_filter.lower()
                    if q_target not in q_lower:
                        continue

                # Skip any zip/batch link found inside an episode block (already handled)
                if "batch" in q_lower or "zip" in q_lower or "zip" in gateway_url.lower():
                    continue

                episodes_raw.append({
                    "post_title": post_title,
                    "post_url": post_url,
                    "episode_title": ep_title,
                    "season": final_season,
                    "episode": ep_e,
                    "quality": quality_clean,
                    "gateway_url": gateway_url,
                    "is_season_pack": False,
                })

        # Sort individual episodes chronologically (ascending season, then ascending episode)
        episodes_raw.sort(key=lambda x: (x.get("season") or 1, x.get("episode") or 0))
        return episodes_raw

    @classmethod
    async def get_tamil_candidates(
        cls,
        max_pages: int = 1,
        solver_url: str = "http://flaresolverr:8192/solve",
        days: Optional[int] = None,
        quality_filter: Optional[str] = "1080p",
        seen: Optional[set[str]] = None,
    ) -> list[HindiAnimeCandidate]:
        """Scrapes Hindi Anime Zone Tamil category and resolves candidate GDFlix/GDShare links."""
        resolved = []
        visited_posts = set()

        after_param = ""
        if days:
            from datetime import datetime, timezone, timedelta
            after_iso = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            after_param = f"&after={after_iso}"

        per_page = 100 if days else 20

        for page in range(1, max_pages + 1):
            rest_url = f"{HINDIANIME_BASE_URL}/wp-json/wp/v2/posts?categories=11&per_page={per_page}&page={page}{after_param}"
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

                raw_items = cls.parse_post_episodes(p_content, p_title, p_url, quality_filter=quality_filter)
                for item in raw_items:
                    gateway_url = item["gateway_url"]
                    if "pixeldrain" in gateway_url.lower():
                        continue

                    # Pre-check seen items cache to avoid wasting Turnstile solves on already downloaded items
                    if seen:
                        temp_gw_key = f"gateway:{gateway_url.strip()}"
                        if temp_gw_key in seen:
                            continue
                        clean_post_key = sub(r"\[.*?\]|\(.*?\)", "", p_title).strip()
                        s_k = f"S{item['season']:02d}" if item['season'] else "S01"
                        e_k = f"E{item['episode']:02d}" if item['episode'] else ""
                        haz_key = f"haz:{clean_post_key}_{s_k}{e_k}_{item['quality']}"
                        if haz_key in seen:
                            continue

                    gdflix_url = ""

                    # Direct link if already a known mirror host URL (skip pixeldrain)
                    mirrors_dict = {}
                    if any(x in gateway_url.lower() for x in ["gdshare", "gcloud", "filepress", "filebee", "gdflix"]):
                        gdflix_url = gateway_url
                        mirrors_dict = {"direct": gateway_url}
                    elif "pixeldrain" in gateway_url.lower():
                        continue
                    else:
                        # Resolve Turnstile and gateway via solver service
                        solved = await cls.resolve_via_solver(gateway_url, solver_url)
                        if solved:
                            raw_mirrors = solved.get("mirrors") or {}
                            mirrors_dict = {
                                k: v for k, v in raw_mirrors.items()
                                if "pixeldrain" not in str(k).lower() and "pixeldrain" not in str(v).lower()
                            }
                            gdflix_url = solved.get("gdflix_url", "")
                            if solved.get("title") and not item.get("file_title"):
                                item["file_title"] = solved["title"]
                        if not gdflix_url and mirrors_dict:
                            gdflix_url = (
                                mirrors_dict.get("gdshare")
                                or mirrors_dict.get("filepress")
                                or mirrors_dict.get("gdflix")
                            )
                        if not gdflix_url and mirrors_dict:
                            for u in mirrors_dict.values():
                                if any(h in u.lower() for h in ["gdshare", "gcloud", "filebee", "filepress", "gdflix"]):
                                    gdflix_url = u
                                    break

                    if not gdflix_url:
                        LOGGER.warning(f"HindiAnimeZone: Could not resolve download mirror for {item['episode_title']} ({item['quality']})")
                        continue

                    # Construct clean file title with proper media extension
                    clean_post = sub(r"\[.*?\]|\(.*?\)", "", p_title).strip()
                    s_str = f"S{item['season']:02d}" if item['season'] else "S01"
                    e_str = f"E{item['episode']:02d}" if item['episode'] else ""
                    se_tag = f"[{s_str}{e_str}]" if e_str else f"[{s_str}]"
                    ext = ".zip" if item.get("is_season_pack") else ".mkv"
                    if item.get("is_season_pack"):
                        file_name = f"[{s_str}] {clean_post} - {item['quality']} [Batch Zip] [Tamil]{ext}"
                    else:
                        file_name = f"{se_tag} {clean_post} - {item['quality']} [Tamil]{ext}"

                    final_title = item.get("file_title") or file_name
                    if not final_title.lower().endswith(ext):
                        final_title = f"{final_title}{ext}"

                    fallback_urls = [
                        u for u in mirrors_dict.values()
                        if u != gdflix_url and "pixeldrain" not in u.lower()
                    ]

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
                        file_title=final_title,
                        mirrors=mirrors_dict,
                        fallback_urls=fallback_urls,
                    )
                    resolved.append(candidate)

        return resolved
