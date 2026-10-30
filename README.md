# Starfall-Go Debrid (`starfall-debrid`)

High-performance, resource-safe private **Debrid Engine, Lossless Video Tools Studio, and Silent Telegram Cloud Vault** engineered in Go 1.23+ specifically for **2 vCPU, 4 GB RAM Linux VPS** deployments (Ubuntu 24.04 LTS / Debian 12).

## Hardware Protection & VPS Safety Architecture

| Protection Layer | Enforced Limit | Mechanism |
| :--- | :--- | :--- |
| **Container Cgroups Ceiling** | `3400 MiB` (`cpus: 1.9`) | `docker-compose.yml` & `deploy/starfall.service` (`MemoryMax=3400M`) guarantee at least **600 MiB** of physical RAM remains dedicated to the Linux kernel and `sshd` |
| **Go Runtime Memory Ceiling** | `3000 MiB` (`GOGC=50`) | `debug.SetMemoryLimit(3000 * 1024 * 1024)` + `ENV GOMEMLIMIT=3000MiB GOGC=50` |
| **1-Second Memory Watchdog** | `NORMAL` (<70%), `WARN` (70–82%), `CRITICAL` (>82%) | Polls `runtime.MemStats`, `/proc/meminfo`, and `/proc/stat`; throttles transfers, pauses downloads, and invokes `debug.FreeOSMemory()` |
| **Zero Memory Ballooning** | `512 KiB` `sync.Pool` | All HTTP/GDrive/Mega/Terabox downloads and 4-worker MTProto chunk uploads reuse fixed 512 KiB buffers (< 35 MB active upload heap) |
| **Zero Video Transcoding** | `0% CPU Re-encoding` | Video re-encoding (`libx264`/`libx265`) is forbidden; all track stripping, syncing, and >2GB splitting use lossless `mkvmerge` stream copy |
| **P2P Socket Limiter** | `150` Global / `30` per Task | Prevents hypervisor network abuse suspensions |
| **Concurrency Pool** | `4` Workers (`3 DL / 2 UL / 1 Remux`) | Bounded buffered channel semaphores in `internal/engine/dispatcher.go` |

## Complete Repository Structure

```text
starfall-go/
├── cmd/starfall/main.go                # Bootstrap, GOMEMLIMIT, embed.FS, HTTP/WS server
├── internal/
│   ├── config/config.go                # Viper & ENV loader with VPS safety clamping
│   ├── safety/
│   │   ├── watchdog.go                 # 1s RAM/CPU monitor, OOM circuit breaker & disk pacer
│   │   └── ratelimit.go                # 150 global / 30 per-task socket & bandwidth limiter
│   ├── models/
│   │   ├── task.go                     # Task lifecycle, EMA speed stats, pause/cancel gates
│   │   └── user.go                     # Google OAuth2 user schema, Telegram dump chat, themes
│   ├── engine/
│   │   ├── dispatcher.go               # 4-worker pool (3 DL, 2 UL, 1 lossless mkvmerge)
│   │   └── task_registry.go            # Lock-free sync.Map task registry
│   ├── ipc/bus.go                      # Non-blocking channel Pub/Sub event bus
│   ├── auth/
│   │   ├── google.go                   # Google OAuth 2.0 login & callback handler
│   │   ├── jwt.go                      # 30-day HTTP-Only Secure HS256 JWT manager
│   │   └── middleware.go               # Route protection middleware for /api/*
│   ├── user/
│   │   ├── store.go                    # Pure-Go WAL SQLite store (modernc.org/sqlite)
│   │   └── service.go                  # Profile, Telegram Dump linking, & theme endpoints
│   ├── downloader/
│   │   ├── manager.go                  # Unified Direct Leech Manager (Start/Pause/Resume/Abort)
│   │   ├── buffer/pool.go              # 512 KiB sync.Pool reusable chunk buffer
│   │   └── direct/
│   │       ├── resolver.go             # Universal URL classifier
│   │       ├── http_downloader.go      # 4-segment Range downloader + 500ms telemetry
│   │       ├── gdrive.go               # Google Drive virus-scan/quota bypass
│   │       ├── mega.go                 # Native Mega.nz AES-CTR decrypted chunk streamer
│   │       ├── terabox.go              # Terabox & MediaFire direct CDN extractor
│   │       └── ytdlp.go                # Streaming yt-dlp wrapper
│   ├── web/
│   │   ├── handlers_video.go           # FFprobe, JPEG frame grabber, & lossless mkvmerge API
│   │   └── handlers_admin.go           # Owner telemetry, RFC 6455 WebSocket logs, hot config
│   └── telegram/
│       ├── client.go                   # gotd/td MTProto session & 2GB/4GB Premium detector
│       ├── uploader.go                 # 4-worker 512 KiB parallel saveBigFilePart uploader
│       ├── splitter.go                 # Lossless mkvmerge --split size:1950M auto-splitter
│       ├── poster.go                   # Templates 1-11 caption & 16:9 poster card generator
│       └── notifier.go                 # Web Studio completion flash notifier
├── web/
│   ├── embed.go                        # //go:embed all:templates all:static
│   ├── static/css/anime_themes.css     # 5 Anime Themes (Solo Leveling, JJK, Demon Slayer, AoT, Cyberpunk)
│   └── templates/
│       ├── video_studio.html           # Lossless Audio/Sub Studio, Delay Sync, Frame Grabber
│       └── bot_settings.html           # Owner VPS Telemetry, WebSocket Log Terminal & Config
├── deploy/
│   ├── setup_vps.sh                    # 4GB NVMe swapfile, sysctl vm.swappiness=10, ulimit 65535
│   ├── starfall.service                # Bare-metal systemd unit with MemoryMax=3400M
│   └── nginx.conf                      # Reverse proxy with TLS 1.3 & WebSocket upgrade
├── scripts/verify.sh                   # End-to-end dependency, OAuth & mkvmerge split verifier
├── Dockerfile                          # Multi-stage Alpine 3.20 production build
└── docker-compose.yml                  # Cgroups v2 production compose stack
```

## 1-Click Production Deployment Guide

### Step 1: Harden the 2 vCPU / 4 GB VPS Kernel & Swap

Run the automated kernel hardening script as `root` on Ubuntu 24.04 LTS or Debian 12:

```bash
sudo bash deploy/setup_vps.sh
```

This creates a 4 GB NVMe `/swapfile` (`chmod 600`), configures `vm.swappiness = 10` and `fs.file-max = 100000` in `/etc/sysctl.d/99-starfall.conf`, and elevates file descriptors to `65535`.

### Step 2: Configure Environment Credentials

Create a `.env` file in the project root:

```ini
STARFALL_AUTH_GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
STARFALL_AUTH_GOOGLE_CLIENT_SECRET=your-google-client-secret
STARFALL_AUTH_GOOGLE_REDIRECT_URL=https://debrid.example.com/auth/google/callback
STARFALL_AUTH_JWT_SECRET=a_64_character_cryptographically_random_hex_signing_secret_key_01

STARFALL_TELEGRAM_API_ID=12345678
STARFALL_TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
STARFALL_TELEGRAM_BOT_TOKEN=123456789:AAH_your_telegram_bot_token
```

### Step 3: Launch with Docker Compose (Cgroups Shield Active)

```bash
docker compose up -d --build
```

### Step 4: Run the Verification Suite

```bash
docker exec -it starfall-debrid /app/scripts/verify.sh
```
