#!/usr/bin/env bash
set -e

GUARDIAN_LOG="/home/administrator/starfall/logs/hourly_guardian.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "==========================================================" >> "$GUARDIAN_LOG"
echo "[$TIMESTAMP] Starting Starfall Hourly AGY Health & Self-Healing Cycle" >> "$GUARDIAN_LOG"
echo "==========================================================" >> "$GUARDIAN_LOG"

PROMPT=$(cat << 'P_EOF'
You are the Autonomous Starfall Pipeline SRE & Guardian on VPS (85.190.123.86, user: administrator).

Your objective is to inspect the 48-hour continuous anime muxing pipeline, analyze logs for errors, automatically fix any bugs or bottlenecks found, ensure healthy operation, and output a concise status summary.

Working directory: /home/administrator/starfall
Target Service: starfall-pipeline.service
Pipeline Log: /home/administrator/starfall/logs/starfall_48h_pipeline.log

Execute the following autonomous health and self-healing cycle:

1. Telemetry & Process Health Check:
   - Check service status: `systemctl status starfall-pipeline.service --no-pager`
   - If inactive or failed, inspect `journalctl -u starfall-pipeline.service -n 50 --no-pager`, repair the error, and restart with `sudo systemctl restart starfall-pipeline.service`.

2. Log Analysis & Error Detection:
   - Read the last 80 lines of `/home/administrator/starfall/logs/starfall_48h_pipeline.log`.
   - Grep for `ERROR`, `CRITICAL`, `Traceback`, `failed`, or `quarantine`.

3. Automatic Bug-Fixing Protocol:
   - If aria2c fails with code 13, verify `--allow-overwrite=true` and `--auto-file-renaming=false` in `torrent_downloader.py`.
   - If tmpfs or disk is full, check `df -h / /dev/shm` and clean up any partial mux stubs older than 2 hours in `/dev/shm/starfall_mux/`.
   - If autorename differs from user format, ensure `format_episode_filename` and `format_season_merge_filename` strictly produce:
     `🄰🅂- [S01E01] Name (Year) 1080p 10bit BluRay HEVC [Tamil-AAC 2.0] ESub ~ Group ~ [@Anime_Starfall🥰].mkv`
   - If thumbnails differ, ensure the single authoritative 16:9 thumbnail is shared for the entire season.
   - If any python file is edited, run `python3 -m py_compile <file>` and restart `starfall-pipeline.service`.

4. Output a clean status summary:
   - Service state (Uptime, PID, Memory)
   - Currently processing Anime & Season
   - Episodes completed / uploaded
   - Disk & RAM space
   - Fixes applied (or "System healthy, running smoothly")
P_EOF
)

cd /home/administrator/starfall
/usr/local/bin/agy --dangerously-skip-permissions --print "$PROMPT" >> "$GUARDIAN_LOG" 2>&1

echo "[$TIMESTAMP] Finished Starfall Hourly AGY Cycle." >> "$GUARDIAN_LOG"
