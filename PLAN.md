# Automated Tamil Anime Audio-Muxing & Telegram Leech Pipeline
## Production Systems Architecture & Engineering Specification (Starfall v1.6 - Asset Harvesting & Presentation Edition)

**Target Host:** Ubuntu 24.04 LTS (16 vCPU Cores, 32 GB RAM, 756 GB NVMe SSD)  
**Storage Quota:** 500 GB dedicated allocation (Hard Cap: < 450 GB active NVMe footprint)  
**Telegram Infrastructure:** 1 Premium User Session (`@Hsin_The_Moon_Fox` - Admin in both channels) + 5 Helper Bots (`@Anime_11_uplode_bot`, `@Anime_14_uplode_bot`, `@Anime_15_uplode_bot`, `@Starfall_xrobot`, `@Do_up_3_robot`)  
**Source Harvest Channel:** `Tamil Anime Starfall Collection` (`-1004335327733`) — Extract Tamil AAC audio, Poster photos, and 16:9 landscape logo thumbnails  
**Target Upload Channel:** `Tamil Muxed Collection` (`-1004415413346`) — Final muxed anime releases  
**Quality Naming Rule:** Strictly use **`BluRay`** (Never write `BDRip`)  
**Database:** SQLite 3 in WAL Mode (`data/starfall_state.db`)  
**RAM Workspace:** 16 GB `/dev/shm/starfall_mux`  

---

## 1. Zero-Failure Asset Harvesting (Posters & 16:9 Thumbnails)

Instead of relying on web scrapers or synthetic generation, the pipeline harvests original visual assets directly from source channel `-1004335327733`:

### 1.1 Original Poster Card Forwarding / Copying
- For each anime season, the harvester identifies the original **Poster Photo Message** (e.g., Msg #3977 for *Tomb Raider King*).
- The poster card image and formatted Unicode caption are copied/forwarded directly to target channel `-1004415413346` as **Post #1**.

### 1.2 Exact 16:9 Landscape Logo Thumbnail Extraction
- Source video messages in `-1004335327733` already feature official 16:9 widescreen landscape thumbnails (`320x180` / `1280x720`) with custom anime title logos.
- When demuxing Tamil audio from any episode message, the harvester also extracts the video thumbnail:
  ```python
  if message.video and message.video.thumbs:
      thumb_file_id = message.video.thumbs[-1].file_id
      await client.download_media(thumb_file_id, file_name=f"thumbnails/{clean_title}_S{season}_thumb.jpg")
  ```
- This harvested thumbnail is applied to:
  1. The **Full Season Merged video file**
  2. All **individual single episodes (`S01E01`, `S01E02`...)**

---

## 2. Naming & Presentation Standards

### 2.1 The "BluRay" Rule (No BDRip)
Everywhere in the pipeline:
- Replace any occurrence of `BDRip`, `BD-Rip`, or `BRRip` with **`BluRay`**.
- Example filename:
  `🄰🅂-[S01E01] Tomb Raider King 2026 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ [@Anime_Starfall🥰].mkv`
- Example merge filename:
  `🄰🅂-[S1E01-12] Tomb Raider King S01 1080p 10bit BluRay x265 [Tamil-AAC 2.0] ESub ~ [@Anime_Starfall🥰].mkv`

### 2.2 Probed Duration (Eliminate 00:00 Bug)
- Probe exact video duration (seconds) and video dimensions (`1920x1080`) with `ffprobe`.
- Pass `duration=int(dur_sec), width=1920, height=1080, supports_streaming=True` into `send_video()`. Telegram will display the exact runtime (e.g. `23:02` or `4:36:45`).

### 2.3 Periodic Subtitle Branding
- Auto-calculate across the entire video duration:
  - **10-second display** every **5-minute gap (300s)**
  - Text: `Join our Telegram channel [@Anime_Starfall🥰]`

---

## 3. Strict 3-Step Upload Order Per Anime Season

```
┌────────────────────────────────────────────────────────────────────────┐
│             TARGET CHANNEL UPLOAD ORDER (-1004415413346)               │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
       ┌─────────────────────────────────────────────────────────┐
       │ [1. SEASON POSTER CARD]                                 │
       │  • Forwarded/copied directly from old channel           │
       │  • Exact card UI + Unicode details caption              │
       └────────────────────────────┬────────────────────────────┘
                                    │
                                    ▼
       ┌─────────────────────────────────────────────────────────┐
       │ [2. FULL-SEASON MERGED VIDEO FILE]                      │
       │  • Merged via `mkvmerge --append-mode file`             │
       │  • Harvested 16:9 landscape logo thumbnail              │
       │  • Probed exact duration (No 00:00 bug)                 │
       │  • Quality tag: `BluRay` (Never `BDRip`)                │
       │  • Uploaded via @Hsin_The_Moon_Fox if > 1,980 MiB       │
       └────────────────────────────┬────────────────────────────┘
                                    │
                                    ▼
       ┌─────────────────────────────────────────────────────────┐
       │ [3. SEQUENTIAL SINGLE EPISODES (E01, E02, E03...)]      │
       │  • Muxed 1080p BluRay video + Synced Tamil AAC + ESub   │
       │  • Harvested 16:9 landscape logo thumbnail              │
       │  • Probed exact duration (No 00:00 bug)                 │
       │  • Auto-Rename: 🄰🅂-[S{s}E{e}] {name} ... BluRay ...    │
       │  • Dispatched round-robin across 5 Helper Bots          │
       └─────────────────────────────────────────────────────────┘
```
