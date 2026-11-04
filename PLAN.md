# Automated Tamil Anime Audio-Muxing & Telegram Leech Pipeline
## Production Systems Architecture & Engineering Specification (Starfall v1.2)

**Target Host:** Ubuntu 24.04 LTS (Kernel 7.0, 16 vCPU Cores, 32 GB RAM, 756 GB NVMe SSD)  
**Telegram Infrastructure:** 1 Premium User Session (`@Hsin_The_Moon_Fox`) + 5 Helper Bots (`@Anime_11_uplode_bot`, `@Anime_14_uplode_bot`, `@Anime_15_uplode_bot`, `@Starfall_xrobot`, `@Do_up_3_robot`)  
**Target Scale:** ~3,000 Episodes (~125 Anime Seasons)  
**Database:** SQLite 3 in WAL Mode (`starfall_state.db`)  
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
[1. Telegram Harvester]     [2. Nyaa BD Finder]        [3. Acoustic Sync Engine]
  • 5 Helper Bots + User      • Quality Filters:         • 4-Anchor Stereo Side-Ch
  • Fast direct AAC demux       1080p x265 / AV1         • GCC-PHAT Phase Whitening
  • Auto-organize by Show     • Judas / ASW / Ember      • Sub-2s PSR Confidence
       │                            │                            │
       └────────────────────────────┼────────────────────────────┘
                                    ▼
                       [4. MKVToolNix Auto-Muxer]
                         • Track 1: 1080p x265 Video (Copy)
                         • Track 2: Synced Tamil AAC (Copy, Synced)
                         • Track 3: English Subtitles (Preserved)
                         • Japanese Audio Dropped (Tamil-Only Output)
                         • Zero Video Re-encoding (5s / episode)
                                    │
                 ┌──────────────────┴──────────────────┐
                 ▼                                     ▼
      [5A. Single Episodes]                   [5B. Full Season Merge]
        • S01E01, S01E02...                     • `mkvmerge --append-mode`
        • WZML-X Auto-Rename                    • Auto-Pad to exact video frame
        • HD Thumbnail (20% mark)               • Auto-Split if > 3,950 MiB
                 │                                     │
                 └──────────────────┬──────────────────┘
                                    ▼
                         [6. Tiered Telegram Uploader]
                           • Single Eps (<=1,980 MiB) -> 5 Helper Bots
                           • Merged Season (>1,980 MiB) -> Premium User
                           • Immediate Disk Quota Cleanup (<75 GB Peak)
```

---

## 2. Deep Acoustic Delay Engine (Sub-2s 4-Anchor GCC-PHAT)

### 2.1 Physics & Pre-Processing
Cross-language audio synchronization cannot correlate human voice formants (Japanese vs. Tamil dialogue are acoustically uncorrelated). The engine isolates the shared Background Music (BGM) and Foley/SFX:

1. **Mid/Side Spatial Vocal Stripping:**
   $$\text{Side}(t) = 0.5 \times \text{Left}(t) - 0.5 \times \text{Right}(t)$$
   Center-panned dialogue ($\text{Left} \approx \text{Right}$) cancels out mathematically at the FFmpeg C-level, isolating stereo music and directional SFX.
2. **Dual-Mono Fallback:**
   If the Tamil TV track is mono ($\sigma_{\text{side}} < 10^{-4}$), fallback to Center Sum ($\text{Left} + \text{Right}$) + a 4th-order digital Butterworth filter:
   - Bandpass 1 (Bass Impact): $60\text{ Hz} - 320\text{ Hz}$
   - Bandpass 2 (SFX Transients): $3,100\text{ Hz} - 3,900\text{ Hz}$
   - Rejects the vocal formant band ($320\text{ Hz} - 3,100\text{ Hz}$).
3. **8,000 Hz `f32le` RAM Pipe:**
   Audio is downsampled to 8,000 Hz mono 32-bit float PCM directly into RAM via `pipe:1`. At $f_s = 8,000\text{ Hz}$, one sample equals $0.125\text{ ms}$ (333× finer than a $41.7\text{ ms}$ video frame). Four 75-second windows require only $2.4\text{ MB}$ RAM, avoiding NVMe I/O.

### 2.2 Mathematical Formulation (GCC-PHAT)
Cross-power spectrum whitening normalizes frequency bins by amplitude:
$$\Psi_{\text{PHAT}}(f) = \frac{X_{\text{JP}}(f) \cdot X_{\text{TA}}^*(f)}{|X_{\text{JP}}(f) \cdot X_{\text{TA}}^*(f)| + \epsilon}$$
$$R_{\text{PHAT}}(\tau) = \mathcal{F}^{-1} \{\Psi_{\text{PHAT}}(f)\}$$

Sub-sample parabolic peak interpolation:
$$\delta_{\text{sub}} = 0.5 \times \frac{R(k_p - 1) - R(k_p + 1)}{R(k_p - 1) - 2R(k_p) + R(k_p + 1)}$$
$$\Delta t_{\text{exact}} = \frac{k_p + \delta_{\text{sub}}}{f_s} \times 1000.0 \text{ (ms)}$$

### 2.3 Statistical Confidence Gate (PSR)
$$\text{PSR} = \frac{R(k_p) - \mu_{\text{noise}}}{\sigma_{\text{noise}}}$$
A threshold of $\text{PSR} \ge 10.0$ guarantees false-positive probability below $10^{-15}$.

### 2.4 Structural Classification Matrix

| Mode | Anchor Pattern (20%, 40%, 60%, 80%) | Root Cause | Automated Resolution |
| :--- | :--- | :--- | :--- |
| **`CONSTANT`** | All $\Delta t$ within $\pm 4\text{ ms}$, $\text{PSR} \ge 10.0$ | Identical master edit | Median offset via `mkvmerge --sync 0:<delay_ms>`. |
| **`EYECATCH_STEP`** | Step jump between 40% and 60% ($>40\text{ ms}$) | Mid-episode commercial bumper trim | Detect silence frame at mid-break; insert/trim silence in Tamil track. |
| **`PAL_DRIFT`** | Linear drift $\approx -40.96\text{ ms/s}$ ($R^2 \ge 0.998$) | 25.000 fps PAL TV vs 23.976 fps BD | Apply `atempo=0.95904096` pitch-preserving resample, re-verify, and mux. |
| **`CENSOR_CUTS`** | Discontinuous jumps across multiple anchors | Regional broadcast cuts (1–15s) | Dense 25s sliding scan; splice Tamil and bridge cuts with Japanese audio $-3\text{ dB}$. |
| **`EPISODE_SHIFT`** | All anchors $\text{PSR} < 6.0$ | Recap episode (Ep 07.5) offset | Probe BD episodes $k-1$ and $k+1$. If matched ($\text{PSR} \ge 12.0$), remap index. |

---

## 3. Critical Production Edge-Case Protections

### 3.1 Zero-Drift Full-Season Concatenation
When appending 24 episodes via `mkvmerge --append-mode file`, audio packet misalignments accumulate into $2.5\text{s} - 5.0\text{s}$ of lip-sync drift.
- **Normalization Formula:**
  1. Zero container delay: Pad initial positive delay with AAC silence (`adelay`) or trim lead-in (`atrim`), ensuring header delay is strictly `0 ms`.
  2. Match video duration: Query BD video duration $D_{\text{video}}$ via `ffprobe` and apply `apad=whole_dur=Dvideo` followed by `-t Dvideo`. Every audio track terminates within $\pm 10.6\text{ ms}$ (half an AAC packet) of the video track.

### 3.2 Telegram 4,000 MiB Limit Auto-Split
- Before merging a full season, calculate cumulative file size:
  $$\text{Size}_{\text{total}} = \sum \text{Size}(\text{Ep}_i) + \text{Overhead}$$
- If $\text{Size}_{\text{total}} > 3,950\text{ MiB}$, automatically partition at natural episode boundaries:
  - Part 1: Ep 01 – Ep 12 (e.g. $2,800\text{ MiB}$)
  - Part 2: Ep 13 – Ep 24 (e.g. $2,850\text{ MiB}$)
- Each part includes Matroska XML Chapter markers for every episode.

### 3.3 Tiered Telegram Routing & FloodWait Protection

| Destination File | Target Size | Routing Client | Rate Limits & Safeguards |
| :--- | :--- | :--- | :--- |
| **Single Episodes & Audio** | $\le 1,980\text{ MiB}$ | **5 Helper Bots** (Round-Robin) | 3.5s per-bot delay; max 18 msgs/min channel cap. |
| **Merged Season Videos** | $1,980\text{ MiB} - 3,950\text{ MiB}$ | **User Session** (`@Hsin_The_Moon_Fox`) | Dedicated to large files; prevents user account ban. |

### 3.4 Pre-Download qBittorrent Filtering
Inspect `/api/v2/torrents/files` on added Nyaa torrents and automatically set `priority=0` (do not download) on:
- `*NCOP*`, `*NCED*`, `*Menu*`, `*PV*`, `*CM*`, `*SP*`, `*Trailer*`, `*Scans*`.

---

## 4. VPS Resource Budget & Strict Disk Governance

### 4.1 Memory & Filesystem Layout
- **RAM Disk (`/dev/shm/starfall_mux`):** 12 GB tmpfs allocation. Single episode muxing and audio conversions occur entirely in RAM, preserving NVMe life and minimizing latency.
- **Byte-Reservation Disk Semaphore (`DiskQuotaGuard`):**
  $$\text{Used\_NVMe} + \text{InFlight\_Torrents} + (2.1 \times \text{New\_Torrent\_Size}) \le 75\text{ GB}$$
  - The permanent Tamil audio library occupies $\approx 65\text{ GB}$.
  - Single muxed files are deleted immediately after upload confirmation.
  - Raw BD torrent files are deleted immediately after the season merge upload completes.
  - Active NVMe usage is held strictly under $75\text{ GB}$ (leaving $>675\text{ GB}$ safety buffer).

### 4.2 Concurrency Sizing (16 vCPU / 32 GB RAM)
- **Audio Harvester:** 5 concurrent async download workers.
- **qBittorrent:** Max 2 active downloading torrents (`2048 MiB` cache).
- **Acoustic Sync Pool:** `ProcessPoolExecutor(max_workers=6)` with `OMP_NUM_THREADS=2`.
- **MKVToolNix Mux Pool:** `asyncio.Semaphore(4)` writing to `/dev/shm`.
- **Telegram Upload Pool:** 6 concurrent upload streams.

---

## 5. Database Schema (`starfall_state.db` - SQLite WAL Mode)

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- 1. Anime Seasons
CREATE TABLE IF NOT EXISTS anime_seasons (
    season_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_romaji TEXT NOT NULL,
    season_number INTEGER NOT NULL,
    total_episodes INTEGER NOT NULL,
    nyaa_info_hash TEXT,
    nyaa_release_group TEXT,
    reserved_bytes INTEGER DEFAULT 0,
    season_state TEXT DEFAULT 'PENDING' CHECK(season_state IN ('PENDING', 'HARVESTING', 'DOWNLOADING', 'PROCESSING', 'MERGING', 'COMPLETED', 'FAILED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Tamil Audio Library
CREATE TABLE IF NOT EXISTS tamil_audio_library (
    audio_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER REFERENCES anime_seasons(season_id),
    episode_number INTEGER NOT NULL,
    source_msg_id INTEGER NOT NULL,
    source_file_unique TEXT UNIQUE,
    local_m4a_path TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    audio_sha256 TEXT,
    file_size_bytes INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. Episode Jobs
CREATE TABLE IF NOT EXISTS episode_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER REFERENCES anime_seasons(season_id),
    episode_number INTEGER NOT NULL,
    audio_id INTEGER REFERENCES tamil_audio_library(audio_id),
    bd_relative_path TEXT,
    sync_mode TEXT CHECK(sync_mode IN ('CONSTANT', 'EYECATCH_STEP', 'PAL_DRIFT', 'CENSOR_CUTS', 'EPISODE_SHIFT', 'FAILED')),
    calculated_delay_ms INTEGER,
    speed_ratio REAL DEFAULT 1.0,
    min_psr_score REAL,
    anchor_telemetry JSON,
    job_state TEXT DEFAULT 'QUEUED' CHECK(job_state IN ('QUEUED', 'SYNCING', 'MUXING', 'UPLOADING', 'COMPLETED', 'QUARANTINE')),
    tg_single_msg_id INTEGER,
    uploaded_by_bot TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Season Merge Outputs
CREATE TABLE IF NOT EXISTS season_merge_outputs (
    merge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER REFERENCES anime_seasons(season_id),
    part_index INTEGER DEFAULT 1,
    episode_range TEXT NOT NULL,
    file_size_bytes INTEGER,
    tg_season_msg_id INTEGER,
    upload_state TEXT DEFAULT 'PENDING' CHECK(upload_state IN ('PENDING', 'MERGING', 'UPLOADING', 'COMPLETED', 'FAILED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. Telegram Client Pool Telemetry
CREATE TABLE IF NOT EXISTS tg_client_pool (
    client_username TEXT PRIMARY KEY,
    client_role TEXT CHECK(client_role IN ('HELPER_BOT', 'USER_PREMIUM')),
    cooldown_until_ts REAL DEFAULT 0,
    uploads_last_min INTEGER DEFAULT 0,
    total_bytes_sent INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_audio_ep ON tamil_audio_library(season_id, episode_number);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON episode_jobs(season_state, episode_number);
```

---

## 6. Directory Structure & Modular Layout

```
/home/administrator/starfall/
├── bot/
│   └── modules/
│       └── starfall_pipeline/
│           ├── __init__.py
│           ├── config.py              # Tunable thresholds, paths, timeouts
│           ├── db.py                  # SQLite WAL connection & queries
│           ├── disk_guard.py          # Strict <75 GB NVMe quota semaphore
│           ├── harvester.py           # Telegram channel audio demuxer
│           ├── nyaa_matcher.py        # RSS/Scraper for Judas/ASW/Ember
│           ├── qbit_controller.py     # qBittorrent RPC & file priority filter
│           ├── acoustic_sync.py       # 4-Anchor GCC-PHAT & PSR analyzer
│           ├── mux_engine.py          # Lossless mkvmerge single & merge engine
│           ├── thumb_namer.py         # 20% screenshot generator & auto-renamer
│           └── upload_router.py       # Tiered 5-bot / Premium session uploader
├── web/
│   └── starfall_ui/
│       ├── api_router.py          # FastAPI endpoints mounted to port 8080
│       ├── static/
│       │   ├── js/studio.js       # Waveform visualizer & live queue WebSockets
│       │   └── css/studio.css     # Clean dark dashboard theme
│       └── templates/
│           └── studio.html        # Multi-page responsive console
├── data/
│   ├── tam_audios/                # Permanent Tamil audio archive (~65 GB)
│   └── starfall_state.db          # Production database
└── /dev/shm/starfall_mux/         # RAM disk workspace (12 GB tmpfs)
```

---

## 7. Autonomous Execution Plan for AGY on VPS

When running inside `agy-bg` on the VPS, the workflow is executed across 4 phases:

### Phase 1: Database, Quota Guard & Channel Harvester
- Set up SQLite WAL schema and initialize `/dev/shm/starfall_mux`.
- Build `harvester.py` to index channel `-1004335327733` and extract AAC audio tracks into `data/tam_audios/`.

### Phase 2: Acoustic Sync Engine & Muxer
- Implement `acoustic_sync.py` with 4-anchor GCC-PHAT, parabolic interpolation, and classification heuristics.
- Implement `mux_engine.py` with zero-drift duration normalization and auto-split season merge.

### Phase 3: Nyaa Matcher, qBittorrent Filter & Upload Router
- Implement `nyaa_matcher.py` with automated quality filtering.
- Implement `qbit_controller.py` with selective file download.
- Implement `upload_router.py` with tiered routing across helper bots and user session.

### Phase 4: Web Dashboard Integration
- Mount `web/starfall_ui/api_router.py` into `web/wserver.py`.
- Connect WebSocket telemetry and Chart.js waveform alignment visualizer.
