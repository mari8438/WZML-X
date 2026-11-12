#!/usr/bin/env python3
"""
scripts/download_and_standardize_thumbnails.py
---------------------------------------------
Downloads official high-resolution 16:9 landscape banners from AniList
for Demon Slayer (S01-S04) and Jujutsu Kaisen (S01-S02, Movie 0),
crops/resizes them to 1280x720 JPEG, and standardizes thumbnail filenames.
"""

import os
import requests
from PIL import Image
import io

THUMBS_DIR = "/home/administrator/starfall/data/thumbnails"
os.makedirs(THUMBS_DIR, exist_ok=True)

BANNERS = {
    # Demon Slayer: Kimetsu no Yaiba
    "Demon_Slayer_Kimetsu_no_Yaiba_S01_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/101922-33MtJGsUSxga.jpg",
    "Demon_Slayer_Kimetsu_no_Yaiba_S02_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/142329-i413SzLmToZN.jpg",
    "Demon_Slayer_Kimetsu_no_Yaiba_S03_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/145139-V01Prh6UzfRk.jpg",
    "Demon_Slayer_Kimetsu_no_Yaiba_S04_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/166240-YdxoEhrfwNk0.jpg",
    
    # Jujutsu Kaisen
    "JUJUTSU_KAISEN_S01_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/113415-jQBSkxWAAk83.jpg",
    "JUJUTSU_KAISEN_S02_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/145064-esDtAY2He7sk.jpg",
    "JUJUTSU_KAISEN_0_Movie_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/131573-3veuVz5p0z2I.jpg",
    
    # Attack on Titan
    "Attack_on_Titan_S01_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/16498-8jpFCOcDmneX.jpg",
    "Attack_on_Titan_S02_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/20958-i0eUf2pXQv82.jpg",
    "Attack_on_Titan_S03_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/99147-yN4n8r6f52s6.jpg",
    "Attack_on_Titan_S04_landscape.jpg": "https://s4.anilist.co/file/anilistcdn/media/anime/banner/110277-ihST22sTfZvO.jpg",
}


def process_image(url: str, dest_path: str):
    print(f"📥 Downloading banner from {url}...")
    headers = {"User-Agent": "StarfallPipeline/1.3"}
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200 or len(resp.content) < 1000:
        print(f"❌ Failed to download {url} (status {resp.status_code})")
        return False

    with Image.open(io.BytesIO(resp.content)) as im:
        im = im.convert("RGB")
        target_w, target_h = 1280, 720
        src_w, src_h = im.size

        # Fit into 16:9 aspect ratio using center crop
        src_ratio = src_w / src_h
        target_ratio = target_w / target_h

        if src_ratio > target_ratio:
            # Source is wider: crop width
            new_w = int(src_h * target_ratio)
            left = (src_w - new_w) // 2
            im = im.crop((left, 0, left + new_w, src_h))
        elif src_ratio < target_ratio:
            # Source is taller: crop height
            new_h = int(src_w / target_ratio)
            top = (src_h - new_h) // 2
            im = im.crop((0, top, src_w, top + new_h))

        im_resized = im.resize((target_w, target_h), Image.Resampling.LANCZOS)
        im_resized.save(dest_path, "JPEG", quality=92, optimize=True)
        sz = os.path.getsize(dest_path)
        print(f"✅ Saved HD thumbnail: {dest_path} ({sz / 1024:.1f} KB, 1280x720)")
        return True


def main():
    print("=" * 60)
    print("🎨 DOWNLOADING & STANDARDIZING OFFICIAL 16:9 LANDSCAPE THUMBNAILS")
    print("=" * 60)

    for fname, url in BANNERS.items():
        dest = os.path.join(THUMBS_DIR, fname)
        process_image(url, dest)

    print("\n🎉 All landscape thumbnails standardized!")


if __name__ == "__main__":
    main()
