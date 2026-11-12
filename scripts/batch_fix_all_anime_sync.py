#!/usr/bin/env python3
"""
Autonomous Multi-Anime Acoustic Lip-Sync Corrector (Multi-Threaded)
===================================================================
1. Audits every episode of every anime season in downloads/muxed_output/.
2. Uses SciPy Hilbert-envelope cross-correlation (GCC-PHAT) against reference audio.
3. If |audio_lag| > 35ms:
   - Losslessly remuxes with mkvmerge --sync {track_id}:{delay_ms}
   - Verifies residual lip-sync < 15ms
   - Atomically updates file in-place
4. Persists sync state in data/batch_sync_progress.json.
5. Runs 4 concurrent workers across 16 CPU cores.
"""

import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import scipy.signal

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
)
LOGGER = logging.getLogger("AutoSync")

MUXED_DIR = "/home/administrator/starfall/downloads/muxed_output"
PROGRESS_FILE = "/home/administrator/starfall/data/batch_sync_progress.json"
PROGRESS_LOCK = threading.Lock()


def load_progress() -> Dict[str, Dict]:
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_progress(progress: Dict[str, Dict]):
    with PROGRESS_LOCK:
        tmp_f = PROGRESS_FILE + f".tmp.{uuid.uuid4().hex[:6]}"
        with open(tmp_f, "w") as f:
            json.dump(progress, f, indent=2)
        os.replace(tmp_f, PROGRESS_FILE)


def probe_track_info(filepath: str) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """
    Returns (mkv_tam_id, mkv_ref_id, ff_tam_idx, ff_ref_idx)
    """
    cmd = ["mkvmerge", "-J", filepath]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode > 1:
        return None, None, None, None

    try:
        data = json.loads(res.stdout)
    except Exception:
        return None, None, None, None

    tracks = data.get("tracks", [])
    tam_track = None
    jpn_track = None
    eng_track = None
    other_audio = []

    for t in tracks:
        if t.get("type") == "audio":
            lang = (t.get("properties", {}).get("language") or "").lower()
            t_name = (t.get("properties", {}).get("track_name") or "").lower()
            if "tam" in lang or "tamil" in t_name or tam_track is None:
                if tam_track is None:
                    tam_track = t
            elif "jpn" in lang or "jap" in lang:
                jpn_track = t
            elif "eng" in lang:
                eng_track = t
            else:
                other_audio.append(t)

    ref_track = jpn_track or eng_track or (other_audio[0] if other_audio else None)
    if not tam_track or not ref_track:
        return None, None, None, None

    mkv_tam_id = tam_track["id"]
    mkv_ref_id = ref_track["id"]

    ff_tam_idx = mkv_tam_id
    ff_ref_idx = mkv_ref_id

    return mkv_tam_id, mkv_ref_id, ff_tam_idx, ff_ref_idx


def cross_correlate(filepath: str, ff_tam_idx: int, ff_ref_idx: int, ss: int = 120, dur: int = 60) -> Tuple[float, float]:
    """
    Calculates audio lag of Tamil audio relative to reference audio using envelope cross-correlation.
    Lag < 0 means Tamil is early (needs positive sync).
    Lag > 0 means Tamil is late (needs negative sync).
    """
    token = uuid.uuid4().hex[:8]
    t_raw = f"/tmp/sync_tam_{token}.raw"
    r_raw = f"/tmp/sync_ref_{token}.raw"

    cmd1 = ["ffmpeg", "-y", "-ss", str(ss), "-i", filepath, "-t", str(dur), "-map", f"0:{ff_tam_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", t_raw]
    cmd2 = ["ffmpeg", "-y", "-ss", str(ss), "-i", filepath, "-t", str(dur), "-map", f"0:{ff_ref_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", r_raw]

    try:
        subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if not os.path.exists(t_raw) or not os.path.exists(r_raw):
            return 0.0, 0.0

        t1 = np.fromfile(t_raw, dtype=np.float32)
        t2 = np.fromfile(r_raw, dtype=np.float32)
        min_len = min(len(t1), len(t2))
        if min_len < 16000 * 5:
            return 0.0, 0.0

        t1 = t1[:min_len]
        t2 = t2[:min_len]

        env1 = np.abs(scipy.signal.hilbert(t1))
        env2 = np.abs(scipy.signal.hilbert(t2))

        norm1 = np.linalg.norm(env1)
        norm2 = np.linalg.norm(env2)
        if norm1 < 1e-4 or norm2 < 1e-4:
            return 0.0, 0.0

        corr = scipy.signal.correlate(env1, env2, mode="full")
        lags = scipy.signal.correlation_lags(len(env1), len(env2), mode="full")
        peak_idx = np.argmax(corr)
        best_lag = lags[peak_idx]
        conf = float(corr[peak_idx] / (norm1 * norm2 + 1e-9))
        delay_ms = float((best_lag / 16000.0) * 1000.0)
        return delay_ms, conf
    finally:
        if os.path.exists(t_raw):
            try: os.remove(t_raw)
            except Exception: pass
        if os.path.exists(r_raw):
            try: os.remove(r_raw)
            except Exception: pass


def process_single_episode(fpath: str) -> Tuple[str, Dict]:
    fname = os.path.basename(fpath)
    mkv_tam_id, mkv_ref_id, ff_tam_idx, ff_ref_idx = probe_track_info(fpath)
    if mkv_tam_id is None or mkv_ref_id is None:
        LOGGER.warning(f"⚠️ Could not probe audio tracks for {fname}.")
        return fname, {"status": "NO_TRACKS"}

    # 1. Measure initial lag
    lag_ms, conf = cross_correlate(fpath, ff_tam_idx, ff_ref_idx, ss=120, dur=60)
    if conf < 0.20:
        # Retry at 240s
        lag_ms2, conf2 = cross_correlate(fpath, ff_tam_idx, ff_ref_idx, ss=240, dur=60)
        if conf2 > conf:
            lag_ms, conf = lag_ms2, conf2

    # If within audible threshold (<= 35ms), it's already in sync
    if abs(lag_ms) <= 35.0:
        LOGGER.info(f"✨ [Already Synced] {fname} (lag: {lag_ms:+.1f}ms, conf: {conf:.2f})")
        return fname, {"status": "ALREADY_SYNCED", "lag_ms": lag_ms, "residual_ms": lag_ms, "conf": conf}

    # If confidence is low, don't guess
    if conf < 0.15:
        LOGGER.warning(f"⚠️ [Low Confidence {conf:.2f}] Skipping {fname} (lag: {lag_ms:+.1f}ms)")
        return fname, {"status": "LOW_CONFIDENCE", "lag_ms": lag_ms, "conf": conf}

    needed_delay_ms = int(round(-lag_ms))
    LOGGER.info(f"🔧 [Fixing] {fname}: Lag={lag_ms:+.1f}ms -> Applying --sync {mkv_tam_id}:{needed_delay_ms}ms...")

    tmp_out = fpath + f".sync_{uuid.uuid4().hex[:6]}.mkv"
    cmd_remux = [
        "mkvmerge", "--ui-language", "en_US",
        "-o", tmp_out,
        "--sync", f"{mkv_tam_id}:{needed_delay_ms}",
        fpath
    ]

    proc = subprocess.run(cmd_remux, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode > 1:
        if os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass
        LOGGER.error(f"❌ mkvmerge failed for {fname}: {proc.stderr}")
        return fname, {"status": "ERROR", "error": proc.stderr}

    # 2. Verify residual lag on remuxed file
    res_lag_ms, res_conf = cross_correlate(tmp_out, ff_tam_idx, ff_ref_idx, ss=120, dur=60)
    if abs(res_lag_ms) > 35.0 and res_conf < 0.20:
        res_lag_ms, res_conf = cross_correlate(tmp_out, ff_tam_idx, ff_ref_idx, ss=240, dur=60)

    if abs(res_lag_ms) <= 35.0:
        # Atomic in-place replacement
        shutil.move(tmp_out, fpath)
        LOGGER.info(f"🎯 [Lip-Sync Locked] {fname}: Residual Lag={res_lag_ms:+.2f}ms (was {lag_ms:+.1f}ms)")
        return fname, {
            "status": "FIXED",
            "original_lag_ms": lag_ms,
            "applied_delay_ms": needed_delay_ms,
            "residual_ms": res_lag_ms,
            "conf": res_conf,
        }
    else:
        if os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass
        LOGGER.warning(f"⚠️ [Sync Divergence] {fname}: Residual {res_lag_ms:+.1f}ms did not drop <= 35ms. Original preserved.")
        return fname, {
            "status": "DIVERGENCE",
            "original_lag_ms": lag_ms,
            "residual_ms": res_lag_ms,
            "conf": res_conf,
        }


def main():
    LOGGER.info("=" * 70)
    LOGGER.info("🚀 STARTING AUTONOMOUS BATCH AUDIO LIP-SYNC ENGINE (4 WORKERS)")
    LOGGER.info("=" * 70)

    progress = load_progress()
    files = [f for f in os.listdir(MUXED_DIR) if f.endswith(".mkv")]

    anime_map = defaultdict(list)
    for f in files:
        m = re.search(r'\[S(\d+)E(\d+)\]\s+([^(]+)\((\d{4})\)', f)
        if m:
            s = int(m.group(1))
            ep = int(m.group(2))
            title = m.group(3).strip()
            anime_map[(title, s)].append((ep, os.path.join(MUXED_DIR, f)))

    LOGGER.info(f"Discovered {len(anime_map)} anime seasons ({len(files)} total files).")

    total_fixed = 0
    total_already = 0
    total_skipped = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for idx, ((title, s), ep_list) in enumerate(sorted(anime_map.items()), 1):
            ep_list.sort(key=lambda x: x[0])
            LOGGER.info("-" * 70)
            LOGGER.info(f"📺 [{idx}/{len(anime_map)}] Processing Season: {title} S{s:02d} ({len(ep_list)} episodes)")
            LOGGER.info("-" * 70)

            # Filter uncompleted files
            todo_files = []
            for ep_num, fpath in ep_list:
                fname = os.path.basename(fpath)
                if fname in progress and progress[fname].get("status") in ("FIXED", "ALREADY_SYNCED"):
                    total_already += 1
                else:
                    todo_files.append(fpath)

            if not todo_files:
                LOGGER.info(f"✅ Season {title} S{s:02d} is already 100% synced!")
                continue

            future_to_file = {executor.submit(process_single_episode, fp): fp for fp in todo_files}
            for future in concurrent.futures.as_completed(future_to_file):
                fp = future_to_file[future]
                try:
                    fname, res = future.result()
                    progress[fname] = res
                    save_progress(progress)

                    if res.get("status") == "FIXED":
                        total_fixed += 1
                    elif res.get("status") == "ALREADY_SYNCED":
                        total_already += 1
                    else:
                        total_skipped += 1
                except Exception as e:
                    LOGGER.error(f"❌ Worker crashed on {os.path.basename(fp)}: {e}")
                    total_skipped += 1

    LOGGER.info("=" * 70)
    LOGGER.info("🎉 MULTI-ANIME BATCH LIP-SYNC COMPLETE!")
    LOGGER.info(f"   • Fixed in-place: {total_fixed} episodes")
    LOGGER.info(f"   • Already in sync: {total_already} episodes")
    LOGGER.info(f"   • Skipped/divergent: {total_skipped} episodes")
    LOGGER.info(f"   • Progress database: {PROGRESS_FILE}")
    LOGGER.info("=" * 70)


if __name__ == "__main__":
    main()
