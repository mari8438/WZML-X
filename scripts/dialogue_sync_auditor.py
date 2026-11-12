#!/usr/bin/env python3
"""
Subtitle-Anchored Dialogue Audio Sync Auditor
============================================
Finds the exact first spoken dialogue subtitle, extracts a 15-second speech window
around it, and computes cross-correlation between Tamil and Japanese audio.
"""

import json
import os
import re
import subprocess
from collections import defaultdict
import numpy as np
import scipy.signal

MUXED_DIR = "/home/administrator/starfall/downloads/muxed_output"

def get_first_dialogue_timestamp(fpath: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_name:stream_tags=title", "-of", "json", fpath]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
    except Exception:
        return 60.0

    sub_streams = [s for s in data.get("streams", []) if s.get("codec_name") in ("ass", "subrip")]
    if not sub_streams:
        return 60.0

    # Pick dialogue stream (prefer 'full' or 'dialogue' over 'signs')
    best_idx = sub_streams[0]["index"]
    for s in sub_streams:
        title = (s.get("tags", {}).get("title") or "").lower()
        if "dialogue" in title or "full" in title:
            best_idx = s["index"]
            break

    # Extract first 3 minutes of subtitles
    cmd_sub = ["ffmpeg", "-i", fpath, "-map", f"0:{best_idx}", "-t", "200", "-f", "srt", "-"]
    res_sub = subprocess.run(cmd_sub, capture_output=True, text=True)

    for line in res_sub.stdout.split("\n"):
        m = re.search(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->", line)
        if m:
            h, mn, sec, ms = map(int, m.groups())
            t = h * 3600 + mn * 60 + sec + ms / 1000.0
            if t > 2.0: # first spoken line
                return t
    return 60.0

def audit_file(fpath: str):
    fname = os.path.basename(fpath)
    t_start = get_first_dialogue_timestamp(fpath)
    ss = max(0, t_start - 2)

    # Audio tracks
    cmd_ff = ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type:stream_tags=language", "-of", "json", fpath]
    res_ff = subprocess.run(cmd_ff, capture_output=True, text=True)
    data = json.loads(res_ff.stdout)
    a_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if len(a_streams) < 2:
        return {"status": "NO_REF", "file": fname}

    tam_idx = a_streams[0]["index"]
    ref_idx = a_streams[1]["index"]
    for s in a_streams[1:]:
        if (s.get("tags", {}).get("language") or "").lower() in ("jpn", "jap"):
            ref_idx = s["index"]
            break

    t_raw = f"/tmp/aud_tam_{os.getpid()}.raw"
    r_raw = f"/tmp/aud_ref_{os.getpid()}.raw"

    cmd1 = ["ffmpeg", "-y", "-ss", str(ss), "-i", fpath, "-t", "15", "-map", f"0:{tam_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", t_raw]
    cmd2 = ["ffmpeg", "-y", "-ss", str(ss), "-i", fpath, "-t", "15", "-map", f"0:{ref_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", r_raw]
    subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    t1 = np.fromfile(t_raw, dtype=np.float32)
    t2 = np.fromfile(r_raw, dtype=np.float32)
    if os.path.exists(t_raw): os.remove(t_raw)
    if os.path.exists(r_raw): os.remove(r_raw)

    min_l = min(len(t1), len(t2))
    if min_l < 16000 * 3:
        return {"status": "TOO_SHORT", "file": fname}

    e1 = np.abs(scipy.signal.hilbert(t1[:min_l]))
    e2 = np.abs(scipy.signal.hilbert(t2[:min_l]))
    corr = scipy.signal.correlate(e1, e2, mode="full")
    lags = scipy.signal.correlation_lags(len(e1), len(e2), mode="full")
    best = lags[np.argmax(corr)]
    delay_ms = float((best / 16000.0) * 1000.0)
    conf = float(corr[np.argmax(corr)] / (np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-9))

    is_synced = abs(delay_ms) <= 45.0
    return {
        "file": fname,
        "dialogue_time_s": t_start,
        "lag_ms": delay_ms,
        "conf": conf,
        "synced": is_synced,
    }

def main():
    files = [f for f in os.listdir(MUXED_DIR) if f.endswith(".mkv")]
    anime_map = defaultdict(list)
    for f in files:
        m = re.search(r'\[S(\d+)E(\d+)\]\s+([^(]+)\((\d{4})\)', f)
        if m:
            s = int(m.group(1))
            ep = int(m.group(2))
            title = m.group(3).strip()
            anime_map[(title, s)].append((ep, os.path.join(MUXED_DIR, f)))

    print(f"Auditing Dialogue Sync across {len(anime_map)} Anime Seasons...\n")
    report = []
    for (title, s), ep_list in sorted(anime_map.items()):
        ep_list.sort(key=lambda x: x[0])
        # Pick ep 1
        ep_num, fpath = ep_list[0]
        res = audit_file(fpath)
        tag = "PERFECT" if res.get("synced") else "DELAY DETECTED"
        lag = res.get("lag_ms", 0)
        conf = res.get("conf", 0)
        t_diag = res.get("dialogue_time_s", 0)
        print(f"[{tag:14s}] {title} S{s:02d}E{ep_num:02d} (dialogue@{t_diag:.1f}s) -> Lag: {lag:+.1f} ms (conf: {conf:.2f})")
        report.append({"title": title, "season": s, "result": res})

    with open("/home/administrator/starfall/data/dialogue_sync_report.json", "w") as f:
        json.dump(report, f, indent=2)

if __name__ == "__main__":
    main()
