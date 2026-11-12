"""
bot/modules/starfall_pipeline/toonworld_helper.py
-------------------------------------------------
Helper client to search and source missing anime audio tracks and movies
from toonworld4all.me when local Audio Bank is missing episodes or seasons.
"""

import re
import logging
import requests
from typing import List, Dict, Optional

LOGGER = logging.getLogger("ToonworldHelper")

BASE_URL = "https://toonworld4all.me"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class ToonworldHelper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def search(self, anime_title: str, tamil_only: bool = True) -> List[Dict[str, str]]:
        """
        Search toonworld4all.me for the specified anime title.
        Returns a list of matching entries with title, url, and format tags.
        """
        clean = re.sub(r"[^\w\s]", " ", anime_title).strip()
        query = f"{clean} Tamil" if tamil_only else clean
        url = f"{BASE_URL}/?s={query.replace(' ', '+')}"
        
        results = []
        try:
            resp = self.session.get(url, timeout=12)
            if resp.status_code != 200:
                LOGGER.warning(f"Toonworld search returned status {resp.status_code} for query: {query}")
                return results

            # Regex search for article entries
            entries = re.findall(
                r'<h2[^>]*class=[\'"][^\'"]*entry-title[^\'"]*[\'"][^>]*>\s*<a\s+href=[\'"]([^\'"]+)[\'"][^>]*>([^<]+)</a>',
                resp.text
            )
            if not entries:
                entries = re.findall(
                    r'<a\s+href=[\'"](https://toonworld4all\.me/[^\'"]+)[\'"][^>]*rel=[\'"]bookmark[\'"][^>]*>([^<]+)</a>',
                    resp.text
                )

            for href, raw_title in entries:
                title_clean = raw_title.replace("&#038;", "&").replace("&#8211;", "-").strip()
                results.append({
                    "title": title_clean,
                    "url": href,
                    "has_tamil": "tamil" in title_clean.lower(),
                })

            LOGGER.info(f"Found {len(results)} results on Toonworld for '{query}'")
        except Exception as e:
            LOGGER.error(f"Error searching Toonworld for '{query}': {e}")

        return results

    def extract_download_links(self, page_url: str) -> List[Dict[str, str]]:
        """
        Scrapes episode/batch links from a toonworld post page.
        """
        links = []
        try:
            resp = self.session.get(page_url, timeout=15)
            if resp.status_code != 200:
                return links

            # Match episode download anchors including accordion style
            ep_blocks = re.findall(
                r'Episode\s*(\d{1,3})[\s\S]*?<a\s+[^>]*href=[\'"]([^\'"]+)[\'"][^>]*>\s*(?:Watch/Download|Download|Watch|Link)',
                resp.text,
                flags=re.I
            )
            for ep_str, link_url in ep_blocks:
                ep_num = int(ep_str)
                links.append({"episode": ep_num, "url": link_url})

            if not links:
                # Fallback pattern
                ep_matches = re.findall(
                    r'(?:Episode\s*(\d{1,3})|E(\d{1,3}))[^\n<]*?<a\s+href=[\'"]([^\'"]+)[\'"][^>]*>(?:Download|Watch|Link)[^<]*</a>',
                    resp.text,
                    flags=re.I
                )
                for m in ep_matches:
                    ep_num = int(m[0] or m[1])
                    link_url = m[2]
                    links.append({"episode": ep_num, "url": link_url})
        except Exception as e:
            LOGGER.error(f"Error scraping links from {page_url}: {e}")

        return links


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    helper = ToonworldHelper()
    res = helper.search("Demon Slayer")
    for r in res[:5]:
        print(f"• {r['title']} -> {r['url']}")
