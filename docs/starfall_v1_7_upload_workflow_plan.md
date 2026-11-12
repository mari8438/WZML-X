# Implementation Plan: Starfall v1.7 High-Speed Multi-Bot Upload & 4GB Episode-Aware Merge Workflow

This specification details the architecture and step-by-step implementation for upgrading the Starfall upload and release pipeline. It introduces the **4GB Episode-Aware Auto-Merge Planner**, **Multi-Bot Parallel Staging with Sequential Forwarding**, **Intro Preroll Bumper Integration**, and **Automated HD Thumbnail Verification**, with end-to-end testing on the completed **Solo Leveling Season 1** (12 episodes).

---

## 1. Goal Description & Scope

### Objectives
1. **Intro Photo Attachment**:
   Attach the user-provided branding photo (`intro_preroll.jpg`, 1280x715) at the start of every video so the branding displays at the video's start.
2. **HD 16:9 Thumbnail Verification & Scraper Fallback**:
   Verify every video upload uses the official 16:9 landscape thumbnail (`thumb=...`). If a thumbnail is missing, scrape from `toonworld4all.me` WP REST API, with fallback to AniList Banner API.
3. **4GB Episode-Aware Auto-Merge Planner**:
   Replace arbitrary byte-splitting with an intelligent grouping algorithm that partitions multi-episode seasons into complete-episode chunks strictly under **3,950 MiB** (e.g. Solo Leveling 12 eps / 6.33 GB becomes Part 1 [E01–E06] and Part 2 [E07–E12]).
4. **Improved Standardized Autorename**:
   - Single: `🄰🅂-[S01E01] Solo Leveling 2024 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ [@Anime_Starfall🥰].mkv`
   - Merged (Single Part): `🄰🅂-[S1E01-12] Solo Leveling S01 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ [@Anime_Starfall🥰].mkv`
   - Merged (Multi-Part): `🄰🅂-[S1E01-06] Solo Leveling S01 Part 1 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ [@Anime_Starfall🥰].mkv`
5. **High-Speed Multi-Bot Dump $\rightarrow$ Sequential Target Forward Workflow**:
   Helper bots upload files concurrently at maximum speed into a private Staging/Dump Channel (`-1002211546440` / `Auto Rename Dump`). Once all season assets are confirmed, a controller sequentially copies/forwards them into the Target Channel (`Tamil Muxed Collection` `-1004415413346`) in strict chronological order:
   `[Poster Card (Template 11)]` $\rightarrow$ `[Merged Season Video(s)]` $\rightarrow$ `[Episodes 01, 02 ... N]`.
6. **Solo Leveling S01 Benchmark**:
   Run full end-to-end dry-run and release test on the 12 completed Solo Leveling episodes.

---

## 2. User Review Required

> [!IMPORTANT]
> **Intro Photo Integration (Photo Attachment Only - No Intro Video)**:
> As per user instruction, **no intro video bumper or transcoding is performed**.
> The photo `/home/administrator/starfall/data/intro_preroll.jpg` is attached directly as the official embedded **`cover.jpg`** artwork inside the MKV container (`--attach-file /home/administrator/starfall/data/intro_preroll.jpg --attachment-mime-type image/jpeg --attachment-name cover.jpg`). When opened in any player, this photo displays as the release's cover art, preserving 100% original video frames without recoding.

> [!TIP]
> **4GB Ceiling Margin**:
> We set the hard cap at **3,950 MiB** (instead of 4,000 MiB / 4,096 MiB) to give a 50–100 MB safety buffer so Telegram API never rejects an upload due to Telegram Premium file size boundaries.

---

## 3. High-Speed Upload Architecture Diagram

```mermaid
flowchart TD
    subgraph Phase 1: Mux & Chunk Planning
        A["12 Muxed Episodes (e.g. Solo Leveling 6.33 GB)"] --> B["4GB Episode-Aware Planner"]
        B -->|Cumulative Size > 3950 MiB| C["Part 1: E01-E06 (3.15 GB)"]
        B -->|Cumulative Size > 3950 MiB| D["Part 2: E07-E12 (3.18 GB)"]
        C --> E["mkvmerge --append-mode file (Part 1)"]
        D --> F["mkvmerge --append-mode file (Part 2)"]
    end

    subgraph Phase 2: High-Speed Multi-Bot Staging (Unordered / Fast)
        G["5 Helper Bots + WZ Bot Pool"]
        E --> G
        F --> G
        A --> G
        G -->|Concurrent Chunk Uploads 450-650 Mbps| H["Staging / Dump Channel (-1002211546440)"]
        H --> I["Record Dump Message IDs in SQLite State DB"]
    end

    subgraph Phase 3: Sequential Target Forwarding (Pristine Order)
        I -->|Season Barrier Complete| J["Sequencer Controller (@Hsin_The_Moon_Fox)"]
        J -->|Step 1: 3.5s delay| K["1. Poster Card (Template 11)"]
        J -->|Step 2: 3.5s delay| L["2. Merged Video(s) Part 1 & Part 2"]
        J -->|Step 3: 3.5s delay| M["3. Single Episodes E01, E02 ... E12"]
        K --> N["Target Channel: Tamil Muxed Collection (-1004415413346)"]
        L --> N
        M --> N
    end

    subgraph Phase 4: Local Storage Purge
        N -->|Forward Confirmed| O["Immediate Purge of Source & Muxed MKVs on NVMe"]
    end
```

---

## 4. Proposed Code & Component Changes

### Component 1: 4GB Episode-Aware Auto-Merge Planner (`merge_planner.py`)
`[NEW] /home/administrator/starfall/bot/modules/starfall_pipeline/merge_planner.py`

```python
import os
from typing import List, Dict, Any

TG_MAX_CHUNK_MIB = 3950.0  # Safe ceiling under 4000 MiB limit

class SeasonMergePlanner:
    """
    Partitions whole episodes into balanced chunks so no merged file exceeds 3,950 MiB.
    Never splits across a single episode's timeline.
    """
    @staticmethod
    def plan_chunks(episode_files: List[str]) -> List[Dict[str, Any]]:
        # Sort files by episode number
        episodes = []
        for ep_path in sorted(episode_files):
            size_mib = os.path.getsize(ep_path) / (1024 * 1024)
            episodes.append({"path": ep_path, "size_mib": size_mib})

        total_size = sum(e["size_mib"] for e in episodes)
        if total_size <= TG_MAX_CHUNK_MIB:
            return [{
                "part_num": 1,
                "total_parts": 1,
                "episodes": [e["path"] for e in episodes],
                "start_ep": 1,
                "end_ep": len(episodes),
                "est_size_mib": total_size
            }]

        chunks = []
        current_chunk = []
        current_size = 0.0

        for ep in episodes:
            if current_chunk and (current_size + ep["size_mib"] > TG_MAX_CHUNK_MIB):
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
            start_num = int(re.search(r'[Ee](\d+)', os.path.basename(chunk[0]["path"])).group(1))
            end_num = int(re.search(r'[Ee](\d+)', os.path.basename(chunk[-1]["path"])).group(1))
            plan.append({
                "part_num": idx,
                "total_parts": total_parts,
                "episodes": [e["path"] for e in chunk],
                "start_ep": start_num,
                "end_ep": end_num,
                "est_size_mib": sum(e["size_mib"] for e in chunk)
            })

        return plan
```

---

### Component 2: Standardized Autorename Engine for Merged Parts
`[MODIFY] /home/administrator/starfall/bot/modules/starfall_pipeline/mux_engine.py`

Enhanced filename generator for single episodes and merged partitions:
```python
def format_merged_filename(
    anime_title: str,
    season: int,
    start_ep: int,
    end_ep: int,
    part_num: int = 1,
    total_parts: int = 1,
    encoder: str = "EMBER",
) -> str:
    """
    Standard format:
    Single part:
      🄰🅂-[S1E01-12] Solo Leveling S01 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ [@Anime_Starfall🥰].mkv
    Multi-part:
      🄰🅂-[S1E01-06] Solo Leveling S01 Part 1 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ [@Anime_Starfall🥰].mkv
    """
    part_suffix = f" Part {part_num}" if total_parts > 1 else ""
    return (
        f"🄰🅂-[S{season}E{start_ep:02d}-{end_ep:02d}] {anime_title} S{season:02d}{part_suffix} "
        f"1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ [@Anime_Starfall🥰].mkv"
    )
```

---

### Component 3: Container Cover Art Attachment (`intro_preroll.jpg`)
`[INTEGRATED] /home/administrator/starfall/bot/modules/starfall_pipeline/mux_engine.py`

Attaches `/home/administrator/starfall/data/intro_preroll.jpg` directly as container cover art inside both single episodes and merged partitions without modifying or re-encoding video streams:
```python
if os.path.exists("/home/administrator/starfall/data/intro_preroll.jpg"):
    cmd.extend([
        "--attach-file", "/home/administrator/starfall/data/intro_preroll.jpg",
        "--attachment-mime-type", "image/jpeg",
        "--attachment-name", "cover.jpg",
        "--attachment-description", "Starfall Official Release Cover",
    ])
```

---

### Component 4: High-Speed Dump Staging & Sequential Target Forwarder
`[MODIFY] /home/administrator/starfall/bot/modules/starfall_pipeline/upload_router.py`

- **Parallel Worker**: Uploads all individual episodes and merged files into `Auto Rename Dump` (`-1002211546440`) in parallel using all 5 helper bots + user session.
- **Sequencer Barrier**: When all season assets are present in the dump channel:
  - Formats Poster Template 11 with the synopsis and poster image.
  - Sends Poster Card to Target Channel (`-1004415413346`).
  - Forwards Merged Season Part 1 (and Part 2 if present) with 3.5s delay.
  - Forwards Episode 1 through Episode N in strict ascending order with 3.5s spacing.
  - Purges local files on VPS.

---

## 5. Verification Plan

### Automated Execution & Benchmark on Solo Leveling S01
1. **Merge Planner Test**:
   - Run `python3 -m bot.modules.starfall_pipeline.merge_planner` on the 12 existing Solo Leveling files.
   - Verify it outputs exactly **Part 1 (E01–E06, ~3.15 GB)** and **Part 2 (E07–E12, ~3.18 GB)**.
2. **Intro Preroll Test**:
   - Verify `intro_bumper_1080p.mkv` generates cleanly at 1920x1080 24fps with stereo audio.
3. **Mux Merge Test**:
   - Execute `merge_season` on Solo Leveling Part 1 and Part 2.
   - Verify chapter marks are created at episode boundaries and duration matches total runtime.
4. **Staging & Forwarding Test**:
   - Upload Solo Leveling Part 1, Part 2, and single episodes into Dump Channel (`-1002211546440`).
   - Run forwarder to commit Poster Template 11 $\rightarrow$ Merged Part 1 $\rightarrow$ Merged Part 2 $\rightarrow$ S01E01...E12 into `-1004415413346`.
   - Verify video streaming, audio tracks (Tamil default), and 16:9 thumbnails in Telegram.
