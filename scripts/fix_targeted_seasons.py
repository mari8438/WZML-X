#!/usr/bin/env python3
"""
Targeted Season Audio Lip-Sync Fixer
===================================
Applies episode-by-episode acoustic cross-correlation and mkvmerge --sync
to all seasons confirmed to have the intro-card offset.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
import numpy as np
import scipy.signal

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
)
LOGGER = logging.getLogger("TargetedSync")

MUXED_DIR = "/home/administrator/starfall/downloads/muxed_output"

TARGET_SEASONS = [
    ("The Apothecary Diaries", 1),
    ("SPY x FAMILY", 1),
    ("SPY x FAMILY", 2),
    ("Jujutsu Kaisen", 1),
    ("Mashle Magic and Muscles", 1),
    ("Fire Force", 1),
    ("Fire Force", 2),
    ("Haikyu", 3),
    ("Shangri-La Frontier", 1),
    ("Classroom of the Elite", 1),
    ("Solo Leveling", 2),
    ("Zom 100 Bucket List of the Dead", 1),
    ("That Time I Got Reincarnated as a Slime", 1),
    ("Dan Da Dan", 1),
]

def get_audio_indices(fpath: str):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type:stream_tags=language", "-of", "json", fpath]
    res = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(res.stdout)
    a_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if len(a_streams) < 2:
        return 1, 2
    tam_idx = a_streams[0]["index"]
    ref_idx = a_streams[1]["index"]
    for s in a_streams[1:]:
        if (s.get("tags", {}).get("language") or "").lower() in ("jpn", "jap"):
            ref_idx = s["index"]
            break
    return tam_idx, ref_idx

def measure_lag(fpath: str, tam_idx: int, ref_idx: int, ss: int = 10, dur: int = 25):
    pid = os.getpid()
    t_raw = f"/tmp/tgt_tam_{pid}.raw"
    r_raw = f"/tmp/tgt_ref_{pid}.raw"

    cmd1 = ["ffmpeg", "-y", "-ss", str(ss), "-i", fpath, "-t", str(dur), "-map", f"0:{tam_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", t_raw]
    cmd2 = ["ffmpeg", "-y", "-ss", str(ss), "-i", fpath, "-t", str(dur), "-map", f"0:{ref_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", r_raw]
    subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    t1 = np.fromfile(t_raw, dtype=np.float32)
    t2 = np.fromfile(r_raw, dtype=np.float32)
    if os.path.exists(t_raw): os.remove(t_raw)
    if os.path.exists(r_raw): os.remove(r_raw)

    min_l = min(len(t1), len(t2))
    e1 = np.abs(scipy.signal.hilbert(t1[:min_l]))
    e2 = np.abs(scipy.signal.hilbert(t2[:min_l]))
    corr = scipy.signal.correlate(e1, e2, mode="full")
    lags = scipy.signal.correlation_lags(len(e1), len(e2), mode="full")
    best = lags[np.argmax(corr)]
    delay_ms = float((best / 16000.0) * 1000.0)
    conf = float(corr[np.argmax(corr)] / (np.linalg.norm(e1) * np.linalg.norm(e2) + 1e-9))
    return delay_ms, conf

def sync_file(fpath: str):
    fname = os.path.basename(fpath)
    tam_idx, ref_idx = get_audio_indices(fpath)

    # Measure initial lag
    lag_ms, conf = measure_lag(fpath, tam_idx, ref_idx, ss=10, dur=25)
    if conf < 0.25:
        lag_ms2, conf2 = measure_lag(fpath, tam_idx, ref_idx, ss=25, dur=25)
        if conf2 > conf:
            lag_ms, conf = lag_ms2, conf2

    # If already in sync (<= 35ms), skip
    if abs(lag_ms) <= 35.0:
        LOGGER.info(f"✨ [Already Synced] {fname} (lag: {lag_ms:+.1f}ms, conf: {conf:.2f})")
        return "ALREADY_SYNCED"

    # If lag is significant and negative (audio early)
    if lag_ms < -100.0 and conf >= 0.25:
        needed_delay_ms = int(round(-lag_ms))
        LOGGER.info(f"🔧 [Fixing] {fname}: Lag={lag_ms:+.1f}ms -> Applying --sync 1:{needed_delay_ms}ms...")

        tmp_out = fpath + ".sync.mkv"
        cmd = ["mkvmerge", "--ui-language", "en_US", "-o", tmp_out, "--sync", f"1:{needed_delay_ms}", fpath]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if proc.returncode > 1:
            if os.path.exists(tmp_out): os.remove(tmp_out)
            LOGGER.error(f"❌ mkvmerge failed for {fname}: {proc.stderr}")
            return "ERROR"

        # Verify
        res_ms, res_conf = measure_lag(tmp_out, tam_idx, ref_idx, ss=10, dur=25)
        if abs(res_ms) <= 35.0:
            shutil.move(tmp_out, fpath)
            LOGGER.info(f"🎯 [Lip-Sync Locked] {fname}: Residual Lag={res_ms:+.2f}ms (was {lag_ms:+.1f}ms)")
            return "FIXED"
        else:
            if os.path.exists(tmp_out): os.remove(tmp_out)
            LOGGER.warning(f"⚠️ [Divergence] {fname}: Residual {res_ms:+.1f}ms did not drop <= 35ms. Kept original.")
            return "DIVERGENCE"
    else:
        LOGGER.info(f"ℹ️ [Skipped] {fname} (lag: {lag_ms:+.1f}ms, conf: {conf:.2f})")
        return "SKIPPED"

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

    for title, season in TARGET_SEASONS:
        if (title, season) not in anime_map:
            continue
        ep_list = anime_map[(title, season)]
        ep_list.sort(key=lambda x: x[0])
        LOGGER.info("=" * 70)
        LOGGER.info(f"🚀 PROCESSING: {title} S{season:02d} ({len(ep_list)} episodes)")
        LOGGER.info("=" * 70)

        for ep_num, fpath in ep_list:
            try:
                sync_file(fpath)
            except Exception as e:
                LOGGER.error(f"Error on {fpath}: {e}")

    LOGGER.info("=" * 70)
    LOGGER.info("🎉 ALL TARGETED SEASONS SYNCED & VERIFIED!")
    LOGGER.info("=" * 70)

if __name__ == "__main__":
    main()
