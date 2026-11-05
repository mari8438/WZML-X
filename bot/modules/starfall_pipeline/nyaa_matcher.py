import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import logging
from typing import List, Dict, Any, Optional

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.anilist_client import AniListClient
else:
    from .anilist_client import AniListClient

LOGGER = logging.getLogger("StarfallNyaaMatcher")

HEVC_RANKS = {
    "YURASUKA": 1,
    "DB": 2,
    "ANIMENCODES": 2,
    "JUDAS": 3,
    "DKB": 4,
    "NEOHEVC": 5,
    "SAM": 6,
    "SEADEX": 6,
    "ASW": 7,
    "EMBER": 8,
    "TENRAI-SENSEI": 9,
    "TENRAI": 9,
    "ANIME TIME": 10,
    "ANIMETIME": 10,
}

AV1_RANKS = {
    "BREEZE": 1,
    "LASTBREEZE": 1,
    "IRONCLAD": 2,
    "SOKUDO": 3,
    "TRIX": 4,
    "NEKOTRIX": 4,
    "NEODESU": 5,
    "LUMEN": 6,
    "ANIME-AV1": 7,
    "OMNIPOTENT500": 7,
    "SAVI0R": 8,
    "SAVI0R-PRO": 8,
}

TRACKERS = [
    "http://nyaa.tracker.wf:7777/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]

class NyaaMatcher:
    def __init__(self, anilist_client: Optional[AniListClient] = None):
        self.anilist = anilist_client or AniListClient()

    @staticmethod
    def extract_release_group(title: str) -> str:
        """Extract encoder group name from title (e.g. '[Judas] Title' -> 'Judas')."""
        known_groups = [
            "YURASUKA", "DB", "animencodes", "Judas", "DKB", "sam", "SeaDex",
            "ASW", "EMBER", "Tenrai-Sensei", "Anime Time", "Breeze", "LastBreeze",
            "Ironclad", "Sokudo", "Trix", "NekoTrix", "neoDESU", "neoHEVC",
            "Lumen", "Anime-AV1", "omnipotent500", "SavI0r", "SavI0r-Pro", "StarLight"
        ]
        for grp in known_groups:
            if re.search(r"[\W_]" + re.escape(grp) + r"([\W_]|$)", title, re.IGNORECASE) or title.startswith(f"[{grp}]"):
                return grp

        m = re.match(r"^\[([A-Za-z0-9_.\-]+)\]", title.strip())
        if m:
            return m.group(1).strip()
        m2 = re.search(r"-([A-Za-z0-9_.\-]+)(?:\s*(?:\[|\().*?(?:\]|\)))*(?:\.mkv|\.mp4|\s*$)", title.strip())
        if m2:
            return m2.group(1).strip()
        return "Unknown"

    @staticmethod
    def detect_codec(title: str) -> str:
        """Detect video codec (AV1, HEVC/x265, AVC/x264)."""
        t = title.upper()
        if "AV1" in t:
            return "AV1"
        if "HEVC" in t or "X265" in t or "H.265" in t or "H265" in t or "265" in t:
            return "HEVC"
        if "X264" in t or "H.264" in t or "H264" in t or "AVC" in t:
            return "AVC"
        return "UNKNOWN"

    @staticmethod
    def is_bluray(title: str) -> bool:
        """Check if release is from Blu-ray source."""
        t = title.upper()
        return bool(re.search(r"\b(BD|BLURAY|BD-RIP|BDRIP|BLU-RAY)\b", t))

    @staticmethod
    def is_complete_batch(title: str, target_season: int = 1) -> bool:
        """
        Check if torrent is a complete batch/season rather than single episode or weekly release.
        """
        t = title.upper()

        # Reject other seasons (e.g., if searching season 1, reject S02, Season 2, 2nd Season)
        for s in range(1, 10):
            if s != target_season and re.search(rf"\b(S(?:EASON)?\s*0?{s}|{s}(?:ND|RD|TH)\s*SEASON)\b", t):
                return False

        # Reject obvious single episode patterns
        if re.search(r"\bS\d{1,2}E\d{1,3}\b", t):
            if not re.search(r"E\d{1,3}\s*[-~]\s*E?\d{1,3}", t):
                return False
        if re.search(r"\b(EP|EPISODE)[.\s_-]*\d{1,3}\b", t) and not re.search(r"(EP|EPISODE)[.\s_-]*\d{1,3}\s*[-~]\s*\d{1,3}", t):
            return False
        if re.search(r"\(WEEKLY\)|\[WEEKLY\]", t):
            return False

        # Positive batch signals
        if any(w in t for w in ["BATCH", "COMPLETE", "COMPLETO"]):
            return True
        if re.search(r"\b0?1\s*[-~]\s*\d{2,3}\b", t) or re.search(r"\b0?1\s*[-~]\s*E?\d{2,3}\b", t):
            return True
        if re.search(rf"\bS(?:EASON)?\s*0?{target_season}\b", t):
            return True
        if target_season == 1 and not re.search(r"\bS(?:EASON)?\s*0?[2-9]\b", t) and ("BD1080P" in t or "1080P" in t) and not re.search(r"[-_]\s*0\d\b", t):
            return True

        return False

    def search_nyaa_rss(self, query: str) -> List[Dict[str, Any]]:
        """Query Nyaa.si RSS feed with category 1_2 (Anime - English-translated)."""
        url = f"https://nyaa.si/?page=rss&f=0&c=1_2&q={urllib.parse.quote_plus(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        req = urllib.request.Request(url, headers=headers)
        results = []
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                xml_content = resp.read()
            root = ET.fromstring(xml_content)
            channel = root.find("channel")
            if channel is None:
                return results

            for item in channel.findall("item"):
                title_elem = item.find("title")
                link_elem = item.find("link")
                guid_elem = item.find("guid")
                if title_elem is None or link_elem is None:
                    continue

                title = title_elem.text or ""
                torrent_url = link_elem.text or ""
                page_url = guid_elem.text if guid_elem is not None else ""

                info_hash = ""
                seeders = 0
                leechers = 0
                size = ""
                for child in item:
                    if child.tag.endswith("infoHash"):
                        info_hash = child.text or ""
                    elif child.tag.endswith("seeders"):
                        try:
                            seeders = int(child.text or "0")
                        except ValueError:
                            seeders = 0
                    elif child.tag.endswith("leechers"):
                        try:
                            leechers = int(child.text or "0")
                        except ValueError:
                            leechers = 0
                    elif child.tag.endswith("size"):
                        size = child.text or ""

                # Construct magnet URL
                magnet_uri = ""
                if info_hash:
                    tr_params = "".join([f"&tr={urllib.parse.quote(t)}" for t in TRACKERS])
                    magnet_uri = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(title)}{tr_params}"

                results.append({
                    "title": title,
                    "torrent_url": torrent_url,
                    "page_url": page_url,
                    "info_hash": info_hash,
                    "magnet_uri": magnet_uri,
                    "seeders": seeders,
                    "leechers": leechers,
                    "size": size,
                    "release_group": self.extract_release_group(title),
                    "codec": self.detect_codec(title),
                    "is_bluray": self.is_bluray(title),
                })
        except Exception as e:
            LOGGER.error(f"Error querying Nyaa RSS for '{query}': {e}")

        return results

    def find_multi_source_batches(
        self,
        title_english: str,
        season_number: int = 1,
        max_hevc: int = 4,
        max_av1: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        Perform complete multi-source discovery for an anime season:
        1. Resolve Romaji, English, and Native titles via AniList.
        2. Query Nyaa for batch torrents.
        3. Filter for 1080p Complete Season Batches.
        4. Prioritize BluRay > WebRip.
        5. Rank HEVC encoders (up to 4) and AV1 encoders (up to 2). Minimum 1 source.
        """
        meta = self.anilist.search_anime(title_english)
        romaji_title = ""
        eng_title = title_english
        if meta and meta.get("title"):
            romaji_title = meta["title"].get("romaji", "")
            eng_title = meta["title"].get("english") or title_english

        search_titles = []
        if romaji_title:
            search_titles.append(romaji_title)
        if eng_title and eng_title.lower() != romaji_title.lower():
            search_titles.append(eng_title)
        if not search_titles:
            search_titles.append(title_english)

        all_candidates: Dict[str, Dict[str, Any]] = {}

        # Search queries for each name variant
        for t in search_titles:
            queries = [
                f"{t} S{season_number:02d} 1080p",
                f"{t} Season {season_number} 1080p",
                f"{t} Batch 1080p",
                f"{t} 1080p",
            ]
            for q in queries:
                items = self.search_nyaa_rss(q)
                for item in items:
                    title = item["title"]
                    # Must be 1080p
                    if "1080" not in title:
                        continue
                    # Must be complete batch
                    if not self.is_complete_batch(title, target_season=season_number):
                        continue
                    # Must have seeders >= 1
                    if item["seeders"] < 1:
                        continue

                    # Deduplicate by info_hash or title
                    key = item["info_hash"] or title
                    if key not in all_candidates:
                        all_candidates[key] = item

        candidates = list(all_candidates.values())
        if not candidates:
            LOGGER.warning(f"No batch torrents found for {title_english} S{season_number:02d}")
            return []

        # Check if any BluRay release exists
        has_bd = any(c["is_bluray"] for c in candidates)
        if has_bd:
            # Enforce BluRay > WebRip: filter to BD releases
            bd_candidates = [c for c in candidates if c["is_bluray"]]
            if bd_candidates:
                candidates = bd_candidates

        # Separate HEVC and AV1 candidates
        hevc_list = []
        av1_list = []
        other_list = []

        for c in candidates:
            grp_upper = c["release_group"].upper()
            codec = c["codec"]

            if codec == "AV1" or grp_upper in AV1_RANKS:
                rank = AV1_RANKS.get(grp_upper, 99)
                c["rank"] = rank
                av1_list.append(c)
            elif codec == "HEVC" or grp_upper in HEVC_RANKS:
                rank = HEVC_RANKS.get(grp_upper, 99)
                c["rank"] = rank
                hevc_list.append(c)
            else:
                other_list.append(c)

        # Sort by rank ascending (1 = best), then seeders descending
        hevc_list.sort(key=lambda x: (x["rank"], -x["seeders"]))
        av1_list.sort(key=lambda x: (x["rank"], -x["seeders"]))

        # Deduplicate by release group (keep best release per group)
        def dedupe_by_group(items: List[Dict[str, Any]], max_items: int) -> List[Dict[str, Any]]:
            seen = set()
            selected = []
            for item in items:
                grp = item["release_group"].lower()
                if grp not in seen:
                    seen.add(grp)
                    selected.append(item)
                    if len(selected) >= max_items:
                        break
            return selected

        selected_hevc = dedupe_by_group(hevc_list, max_hevc)
        selected_av1 = dedupe_by_group(av1_list, max_av1)

        # Combine
        final_selection = selected_hevc + selected_av1

        # Fallback if both empty: select best available candidate
        if not final_selection and candidates:
            candidates.sort(key=lambda x: -x["seeders"])
            final_selection.append(candidates[0])

        LOGGER.info(
            f"NyaaMatcher selected {len(final_selection)} sources for '{title_english}' S{season_number:02d}: "
            f"({len(selected_hevc)} HEVC, {len(selected_av1)} AV1)"
        )
        for s in final_selection:
            LOGGER.info(
                f"  -> [{s['release_group']}] {s['title']} | Codec: {s['codec']} | "
                f"BD: {s['is_bluray']} | Seeders: {s['seeders']} | Size: {s['size']}"
            )

        return final_selection

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    matcher = NyaaMatcher()
    for anime, s in [("A Couple of Cuckoos", 1), ("Blue Box", 1), ("Classroom of the Elite", 1)]:
        print(f"\n=== Searching batches for {anime} Season {s} ===")
        res = matcher.find_multi_source_batches(anime, season_number=s)
        for r in res:
            print(f"  [{r['release_group']}] (Rank {r.get('rank', 'N/A')}, Codec {r['codec']}, BD {r['is_bluray']}): {r['title']} ({r['size']}, {r['seeders']} seeders)")
