import os
import re
import logging
import requests
from typing import Dict, Any, Optional

LOGGER = logging.getLogger("StarfallAniList")

ANILIST_API_URL = "https://graphql.anilist.co"

ANILIST_MEDIA_QUERY = """
query ($search: String) {
  Media (search: $search, type: ANIME) {
    id
    idMal
    title {
      romaji
      english
      native
    }
    synonyms
    season
    seasonYear
    episodes
    format
    status
    coverImage {
      extraLarge
      large
    }
    bannerImage
  }
}
"""

class AniListClient:
    """Client for querying anime metadata, Romaji translation, and high-res cover art."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self._cache: Dict[str, Any] = {}
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "StarfallPipeline/1.3 (+https://github.com/starfall)"
        })

    def search_anime(self, title: str) -> Optional[Dict[str, Any]]:
        """
        Query AniList for an anime title and return standardized metadata.
        Uses Page.media so non-exact matches never raise HTTP 404, and falls back to
        parenthetical alternate titles or spaced compound words (e.g. Summertime -> Summer Time).
        """
        alt_match = re.search(r"\((.*?)\)", title)
        alt_title = alt_match.group(1).strip() if alt_match else ""

        clean_title = re.sub(r"\[.*?\]|\(.*?\)|Season\s*\d+|S\d+", "", title).strip()
        clean_title = re.sub(r"[-_]", " ", clean_title).strip()
        cache_key = clean_title.lower()
        if cache_key in self._cache:
            return self._cache[cache_key]

        search_candidates = [clean_title]
        if alt_title and alt_title.lower() != clean_title.lower():
            search_candidates.append(alt_title)
        if "summertime" in clean_title.lower():
            search_candidates.append(re.sub(r"(?i)summertime", "Summer Time", clean_title))
        if "&" in clean_title:
            search_candidates.append(clean_title.split("&")[0].strip())

        for cand in search_candidates:
            if not cand:
                continue
            safe_q = cand.replace('"', '\\"')
            gql = f'''
            {{
              Page(perPage: 1) {{
                media(search: "{safe_q}", type: ANIME, sort: SEARCH_MATCH) {{
                  id
                  idMal
                  title {{
                    romaji
                    english
                    native
                  }}
                  synonyms
                  season
                  seasonYear
                  episodes
                  format
                  status
                  genres
                  averageScore
                  description(asHtml: false)
                  studios(isMain: true) {{
                    nodes {{
                      name
                    }}
                  }}
                  coverImage {{
                    extraLarge
                    large
                  }}
                  bannerImage
                }}
              }}
            }}
            '''
            try:
                resp = self.session.post(ANILIST_API_URL, json={"query": gql}, timeout=10)
                if resp.status_code == 200:
                    media_list = ((resp.json().get("data") or {}).get("Page") or {}).get("media") or []
                    if media_list:
                        data = media_list[0]
                        self._cache[cache_key] = data
                        return data
                else:
                    LOGGER.debug(f"AniList query returned status {resp.status_code} for {cand}")
            except Exception as e:
                LOGGER.warning(f"Error querying AniList for {cand}: {e}")

        self._cache[cache_key] = None
        return None

    def get_romaji_title(self, title: str) -> str:
        """Translate English title to official Romaji title."""
        info = self.search_anime(title)
        if info and info.get("title", {}).get("romaji"):
            return info["title"]["romaji"]
        return title

    def download_season_poster(self, title: str, season: int, output_dir: str = "thumbnails") -> Optional[str]:
        """
        Fetch and download official AniList season poster.
        """
        os.makedirs(output_dir, exist_ok=True)
        info = self.search_anime(title)
        if not info:
            return None

        cover_url = info.get("coverImage", {}).get("extraLarge") or info.get("coverImage", {}).get("large")
        if not cover_url:
            return None

        clean_slug = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")
        target_path = os.path.join(output_dir, f"poster_{clean_slug}_S{season:02d}.jpg")

        try:
            resp = self.session.get(cover_url, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 1000:
                with open(target_path, "wb") as f:
                    f.write(resp.content)
                LOGGER.info(f"Saved AniList poster to {target_path} ({len(resp.content) / 1024:.1f} KB)")
                return target_path
        except Exception as e:
            LOGGER.error(f"Failed to download poster from {cover_url}: {e}")

        return None

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = AniListClient()
    for test in ["Demon Slayer", "A Couple of Cuckoos", "Solo Leveling"]:
        res = client.search_anime(test)
        if res:
            print(f"Title: {test}")
            print(f"  • Romaji: {res['title']['romaji']}")
            print(f"  • English: {res['title']['english']}")
            print(f"  • Native: {res['title']['native']}")
            print(f"  • Cover: {res.get('coverImage', {}).get('extraLarge')}")
