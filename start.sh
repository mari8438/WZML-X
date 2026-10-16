#!/usr/bin/env bash
set -Eeuo pipefail

log() {
    printf '[start] %s\n' "$*"
}

repair_packages() {
    if [ "$(id -u)" -ne 0 ]; then
        log "Required binaries are missing and the container is not running as root; cannot auto-repair packages."
        return 1
    fi

    log "Attempting package repair with apt-get."
    apt-get update
    apt-get install -y --no-install-recommends \
        aria2 ffmpeg imagemagick mediainfo mktorrent mkvtoolnix qbittorrent-nox rclone sabnzbdplus
    rm -rf /var/lib/apt/lists/*
}

require_binary() {
    local name="$1"
    local package="${2:-$1}"
    if command -v "$name" >/dev/null 2>&1; then
        log "Found $name at $(command -v "$name")"
        return 0
    fi

    log "Missing required binary: $name (package: $package)"
    repair_packages
    if ! command -v "$name" >/dev/null 2>&1; then
        log "Package repair did not provide $name. Startup cannot continue."
        return 1
    fi
    log "Repaired $name at $(command -v "$name")"
}

main() {
    require_binary aria2c aria2
    require_binary qbittorrent-nox qbittorrent-nox
    require_binary ffmpeg ffmpeg
    require_binary mkvmerge mkvtoolnix
    require_binary rclone rclone
    require_binary mktorrent mktorrent
    require_binary mediainfo mediainfo
    require_binary montage imagemagick

    if [ "${DISABLE_NZB:-false}" != "true" ] && [ "${DISABLE_NZB:-False}" != "True" ]; then
        require_binary sabnzbdplus sabnzbdplus
    fi

    mkdir -p downloads download-temp torrents/seeding thumbnails rclone logs configs
    touch configs/aria2.session

    log "Starting aria2c daemon with NVMe & 2-vCPU optimized flags."
    aria2c \
        --enable-rpc=true \
        --rpc-listen-all=true \
        --rpc-allow-origin-all=true \
        --daemon=true \
        --file-allocation=falloc \
        --no-file-allocation-limit=64M \
        --split=16 \
        --max-connection-per-server=16 \
        --min-split-size=4M \
        --max-concurrent-downloads=8 \
        --disk-cache=64M \
        --bt-max-peers=128 \
        --save-session=configs/aria2.session \
        --input-file=configs/aria2.session || true

    log "Running repository update hook."
    python update.py

    log "Starting bot."
    exec python -m bot
}

main "$@"
