# ==============================================================================
# Starfall-Go Debrid — Multi-Stage Ultra-Light Production Image
# Target Hardware: 2 vCPU, 4 GB RAM VPS (Ubuntu 24.04 LTS / Debian 12)
# Hard Memory Ceiling: 3000 MiB Go Runtime / 3400 MiB Docker Cgroups Shield
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Builder (golang:1.23-alpine)
# ------------------------------------------------------------------------------
FROM golang:1.23-alpine AS builder

RUN apk add --no-cache git gcc musl-dev ca-certificates tzdata

WORKDIR /src

# Cache Go module dependencies first
COPY go.mod go.sum ./
RUN go mod download

# Copy complete source tree including embedded web templates and static assets
COPY . .

# Compile single static binary with debug symbols stripped (-s -w)
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -trimpath \
    -ldflags="-s -w" \
    -o /starfall-debrid \
    cmd/starfall/main.go

# ------------------------------------------------------------------------------
# Stage 2: Final Distro (alpine:3.20)
# ------------------------------------------------------------------------------
FROM alpine:3.20

LABEL org.opencontainers.image.title="starfall-debrid" \
      org.opencontainers.image.description="High-performance, resource-safe private Debrid & Telegram Cloud Vault engine for 2 vCPU / 4GB RAM VPS" \
      org.opencontainers.image.version="1.2.0"

# Install lightweight runtime media & stream utilities (0% transcoding policy)
RUN apk add --no-cache \
    ca-certificates \
    tzdata \
    tini \
    curl \
    ffmpeg \
    mkvtoolnix \
    aria2 \
    yt-dlp \
    && addgroup -g 10001 -S starfall \
    && adduser -u 10001 -S -G starfall -h /app starfall \
    && mkdir -p /app/data /app/downloads /app/downloads/thumbnails /app/scripts \
    && chown -R starfall:starfall /app

WORKDIR /app

# Copy compiled static binary and verification script
COPY --from=builder /starfall-debrid /starfall-debrid
COPY scripts/verify.sh /app/scripts/verify.sh
RUN chmod +x /starfall-debrid /app/scripts/verify.sh

# Enforce Go runtime soft memory limit (3000 MiB) and aggressive GC (50%)
ENV GOMEMLIMIT=3000MiB \
    GOGC=50 \
    GOMAXPROCS=2 \
    STARFALL_APP_LISTEN_ADDR="0.0.0.0:8080" \
    STARFALL_APP_DOWNLOAD_DIR="/app/downloads" \
    STARFALL_DATABASE_SQLITE_PATH="/app/data/starfall_users.db" \
    TZ=UTC

VOLUME ["/app/data", "/app/downloads"]

EXPOSE 8080

USER starfall

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/starfall-debrid"]
