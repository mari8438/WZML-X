#!/usr/bin/env python3
"""
Starfall v1.7 4GB Episode-Aware Season Merge Planner (`merge_planner.py`)
========================================================================
Partitions whole episodes into balanced chunks so no merged file exceeds
the safe ceiling of 3,950 MiB (strictly below Telegram's 4,000 MiB limit).
Never splits across a single episode's timeline.
"""

import os
import re
import asyncio
import logging
import shutil
from typing import List, Dict, Any, Optional

LOGGER = logging.getLogger("SeasonMergePlanner")
TG_MAX_CHUNK_MIB = 3950.0  # Safe ceiling under 4000 MiB limit


def format_merged_filename(
    anime_title: str,
    season: int,
    start_ep: int,
    end_ep: int,
    part_num: int = 1,
    total_parts: int = 1,
    year: int = 2024,
    resolution: str = "1080p",
    bit: str = "10bit",
    medium: str = "BluRay",
    codec: str = "x265",
    languages: str = "Tamil",
    audio_codec: str = "AAC",
    audio_channels: str = "2.0",
    shortsub: str = "ESub",
    release_group: str = "Starfall",
    tag: str = "[@Anime_Starfall🥰]",
) -> str:
    """
    Standard format matching user autorename template:
    Single part:
      🄰🅂- [S1E01-12] Solo Leveling (2024) 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ Starfall ~ [@Anime_Starfall🥰].mkv
    Multi-part:
      🄰🅂- [S1E01-06] Solo Leveling Part 1 (2024) 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ Starfall ~ [@Anime_Starfall🥰].mkv
    """
    part_suffix = f" Part {part_num}" if total_parts > 1 else ""
    year_str = f"({year})" if year else ""
    safe_quality = "BluRay" if medium.lower() in ("bdrip", "bd-rip", "brrip", "bluray", "bdremux", "bd") else medium
    safe_codec = "x265" if codec.upper() in ("HEVC", "H.265", "H265") else codec
    clean_title = re.sub(r"[^\w\s-]", "", anime_title).strip()
    raw_fn = (
        f"🄰🅂- [S{season}E{start_ep:02d}-{end_ep:02d}] {clean_title}{part_suffix} {year_str} "
        f"{resolution} {bit} {safe_quality} {safe_codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~ "
        f"{release_group} ~ {tag}.mkv"
    )
    raw_fn = re.sub(r"\bCR\b\s*", "", raw_fn)
    raw_fn = re.sub(r"\s+", " ", raw_fn).strip()
    return raw_fn


class SeasonMergePlanner:
    """
    Partitions whole episodes into balanced chunks so no merged file exceeds 3,950 MiB.
    Never splits across a single episode's timeline.
    """

    @staticmethod
    def _extract_ep_num(filename: str) -> int:
        match = re.search(r'[Ee](\d+)', os.path.basename(filename))
        if match:
            return int(match.group(1))
        match2 = re.search(r'\b0*(\d{1,3})\b', os.path.basename(filename))
        return int(match2.group(1)) if match2 else 0

    @classmethod
    def plan_chunks(cls, episode_files: List[str]) -> List[Dict[str, Any]]:
        # Sort files by episode number
        episodes = []
        for ep_path in sorted(episode_files, key=cls._extract_ep_num):
            if not os.path.exists(ep_path):
                continue
            size_mib = os.path.getsize(ep_path) / (1024 * 1024)
            ep_num = cls._extract_ep_num(ep_path)
            episodes.append({"path": ep_path, "size_mib": size_mib, "ep_num": ep_num})

        if not episodes:
            return []

        total_size = sum(e["size_mib"] for e in episodes)
        if total_size <= TG_MAX_CHUNK_MIB:
            return [{
                "part_num": 1,
                "total_parts": 1,
                "episodes": [e["path"] for e in episodes],
                "start_ep": episodes[0]["ep_num"],
                "end_ep": episodes[-1]["ep_num"],
                "est_size_mib": total_size
            }]

        import math
        min_parts = math.ceil(total_size / TG_MAX_CHUNK_MIB)
        # Attempt balanced partitioning into min_parts
        target_size = total_size / min_parts

        chunks = []
        current_chunk = []
        current_size = 0.0

        for ep in episodes:
            # If current_chunk has items, check if adding ep would exceed TG_MAX_CHUNK_MIB
            # or if current_size has already reached target_size and we still have parts remaining
            remaining_episodes = len(episodes) - (sum(len(c) for c in chunks) + len(current_chunk))
            remaining_parts = min_parts - len(chunks)

            should_split = False
            if current_chunk:
                if (current_size + ep["size_mib"]) > TG_MAX_CHUNK_MIB:
                    should_split = True
                elif remaining_parts > 1 and current_size >= target_size * 0.95 and remaining_episodes >= (remaining_parts - 1):
                    should_split = True

            if should_split:
                chunks.append(current_chunk)
                current_chunk = [ep]
                current_size = ep["size_mib"]
            else:
                current_chunk.append(ep)
                current_size += ep["size_mib"]

        if current_chunk:
            chunks.append(current_chunk)

        total_parts = len(chunks)
        plan = []
        for idx, chunk in enumerate(chunks, 1):
            plan.append({
                "part_num": idx,
                "total_parts": total_parts,
                "episodes": [e["path"] for e in chunk],
                "start_ep": chunk[0]["ep_num"],
                "end_ep": chunk[-1]["ep_num"],
                "est_size_mib": sum(e["size_mib"] for e in chunk)
            })

        return plan


async def execute_merge_plan(
    plan: List[Dict[str, Any]],
    anime_title: str,
    season: int,
    output_dir: str,
    cover_photo_path: Optional[str] = "/home/administrator/starfall/data/intro_preroll.jpg",
    metadata_title: str = "[@Anime_Starfall🥰]",
) -> List[Dict[str, Any]]:
    """
    Executes mkvmerge for each chunk in the plan.
    Uses: mkvmerge --append-mode file --generate-chapters when-appending
    Attaches cover_photo_path as cover.jpg
    """
    os.makedirs(output_dir, exist_ok=True)
    results = []

    for item in plan:
        part_num = item["part_num"]
        total_parts = item["total_parts"]
        start_ep = item["start_ep"]
        end_ep = item["end_ep"]
        episodes = item["episodes"]

        out_name = format_merged_filename(
            anime_title=anime_title,
            season=season,
            start_ep=start_ep,
            end_ep=end_ep,
            part_num=part_num,
            total_parts=total_parts,
        )
        out_path = os.path.join(output_dir, out_name)

        first_ep = episodes[0]
        rest_eps = episodes[1:]

        cmd = [
            "mkvmerge",
            "--ui-language", "en_US",
            "-o", out_path,
            "--title", metadata_title,
            "--append-mode", "file",
            "--generate-chapters", "when-appending",
        ]

        if cover_photo_path and os.path.exists(cover_photo_path):
            cmd.extend([
                "--attachment-name", "cover.jpg",
                "--attachment-mime-type", "image/jpeg",
                "--attachment-description", "Starfall Official Release Cover",
                "--attach-file", cover_photo_path,
            ])

        cmd.append(first_ep)
        for ep in rest_eps:
            cmd.extend(["+", "--no-attachments", ep])

        LOGGER.info(
            f"📦 Merging Part {part_num}/{total_parts} (E{start_ep:02d}-E{end_ep:02d}) -> {out_name} "
            f"({len(episodes)} episodes, est {item['est_size_mib']:.1f} MiB)"
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode > 1:
            err = stderr.decode(errors="replace")
            LOGGER.error(f"❌ Failed to merge Part {part_num}: {err}")
            raise RuntimeError(f"mkvmerge failed (code {proc.returncode}): {err}")

        actual_size_mib = os.path.getsize(out_path) / (1024 * 1024)
        LOGGER.info(f"✅ Generated {out_name} ({actual_size_mib:.2f} MiB)")

        results.append({
            "part_num": part_num,
            "total_parts": total_parts,
            "start_ep": start_ep,
            "end_ep": end_ep,
            "path": out_path,
            "filename": out_name,
            "size_mib": actual_size_mib,
        })

    return results


if __name__ == "__main__":
    import glob
    logging.basicConfig(level=logging.INFO)
    files = glob.glob("/home/administrator/starfall/downloads/muxed_output/🄰🅂-*Solo Leveling*.mkv")
    plan = SeasonMergePlanner.plan_chunks(files)
    print("Plan:", plan)
