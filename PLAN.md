# Automated Tamil Anime Audio-Muxing & Telegram Leech Pipeline
## Production Systems Architecture & Engineering Specification (Starfall v1.5 - Periodic Branding & Metadata Edition)

**Target Host:** Ubuntu 24.04 LTS (16 vCPU Cores, 32 GB RAM, 756 GB NVMe SSD)  
**Storage Quota:** 500 GB dedicated allocation (Hard Cap: < 450 GB active NVMe footprint)  
**Telegram Infrastructure:** 1 Premium User Session (`@Hsin_The_Moon_Fox` - Admin in both channels) + 5 Helper Bots (`@Anime_11_uplode_bot`, `@Anime_14_uplode_bot`, `@Anime_15_uplode_bot`, `@Starfall_xrobot`, `@Do_up_3_robot`)  
**Source Harvest Channel:** `Tamil Anime Starfall Collection` (`-1004335327733`) — Extract episodic Tamil AAC audios (skip merged files)  
**Target Upload Channel:** `Tamil Muxed Collection` (`-1004415413346`) — Final muxed anime releases  
**Execution Cadence:** Continuous batches of 20 anime series (starting with most popular series)  
**Database:** SQLite 3 in WAL Mode (`data/starfall_state.db`)  
**RAM Workspace:** 16 GB `/dev/shm/starfall_mux`  

---

## 1. Periodic Watermark Subtitle Generator (Full Video Duration)

Instead of a single subtitle at the start of the video, the subtitle generator dynamically reads the probed video duration ($D_{\text{total}}$) and generates recurring subtitle events across the **entire video duration**:

### 1.1 Timing Formula
- **Display Duration:** **10 seconds**
- **Interval Gap:** **5 minutes (300 seconds)**
- Starting at timestamp `00:00:05` (5 seconds in to allow intro fade):
  $$\text{Start}_k = 5 + k \times (300 + 10) \text{ seconds} \quad (k = 0, 1, 2, \dots \text{ while } \text{Start}_k + 10 \le D_{\text{total}})$$

### 1.2 Subtitle Content & Styling
- **Text:** `Join our Telegram channel [@Anime_Starfall🥰]`
- **Format:** Clean ASS (Advanced SubStation Alpha) subtitle track positioned at the top-center or bottom-center with subtle transparency and border outline:
```ass
[Script Info]
Title: Starfall Branding Track
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: StarfallBrand,Arial,36,&H20FFFFFF,&H00000000,&H80000000,&H80000000,1,0,0,0,100,100,0,0,1,2,1,8,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:05.00,0:00:15.00,StarfallBrand,,0,0,0,,Join our Telegram channel [@Anime_Starfall🥰]
Dialogue: 0,0:05:25.00,0:05:35.00,StarfallBrand,,0,0,0,,Join our Telegram channel [@Anime_Starfall🥰]
Dialogue: 0,0:10:45.00,0:10:55.00,StarfallBrand,,0,0,0,,Join our Telegram channel [@Anime_Starfall🥰]
Dialogue: 0,0:16:05.00,0:16:15.00,StarfallBrand,,0,0,0,,Join our Telegram channel [@Anime_Starfall🥰]
... (repeats dynamically across entire episode or full-season merge duration)
```

---

## 2. Complete Metadata Tagging (From user_settings.json)

When muxing video, audio, and subtitle streams with `mkvmerge`, the engine injects container-level and stream-level metadata tags matching `user_settings.json`:

### 2.1 Container-Level Global Tags
- **Title:** `[@Anime_Starfall🥰]`
- **Artist / Publisher:** `[@Anime_Starfall🥰]`
- **Genre:** `Anime`
- **Copyright:** `[@Anime_Starfall🥰]`
- **Comment:** `Encoded by [@Anime_Starfall🥰]`
- **Encoder:** `FFmpeg / MKVToolNix`
- **Description / Synopsis:** `[@Anime_Starfall🥰]`

### 2.2 Track-Level Metadata
- **Video Track:**
  - Name / Title: `[@Anime_Starfall🥰]`
  - Handler Name: `[@Anime_Starfall🥰]`
- **Tamil Audio Track:**
  - Name / Title: `[@Anime_Starfall🥰] - Tamil`
  - Language: `tam` (Tamil)
  - Default Track: `Yes`
- **Subtitle Tracks:**
  - Main Subtitle Track: `[@Anime_Starfall🥰] - English` (Language: `eng`)
  - Periodic Branding Track: `[@Anime_Starfall🥰]` (Default: `No`, Forced: `No`)

---

## 3. Strict Upload Sequencing Architecture

Uploads to `-1004415413346` follow this strict sequence:

1. **POST 1: SEASON POSTER WITH EXPANDABLE DETAILS**
   - High-Resolution Season Poster photo (`send_photo`).
   - Formatted caption from `user_settings.json` (Unicode frame borders, rating, genres, quality, expandable blockquote synopsis, `Dropped By ➤ [@Anime_Starfall🥰]`).
2. **POST 2: FULL-SEASON MERGED VIDEO FILE**
   - Merged video via `mkvmerge --append-mode file`.
   - 16:9 widescreen landscape thumbnail at 20% timestamp.
   - Probed exact duration (hh:mm:ss) + 1920x1080 dimensions.
   - Dispatched via `@Hsin_The_Moon_Fox` if $> 1,980\text{ MiB}$ (split if $> 3,950\text{ MiB}$).
3. **POST 3+: SEQUENTIAL SINGLE EPISODES (E01, E02, E03...)**
   - Muxed 1080p video + Synced Tamil AAC + ESub + Periodic subtitle track.
   - 16:9 widescreen landscape thumbnail at 20% timestamp.
   - Probed exact duration (no `00:00` display).
   - Auto-rename with release group fallback.
   - Dispatched round-robin across the 5 Helper Bots.

---

## 4. Multi-Source Encoders & Quality Waterfall

- **Multi-Source Target:** Up to 6 video releases per anime episode (min 1 to proceed):
  - **Up to 4 HEVC Sources:** YURASUKA > DB (animencodes) > Judas > DKB > sam > ASW > EMBER > Tenrai-Sensei
  - **Up to 2 AV1 Sources:** Breeze (LastBreeze) > Ironclad > Sokudo > Trix > neoDESU > Lumen
- **BluRay Priority:** If BluRay/BDRip exists, skip WebRip.
- **Batch-Only:** Only complete season batch torrents.
