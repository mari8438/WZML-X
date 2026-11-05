# Automated Tamil Anime Audio-Muxing & Telegram Leech Pipeline
## Production Systems Architecture & Engineering Specification (Starfall v1.3 - Multi-Source & High-Tier Encoder Edition)

**Target Host:** Ubuntu 24.04 LTS (16 vCPU Cores, 32 GB RAM, 756 GB NVMe SSD)  
**Storage Allocation:** 500 GB dedicated for automated muxing pipeline (Hard Quota: < 450 GB active footprint)  
**Telegram Infrastructure:** 1 Premium User Session (`@Hsin_The_Moon_Fox`) + 5 Helper Bots (`@Anime_11_uplode_bot`, `@Anime_14_uplode_bot`, `@Anime_15_uplode_bot`, `@Starfall_xrobot`, `@Do_up_3_robot`) — *all added as Admins in channel*  
**Target Channel:** Tamil Anime Starfall Collection (`-1004335327733`) — 3,368 total media files, 333 merged files skipped, 50+ major episodic anime series  
**Execution Cadence:** Continuous batches of 20 anime series (starting with most popular titles)  
**Database:** SQLite 3 in WAL Mode (`data/starfall_state.db`)  
**RAM Workspace:** 16 GB `/dev/shm/starfall_mux`  

---

## 1. High-Level System Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│             STARFALLX AUTOMATED ANIME MUXING & TELEGRAM BOT            │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
       ┌────────────────────────────┼────────────────────────────┐
       ▼                            ▼                            ▼
[1. Telegram Harvester]     [2. Nyaa Batch Finder]      [3. Acoustic Sync Engine]
  • 5 Helper Bots + User      • Strictly BATCH Torrents   • 4-Anchor Stereo Side-Ch
  • Direct AAC demux in RAM   • AniList Romaji / Kanji    • GCC-PHAT Phase Whitening
  • Skip Single Merge Files     name translation          • Sub-2s PSR Confidence
  • Episodic Files Only       • Multi-Source (Max 6:        (Threshold >= 10.0)
       │                        4 HEVC + 2 AV1, Min 1)           │
       │                      • BluRay > WebRip Priority         │
       │                            │                            │
       └────────────────────────────┼────────────────────────────┘
                                    ▼
                       [4. MKVToolNix Auto-Muxer]
                         • Track 1: 1080p x265/AV1 Video (Copy)
                         • Track 2: Synced Tamil AAC (Copy, Synced)
                         • Track 3: English Subtitles (Preserved)
                         • Japanese Audio Dropped (Tamil-Only Output)
                         • Intro Subtitle (0-5s: [@Anime_Starfall🥰])
                         • Zero Video Re-encoding (5s / episode)
                                    │
                 ┌──────────────────┴──────────────────┐
                 ▼                                     ▼
      [5A. Single Episodes]                   [5B. Full Season Merge]
        • S01E01, S01E02...                     • `mkvmerge --append-mode file`
        • Robust Auto-Rename:                   • Duration normalization
          🄰🅂-[S{s}E{e}] {name}...               • Auto-Split if > 3,950 MiB
          (Batch name fallback for group)       • Matroska Chapter Markers
        • AniList/TMDB Season Posters                  │
        • Season-Wise Thumbnails                       │
                 │                                     │
                 └──────────────────┬──────────────────┘
                                    ▼
                         [6. Tiered Telegram Uploader]
                           • Single Eps (<=1,980 MiB) -> 5 Helper Bots
                           • Merged Season (>1,980 MiB) -> Premium User
                           • Immediate NVMe Quota Cleanup (<450 GB Limit)
```

---

## 2. Multi-Source Muxing Strategy & Encoder Rankings

### 2.1 Multi-Source Policy (1 Tamil Audio -> Up to 6 Video Sources)
To provide the highest quality and variety for channel members:
- For each episode/season, the pipeline searches for **up to 6 distinct video releases**:
  - **Up to 4 HEVC (x265 10-bit) sources**
  - **Up to 2 AV1 (10-bit) sources**
  - **Minimum requirement:** 1 valid source is sufficient to proceed.
- Each source is paired with the synchronized Tamil audio, producing separate releases labeled by their respective release group (e.g. `[Judas]`, `[YURASUKA]`, `[Breeze]`).

### 2.2 Top HEVC/x265 Encoder Hierarchy (from Research Report)
1. **YURASUKA** (Gold Standard: 1080p BD Dual-Audio HEVC, VapourSynth debanded, 450–800 MiB/ep)
2. **DB / animencodes** (Best compact BD 10-bit, exceptional line sharpness & dark scenes, 400–600 MiB/ep)
3. **Judas** (Most versatile & active 10-bit group, clean weeklies & BD batches, 300–600 MiB/ep)
4. **DKB** (Best weekly single-audio WEB-DL, no bit-starvation, 350–450 MiB/ep)
5. **sam / SeaDex** (Reference visual quality, uncompressed audio, 1.5–4.4 GiB/ep)
6. **ASW** (Clean weekly single-audio WEB-DL, 240–320 MiB/ep)
7. **EMBER** (Massive catalog, Dual-Audio BD & WEB batches, ~315 MiB/ep)
8. **Tenrai-Sensei** (Complete franchise BD packs, 350–500 MiB/ep)
9. **Anime Time** (Complete catalog batches, 250–400 MiB/ep)

### 2.3 Top AV1 Encoder Hierarchy (from Research Report)
1. **Breeze / LastBreeze** (Gold standard 1080p BD AV1 batches, `svt-av1-5fish`, ~300 MiB/ep)
2. **Ironclad** (Psychovisually target-tuned, CVVDP scene score 9.6, Butteraugli <1.3, 250–400 MiB/ep)
3. **Sokudo** (Ultra-efficient BD, transparent line clarity at ~150–250 MiB/ep)
4. **Trix / NekoTrix** (Best weekly 1080p WEBRip & batch AV1, 200–285 MiB/ep)
5. **neoDESU / neoHEVC** (10-bit AV1 + Opus, ~300–330 MiB/ep)
6. **Lumen** (SSIMULACRA2 >85 verified)
7. **Anime-AV1 / omnipotent500** (Clean BD AV1 + Opus franchise batches, 320–420 MiB/ep)
8. **SavI0r / SavI0r-Pro** (Dual-Audio WEB & BD)

### 2.4 Quality & Torrent Rules
- **BluRay Priority:** If a BluRay (BDRip / BD) batch release is available, **strictly skip WebRip/HDTV releases**.
- **Batch Torrents Only:** Only select complete season batch torrents (`[Batch]`, `01-12`, `01-24`).
- **qBittorrent File Priority Filter:** Inspect torrent file list before unpausing: set `priority=0` (skip download) for `NCOP`, `NCED`, `Menu`, `PV`, `CM`, `OVA`, and non-episode extras.

---

## 3. AniList Title Translation & Multi-Language Discovery

### 3.1 Japanese / Romaji Title Resolution
Nyaa.si encoders predominantly use Romaji or Japanese titles rather than English titles.
- When querying Nyaa for an anime (e.g. *Demon Slayer*, *Attack on Titan*, *My Hero Academia*):
  - Query AniList GraphQL API:
    ```graphql
    query ($search: String) {
      Media (search: $search, type: ANIME) {
        id
        title {
          romaji
          english
          native
        }
        synonyms
        coverImage {
          extraLarge
        }
      }
    }
    ```
  - Retrieve:
    - `title.romaji` (e.g. *Kimetsu no Yaiba*, *Shingeki no Kyojin*, *Boku no Hero Academia*, *Kakkou no Iinazuke*)
    - `title.english` (e.g. *Demon Slayer*, *Attack on Titan*, *My Hero Academia*, *A Couple of Cuckoos*)
    - `title.native` (Japanese Kanji)
    - `synonyms`
- Query Nyaa with both Romaji and English terms (e.g. `[Judas] Kimetsu no Yaiba S01 1080p Batch` OR `[Judas] Demon Slayer S01 1080p Batch`).

---

## 4. Season-Wise Posters & Thumbnails (AniList / TMDB)

### 4.1 Season-Specific Metadata & Cover Art
- Generic video screenshots fail for multi-season shows.
- The pipeline queries AniList / TMDB for the exact `season_number` to download the official, high-resolution Season Poster (`coverImage.extraLarge` / TMDB season poster).
- Stored locally at: `thumbnails/poster_{clean_show}_S{season}.jpg`.
- Applied directly as the Telegram media thumbnail and incorporated into Poster Style 11 descriptions.

---

## 5. Robust Auto-Rename & Release Group Fallback

### 5.1 Release Group Detection Fallback Chain
When renaming muxed episodes, if the individual episode file does not contain the release group name:
1. **Torrent Folder / Batch Title:** Extract group from the torrent name (e.g. `[YURASUKA] ...` -> `YURASUKA`, `[Judas] ...` -> `Judas`, `[Breeze] ...` -> `Breeze`).
2. **File Name Regex:** Match `^\[(.*?)\]` or `-(.*?)(?:\.mkv|$)`.
3. **MKV Metadata:** Read container title and track names via `mkvmerge -J`.
4. **Database Record:** Fall back to the encoder tracked in `anime_seasons.nyaa_release_group`.

### 5.2 Auto-Rename Pattern
```
🄰🅂-[S{season}E{episode}] {name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~ {release_group} ~ [@Anime_Starfall🥰]
```
Example:
`🄰🅂-[S01E01] Jujutsu Kaisen 2020 1080p 10bit CR BDRip x265 [Tamil-AAC 2.0] ESub ~ YURASUKA ~ [@Anime_Starfall🥰].mkv`

### 5.3 Intro Subtitle Injection
Inject a 5-second non-intrusive credit banner at timestamp `00:00:00.000` to `00:00:05.000`:
```
[@Anime_Starfall🥰]
```

---

## 6. Telegram Audio Harvester & Filtering

### 6.1 Single Merge File Exclusion Rule
The source Telegram channel (`-1004335327733`) contains both episodic releases and single merged season files (1-3 hour complete season videos).
- **Duration Filter:** Any video file with `duration > 2,400s` (40 minutes) is **strictly skipped**.
- **Pattern Filter:** Any file or caption containing `merge`, `complete season`, `all in one`, `batch`, `s01-s02`, `e01-e` is bypassed.
- **Episodic Only:** Only harvest standard individual episode files ($SxxExx$, $EP\ xx$, $Title\ -\ xx$).

---

## 7. Acoustic Delay Optimization (Sub-2s 4-Anchor GCC-PHAT)

1. **Mid/Side Spatial Vocal Stripping:**
   $$\text{Side}(t) = 0.5 \times \text{Left}(t) - 0.5 \times \text{Right}(t)$$
   Center-panned dialogue cancels out, isolating shared stereo BGM and directional SFX.
2. **Dual-Mono Fallback:**
   If side channel energy is below threshold ($\sigma_{\text{side}} < 10^{-4}$), switch to center sum + 4th-order Butterworth bandpass ($60\text{--}320\text{ Hz}$ & $3,100\text{--}3,900\text{ Hz}$), rejecting vocal formants ($320\text{--}3,100\text{ Hz}$).
3. **8,000 Hz `f32le` RAM Pipe:**
   Stream 8 kHz float32 PCM directly into memory via `ffmpeg ... -f f32le pipe:1` (zero disk I/O).
4. **4-Anchor Evaluation (20%, 40%, 60%, 80%):**
   Extract four 75s windows. Calculate GCC-PHAT with 3-point parabolic peak interpolation:
   - **`PSR >= 10.0` & all 4 anchors within $\pm 4\text{ ms}$:** `CONSTANT` offset applied via `mkvmerge --sync 0:<delay_ms>`.
   - **Step jump between 40% and 60%:** `EYECATCH_STEP` (commercial trim; insert audio padding).
   - **Linear drift (~42.7 ms/s):** `PAL_DRIFT` (25fps TV vs 23.976fps BD; pitch-preserving resample).
   - **`PSR < 6.0`:** Quarantine and test adjacent BD episodes ($\pm 1$).

---

## 8. Tiered Telegram Upload & Helper Bot Routing

- **Files $\le 1,980\text{ MiB}$ (Single Episodes):** Dispatched round-robin across the **5 Helper Bots** (`@Anime_11_uplode_bot`, `@Anime_14_uplode_bot`, `@Anime_15_uplode_bot`, `@Starfall_xrobot`, `@Do_up_3_robot`) with a 3.5s cooldown and $\le 18\text{ msgs/min}$ rate limit. (All bots are already Admins in the channel).
- **Files $> 1,980\text{ MiB}$ (Merged Season Files):** Uploaded strictly using the Premium User Session (`@Hsin_The_Moon_Fox`).
- **Disk Governance:** 500 GB total allocated. `DiskQuotaGuard` ensures active storage never exceeds 450 GB. Raw torrents are purged immediately after single uploads and season merge completion.
