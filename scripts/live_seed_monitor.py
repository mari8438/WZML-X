#!/usr/bin/env python3
"""
Live Seeding & Swarm Monitor for Starfall & Multi-Tracker Packs
Refreshes in real-time inside your terminal. Press Ctrl+C to exit.
"""
import sys
import time
import os
import json
import urllib.request

QBT_URL = "http://172.18.0.3:8090/api/v2"

def clear_screen():
    sys.stdout.write("\033[H\033[J")

def format_speed(bytes_per_sec):
    mb = bytes_per_sec / (1024 * 1024)
    if mb >= 1.0:
        return f"{mb:6.2f} MB/s"
    return f"{bytes_per_sec / 1024:6.1f} KB/s"

def format_size(bytes_total):
    return f"{bytes_total / (1024**3):6.2f} GiB"

def get_tracker_label(tracker_url):
    tr = tracker_url.lower()
    if "dirtybytes" in tr:
        return "\033[1;33m[DirtyBytes]\033[0m"
    if "bitporn" in tr:
        return "\033[1;35m[ BitPorn  ]\033[0m"
    if "kufirc" in tr:
        return "\033[1;36m[  Kufirc  ]\033[0m"
    if "nyaa" in tr or "tokyotosho" in tr or "anidex" in tr:
        return "\033[1;32m[   Nyaa   ]\033[0m"
    return "\033[1;37m[  Public  ]\033[0m"

def render_bar(pct, width=20):
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:5.1f}%"

def main():
    print("Starting Live Seeding Monitor... (Press Ctrl+C to stop)")
    time.sleep(0.5)
    
    while True:
        try:
            req = urllib.request.Request(f"{QBT_URL}/torrents/info", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                torrents = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            clear_screen()
            print(f"\033[1;31m[Error fetching qBittorrent stats]: {e}\033[0m")
            time.sleep(2)
            continue

        total_up_speed = sum(t.get("upspeed", 0) for t in torrents)
        total_dl_speed = sum(t.get("dlspeed", 0) for t in torrents)
        total_uploaded = sum(t.get("uploaded", 0) for t in torrents)

        out = []
        out.append("\033[1;34m" + "="*86 + "\033[0m")
        out.append(f"\033[1;37m ⚡ STARFALL LIVE SEEDING & TRANSFER MONITOR \033[0m | \033[1;32m{time.strftime('%Y-%m-%d %H:%M:%S')}\033[0m")
        out.append("\033[1;34m" + "="*86 + "\033[0m")
        out.append(
            f" Total Upload:   \033[1;32m{format_speed(total_up_speed)}\033[0m (~{(total_up_speed*8)/(1024**2):.1f} Mbps) | "
            f"Total Download: \033[1;36m{format_speed(total_dl_speed)}\033[0m\n"
            f" Cumulative Up:  \033[1;33m{format_size(total_uploaded)}\033[0m | Active Torrents: \033[1;37m{len(torrents)}\033[0m"
        )
        out.append("\033[1;34m" + "-"*86 + "\033[0m")

        # Prioritize Hstream and active seeds
        sorted_torrents = sorted(
            torrents,
            key=lambda t: (1 if "Hstream" in t.get("name", "") else 0, t.get("upspeed", 0), t.get("uploaded", 0)),
            reverse=True
        )

        for t in sorted_torrents:
            name = t.get("name", "")
            if len(name) > 34:
                display_name = name[:31] + "..."
            else:
                display_name = name.ljust(34)

            state = t.get("state", "unknown")
            if state == "uploading":
                state_colored = "\033[1;32muploading\033[0m"
            elif state in ("stalledUP", "queuedUP"):
                state_colored = "\033[1;33mstalledUP\033[0m"
            elif state == "downloading":
                state_colored = "\033[1;36mdownload \033[0m"
            else:
                state_colored = f"\033[0;37m{state[:9]:<9}\033[0m"

            prog = t.get("progress", 0.0) * 100.0
            upspeed = format_speed(t.get("upspeed", 0))
            uploaded = format_size(t.get("uploaded", 0))
            ratio = t.get("ratio", 0.0)
            seeds = t.get("num_seeds", 0)
            leechs = t.get("num_leechs", 0)
            tracker_tag = get_tracker_label(t.get("tracker", ""))

            out.append(
                f"{tracker_tag} \033[1;37m{display_name}\033[0m | {state_colored} | "
                f"▲ \033[1;32m{upspeed}\033[0m | Up: \033[1;33m{uploaded}\033[0m (R:{ratio:.2f}) | Peers: {seeds}s/{leechs}l"
            )

        out.append("\033[1;34m" + "="*86 + "\033[0m")
        out.append("\033[0;37m [Tip: Press Ctrl+C in terminal to exit monitor] \033[0m")

        clear_screen()
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()
        time.sleep(1.5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\033[1;32mMonitor stopped.\033[0m")
