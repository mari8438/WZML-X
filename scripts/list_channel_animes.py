import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pyrogram import Client

API_ID = 32226674
API_HASH = "c03defc25ce571541f214b07ed51ce6a"
SESSION = "AQHPBdcAnEZfnuHmmgKmAdnimSOkgiCrLiiWYYRVG9D_jHRWrfUaA369827pLO7ra1LFuUQEeym3pMBXn-RyJn5xyn92LZ3jnfP8fp2QsmCNi_esoERO9o1cY0moRB8r_ha8Sf3YYbuNOtLlb3qsGo-duNXe1JcQ2ktJn58mNzqsYr0A2a_cUehxTMNuUe_AESCe2svHzpYnRBl3CgJf1RsW-w5tyS4QIBqYg3V4DsO9P87tCZYDooeif0acCVuPU_8w0RquVVvg5_8tYBWKYbX1WAQ3XUHO-vglqAK07D5Ae9EEIsHT3jEFTDhheZekeyxe1m2jm7mfvheApmC6FqpfJHbRVgAAAAIPtT73AA"
CHANNEL_ID = -1004335327733

def is_merged_season_file(duration_sec: int, text: str) -> bool:
    if duration_sec > 2400: # > 40 min is merged season or movie
        return True
    lower = text.lower()
    if any(k in lower for k in ["merged", "complete season", "all episodes", "full season", "all in one", "batch"]):
        return True
    # check for range e.g. E01-E12 or 01-12
    if re.search(r"E\d+[-_~]E?\d+|01[-_~]\d{2}", text, re.IGNORECASE) and not re.search(r"S\d+E\d+", text, re.IGNORECASE):
        return True
    return False

def clean_title_name(raw_title: str) -> str:
    # remove release tags, channel handles, brackets
    t = re.sub(r"\[.*?\]|\(.*?\)|🄰🅂|[@#]\w+", "", raw_title)
    # remove quality tags
    qual_pattern = r"\b(1080p|720p|2160p|480p|10bit|8bit|BluRay|BDRip|WEB-?DL|WEBRip|HDTV|[x×]26[45]|HEVC|AV1|Tamil|AAC|Dual-Audio|ESub|DDP\d\.\d)\b"
    t = re.sub(qual_pattern, "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(19|20)\d{2}\b", "", t)
    t = t.replace("_", " ").strip(" -_[]():")
    # standard replacements for common anime titles
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def parse_ep(text: str):
    # s_e regex
    s_e = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", text, re.IGNORECASE)
    if s_e:
        s = int(s_e.group(1))
        e = int(s_e.group(2))
        title = text[:s_e.start()].strip()
        if not title or len(title) < 2:
            title = text[s_e.end():].strip()
        return clean_title_name(title), s, e
    
    ep_match = re.search(r"(?:EP|Episode)[.\s_-]*(\d{1,4})", text, re.IGNORECASE)
    if ep_match:
        e = int(ep_match.group(1))
        title = text[:ep_match.start()].strip()
        return clean_title_name(title), 1, e
        
    num_match = re.search(r"[-_\s](\d{1,3})(?:v\d)?(?:\.|\s|$)", text)
    if num_match:
        e = int(num_match.group(1))
        title = text[:num_match.start()].strip()
        return clean_title_name(title), 1, e

    return clean_title_name(text), 1, 1

async def main():
    async with Client("list_animes", api_id=API_ID, api_hash=API_HASH, session_string=SESSION, in_memory=True) as app:
        print("Connected to Telegram Client successfully. Scanning channel...")
        anime_data = defaultdict(lambda: defaultdict(set))
        total_media = 0
        merged_skipped = 0

        async for msg in app.get_chat_history(CHANNEL_ID):
            media = msg.video or msg.document
            if not media:
                continue
            total_media += 1
            duration_sec = getattr(media, "duration", 0) or 0
            filename = getattr(media, "file_name", "") or ""
            caption = msg.caption or ""
            text = f"{filename} {caption}".strip()

            if is_merged_season_file(duration_sec, text):
                merged_skipped += 1
                continue

            title, season, ep = parse_ep(text)
            if not title or len(title) < 2:
                title = "Unknown Anime"
            anime_data[title][season].add(ep)

        print(f"\n--- SCAN RESULTS ---")
        print(f"Total media inspected: {total_media}")
        print(f"Merged single season files skipped: {merged_skipped}")
        print(f"Total episodic anime series identified: {len(anime_data)}")

        results = []
        for title, seasons in anime_data.items():
            total_eps = sum(len(eps) for eps in seasons.values())
            season_details = []
            for s_num in sorted(seasons.keys()):
                eps = sorted(list(seasons[s_num]))
                min_ep = eps[0] if eps else 0
                max_ep = eps[-1] if eps else 0
                count_ep = len(eps)
                season_details.append({
                    "season": s_num,
                    "count": count_ep,
                    "range": f"E{min_ep:02d}-E{max_ep:02d}"
                })
            results.append({
                "title": title,
                "total_eps": total_eps,
                "seasons": season_details
            })

        # Sort by total episodes descending (popular / major shows first)
        results.sort(key=lambda x: x["total_eps"], reverse=True)

        with open("/home/administrator/starfall/data/channel_animes.json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

        print("SUMMARY_OUTPUT_START")
        for idx, item in enumerate(results, 1):
            s_str = ", ".join([f"S{s['season']}: {s['count']} eps ({s['range']})" for s in item['seasons']])
            print(f"{idx}. {item['title']} | Total: {item['total_eps']} eps | {s_str}")
        print("SUMMARY_OUTPUT_END")

if __name__ == "__main__":
    asyncio.run(main())
