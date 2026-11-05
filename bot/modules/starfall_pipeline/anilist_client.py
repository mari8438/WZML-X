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
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "StarfallPipeline/1.3 (+https://github.com/starfall)"
        })

    def search_anime(self, title: str) -> Optional[Dict[str, Any]]:
        """
        Query AniList for an anime title and return standardized metadata.
        """
        clean_title = re.sub(r"\[.*?\]|\(.*?\)|Season\s*\d+|S\d+", "", title).strip()
        clean_title = re.sub(r"[-_]", " ", clean_title).strip()

        # Query GraphQL directly with search string
        gql = f'''
        {{
          Media(search: "{clean_title}", type: ANIME) {{
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
            genres
            averageScore
            description(asHtml: false)
            coverImage {{
              extraLarge
              large
            }}
          }}
        }}
        '''

        try:
            resp = self.session.post(ANILIST_API_URL, json={"query": gql}, timeout=10)
            if resp.status_code != 200:
                LOGGER.warning(f"AniList query returned status {resp.status_code} for {clean_title}")
                return None
            data = resp.json().get("data", {}).get("Media")
            return data
        except Exception as e:
            LOGGER.error(f"Error querying AniList for {clean_title}: {e}")
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
