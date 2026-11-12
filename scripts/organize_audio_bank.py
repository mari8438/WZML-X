#!/usr/bin/env python3
"""
scripts/organize_audio_bank.py
------------------------------
Cleans, deduplicates, renames, and organizes the flat audio bank directory
/home/administrator/starfall/downloads/vt_audio_bank/8814651149
into clean, hierarchical anime and season folders.

Deduplication Rule:
- When multiple audio files exist for the same anime, season, and episode:
  Prioritize EAC3/DDP > AAC/M4A > Opus.
  Delete inferior or exact duplicate copies.
- Purge leftover test muxes (e.g. 'Tamil - 🄰🅂...').

Usage:
  python3 scripts/organize_audio_bank.py --dry-run
  python3 scripts/organize_audio_bank.py --execute
"""

import os
import re
import sys
import shutil
import argparse
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

AUDIO_BANK_DIR = "/home/administrator/starfall/downloads/vt_audio_bank/8814651149"

# Format preference score (higher is better)
FORMAT_PRIORITY = {
    ".eac3": 100,
    ".ac3": 90,
    ".m4a": 80,
    ".aac": 70,
    ".flac": 65,
    ".opus": 50,
    ".mp3": 40,
}

KNOWN_TITLE_CORRECTIONS = {
    "kimetsunoyaiba": "Demon Slayer - Kimetsu no Yaiba",
    "demonslayer": "Demon Slayer - Kimetsu no Yaiba",
    "demonslayerkimetsunoyaiba": "Demon Slayer - Kimetsu no Yaiba",
    "shingekinokyojin": "Attack on Titan",
    "attackontitan": "Attack on Titan",
    "jujutsukaisen": "Jujutsu Kaisen",
    "sololeveling": "Solo Leveling",
    "deathnote": "Death Note",
    "tokyoghou": "Tokyo Ghoul",
    "tokyoghoul": "Tokyo Ghoul",
    "myheroacademia": "My Hero Academia",
    "bokunoheroacademia": "My Hero Academia",
    "kaiju8gou": "Kaiju No. 8",
    "kaijuno8": "Kaiju No. 8",
    "mobpsycho100": "Mob Psycho 100",
    "vinlandsaga": "Vinland Saga",
    "windbreaker": "Wind Breaker",
    "acoupleofcuckoos": "A Couple of Cuckoos",
    "byebyeearth": "Bye Bye, Earth",
    "classroomoftheelite": "Classroom of the Elite",
}


def clean_anime_title(raw: str) -> str:
    """Normalize anime title to clean title-case string."""
    clean = re.sub(r"\[.*?\]|\(.*?\)", "", raw)
    clean = re.sub(r"[-_.]+", " ", clean).strip()
    norm = re.sub(r"[^a-zA-Z0-9]", "", clean).lower()
    
    for k, v in KNOWN_TITLE_CORRECTIONS.items():
        if norm == k or (len(norm) >= 6 and k in norm):
            return v
            
    # Capitalize title properly
    words = clean.split()
    clean_words = []
    for w in words:
        if w.lower() in ("no", "of", "the", "in", "and", "a", "an", "to", "for", "with", "on", "at", "by"):
            clean_words.append(w.lower())
        else:
            clean_words.append(w.capitalize())
    res = " ".join(clean_words).strip()
    if res and res[0].islower():
        res = res[0].upper() + res[1:]
    return res or "Unknown Anime"


def parse_audio_file(fname: str) -> Tuple[str, str, Optional[int], Optional[int], bool]:
    """
    Parses a filename into:
    (category, anime_title, season, episode, is_movie)
    category: 'episode', 'movie', 'test_garbage', 'unknown'
    """
    ext = os.path.splitext(fname)[1].lower()
    
    # 1. Leftover test files
    if "🄰🅂-" in fname or fname.startswith("Tamil - 🄰🅂"):
        return "test_garbage", "", None, None, False

    clean = re.sub(r"^(Tamil\s*-\s*(@\w+\s*-\s*)?)", "", fname, flags=re.I)
    clean = re.sub(r"^\[.*?\]\s*", "", clean).strip()

    # 2. Check for movies
    movie_indicators = ("movie", "infinity castle", "mugen train movie", "heroes rising", "relight special", "scarlet (2025)", "ryusui")
    is_movie = any(k in clean.lower() for k in movie_indicators) or (re.search(r"\b(19\d\d|20\d\d)\b", clean) and not re.search(r"[Ss]\d+", clean))
    
    if is_movie:
        # Extract title
        m_title = re.sub(r"\b(19\d\d|20\d\d)\b.*", "", clean).strip(" -_[]()")
        # Special handlers
        if "ryusui" in clean.lower() or "dr. stone" in clean.lower():
            return "movie", "Dr. Stone", None, None, True
        if "infinity castle" in clean.lower():
            return "movie", "Demon Slayer - Kimetsu no Yaiba", None, None, True
        if "mugen train" in clean.lower() and "movie" in clean.lower():
            return "movie", "Demon Slayer - Kimetsu no Yaiba", None, None, True
        if "heroes rising" in clean.lower():
            return "movie", "My Hero Academia", None, None, True
        if "jujutsu kaisen 0" in clean.lower():
            return "movie", "Jujutsu Kaisen", None, None, True
        
        parsed_title = clean_anime_title(m_title if m_title else clean)
        return "movie", parsed_title, None, None, True

    # 3. Check for SxxExx
    m_se = re.search(r"^(.*?)\s*[-_ ]*[Ss](\d{1,2})[Ee](\d{1,3})", clean)
    if m_se:
        raw_title = m_se.group(1).strip(" -_")
        s = int(m_se.group(2))
        ep = int(m_se.group(3))
        title = clean_anime_title(raw_title)
        return "episode", title, s, ep, False

    # 4. Check for E(\d+)
    m_e = re.search(r"^(.*?)\s*[-_ ]*E(\d{1,3})\b", clean)
    if m_e:
        raw_title = m_e.group(1).strip(" -_")
        ep = int(m_e.group(2))
        title = clean_anime_title(raw_title)
        return "episode", title, 1, ep, False

    # 5. Check for Episode number e.g. - 01
    m_num = re.search(r"^(.*?)\s*[-_ ]+(\d{2,3})\b", clean)
    if m_num:
        raw_title = m_num.group(1).strip(" -_")
        ep = int(m_num.group(2))
        title = clean_anime_title(raw_title)
        return "episode", title, 1, ep, False

    return "unknown", "Unknown", None, None, False


def scan_and_plan():
    files = [f for f in os.listdir(AUDIO_BANK_DIR) if os.path.isfile(os.path.join(AUDIO_BANK_DIR, f))]
    
    garbage_to_delete = []
    movies_to_move = []
    episodes_map = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    unknown_files = []

    for f in sorted(files):
        cat, title, s, ep, is_movie = parse_audio_file(f)
        f_path = os.path.join(AUDIO_BANK_DIR, f)
        sz = os.path.getsize(f_path)
        ext = os.path.splitext(f)[1].lower()

        if cat == "test_garbage":
            garbage_to_delete.append(f)
        elif cat == "movie":
            movies_to_move.append((title, f))
        elif cat == "episode":
            episodes_map[title][s][ep].append((f, sz, ext))
        else:
            unknown_files.append(f)

    # Process duplicates: for each title, season, episode
    episodes_to_keep = []
    episodes_to_delete = []

    for title, seasons in episodes_map.items():
        for s, eps in seasons.items():
            for ep, flist in eps.items():
                if len(flist) == 1:
                    episodes_to_keep.append((title, s, ep, flist[0][0]))
                else:
                    # Sort candidates: prioritize format priority, then file size
                    flist.sort(key=lambda x: (FORMAT_PRIORITY.get(x[2], 0), x[1]), reverse=True)
                    best = flist[0]
                    episodes_to_keep.append((title, s, ep, best[0]))
                    for dup in flist[1:]:
                        episodes_to_delete.append((title, s, ep, dup[0], dup[1]))

    return garbage_to_delete, movies_to_move, episodes_to_keep, episodes_to_delete, unknown_files


def run(execute: bool = False):
    print("=" * 70)
    print("🧹 STARFALL AUDIO BANK ORGANIZER & DEDUPLICATOR")
    print(f"Directory: {AUDIO_BANK_DIR}")
    print(f"Mode: {'EXECUTE (Modifying files)' if execute else 'DRY RUN (Simulating)'}")
    print("=" * 70)

    garbage, movies, keep_eps, delete_eps, unknowns = scan_and_plan()

    print(f"\n📊 Scan Results:")
    print(f"  • Total files processed: {len(garbage) + len(movies) + len(keep_eps) + len(delete_eps) + len(unknowns)}")
    print(f"  • Test garbage to purge: {len(garbage)} files")
    print(f"  • Duplicate copies to delete: {len(delete_eps)} files")
    print(f"  • Episodes to organize: {len(keep_eps)} files")
    print(f"  • Movies to organize: {len(movies)} files")
    print(f"  • Unknown files: {len(unknowns)} files")

    if delete_eps:
        print(f"\n🗑️ Sample Duplicate Files to be Removed:")
        for t, s, ep, fname, sz in delete_eps[:15]:
            print(f"  - [{t} S{s:02d}E{ep:02d}] {fname} ({sz / (1024*1024):.1f} MB)")

    if garbage:
        print(f"\n🗑️ Sample Garbage/Leftover Test Files to be Removed:")
        for g in garbage[:10]:
            print(f"  - {g}")

    if not execute:
        print("\n⚠️ Dry-run complete. Re-run with --execute to perform file operations.")
        return

    # EXECUTION PHASE
    print("\n🚀 Executing file reorganizations and cleanups...")

    # 1. Delete test garbage
    deleted_garbage_count = 0
    for g in garbage:
        try:
            os.remove(os.path.join(AUDIO_BANK_DIR, g))
            deleted_garbage_count += 1
        except Exception as e:
            print(f"Error deleting {g}: {e}")
    print(f"✅ Deleted {deleted_garbage_count} leftover test files.")

    # 2. Delete duplicate redundant files
    deleted_dups_count = 0
    freed_bytes = 0
    for _, _, _, fname, sz in delete_eps:
        try:
            os.remove(os.path.join(AUDIO_BANK_DIR, fname))
            deleted_dups_count += 1
            freed_bytes += sz
        except Exception as e:
            print(f"Error deleting {fname}: {e}")
    print(f"✅ Deleted {deleted_dups_count} duplicate files (Freed {freed_bytes / (1024*1024):.1f} MB).")

    # 3. Move and rename movies
    moved_movies_count = 0
    for title, fname in movies:
        src = os.path.join(AUDIO_BANK_DIR, fname)
        if not os.path.exists(src):
            continue
        dest_dir = os.path.join(AUDIO_BANK_DIR, title, "Movies")
        os.makedirs(dest_dir, exist_ok=True)
        ext = os.path.splitext(fname)[1].lower()
        clean_m_name = re.sub(r"^(Tamil\s*-\s*(@\w+\s*-\s*)?)", "", fname, flags=re.I)
        clean_m_name = re.sub(r"^\[.*?\]\s*", "", clean_m_name)
        new_name = f"[Movie] {clean_m_name}" if not clean_m_name.startswith("[Movie]") else clean_m_name
        dest = os.path.join(dest_dir, new_name)
        try:
            shutil.move(src, dest)
            moved_movies_count += 1
        except Exception as e:
            print(f"Error moving movie {fname}: {e}")
    print(f"✅ Moved {moved_movies_count} movies into <Anime>/Movies/ directories.")

    # 4. Move and rename episodes into <Anime>/Season <XX>/
    moved_eps_count = 0
    for title, s, ep, fname in keep_eps:
        src = os.path.join(AUDIO_BANK_DIR, fname)
        if not os.path.exists(src):
            continue
        dest_dir = os.path.join(AUDIO_BANK_DIR, title, f"Season {s:02d}")
        os.makedirs(dest_dir, exist_ok=True)
        ext = os.path.splitext(fname)[1].lower()
        new_name = f"[S{s:02d}E{ep:02d}] {title}{ext}"
        dest = os.path.join(dest_dir, new_name)
        try:
            shutil.move(src, dest)
            moved_eps_count += 1
        except Exception as e:
            print(f"Error moving {fname}: {e}")
    print(f"✅ Successfully organized {moved_eps_count} episode audio files into clean Anime/Season folders.")

    print("\n🎉 Audio Bank Reorganization and Deduplication Complete!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Starfall Audio Bank Organizer")
    parser.add_argument("--execute", action="store_true", help="Execute reorganization")
    parser.add_argument("--dry-run", action="store_true", help="Simulate reorganization without modifying files")
    args = parser.parse_args()

    if not args.execute and not args.dry_run:
        print("Please specify --dry-run or --execute")
        sys.exit(1)

    run(execute=args.execute)
