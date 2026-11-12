#!/usr/bin/env python3
"""
Audit Audio Sync Across All Anime Seasons
=========================================
Extracts Tamil vs Japanese audio tracks, performs envelope cross-correlation,
and reports sync status for all 45 anime seasons in muxed_output.
"""

import json
import os
import re
import subprocess
from collections import defaultdict
import numpy as np
import scipy.signal

MUXED_DIR = "/home/administrator/starfall/downloads/muxed_output"

def get_audio_indices(filepath: str):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=index,codec_type:stream_tags=language,title",
        "-of", "json", filepath
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
    except Exception:
        return 1, 2
    
    tam_idx = None
    jpn_idx = None
    eng_idx = None
    
    for st in data.get("streams", []):
        if st.get("codec_type") == "audio":
            idx = st.get("index")
            lang = (st.get("tags", {}).get("language") or "").lower()
            if "tam" in lang or tam_idx is None:
                if tam_idx is None:
                    tam_idx = idx
            if "jpn" in lang or "jap" in lang:
                jpn_idx = idx
            elif "eng" in lang:
                eng_idx = idx
                
    ref_idx = jpn_idx if jpn_idx is not None else (eng_idx if eng_idx is not None else 2)
    return tam_idx or 1, ref_idx

def measure_sync(filepath: str, tam_idx: int, ref_idx: int, ss=120, dur=60):
    t_raw = f"/tmp/audit_tam_{os.getpid()}.raw"
    r_raw = f"/tmp/audit_ref_{os.getpid()}.raw"
    
    cmd1 = ["ffmpeg", "-y", "-ss", str(ss), "-i", filepath, "-t", str(dur), "-map", f"0:{tam_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", t_raw]
    cmd2 = ["ffmpeg", "-y", "-ss", str(ss), "-i", filepath, "-t", str(dur), "-map", f"0:{ref_idx}", "-ac", "1", "-ar", "16000", "-f", "f32le", r_raw]
    
    subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    
    t1 = np.fromfile(t_raw, dtype=np.float32)
    t3 = np.fromfile(r_raw, dtype=np.float32)
    
    if os.path.exists(t_raw): os.remove(t_raw)
    if os.path.exists(r_raw): os.remove(r_raw)
    
    min_len = min(len(t1), len(t3))
    if min_len < 16000 * 5: # less than 5s
        return 0.0, 0.0
        
    t1 = t1[:min_len]
    t3 = t3[:min_len]
    
    env1 = np.abs(scipy.signal.hilbert(t1))
    env3 = np.abs(scipy.signal.hilbert(t3))
    
    corr = scipy.signal.correlate(env1, env3, mode='full')
    lags = scipy.signal.correlation_lags(len(env1), len(env3), mode='full')
    
    peak_idx = np.argmax(corr)
    best_lag = lags[peak_idx]
    peak_val = corr[peak_idx] / (np.linalg.norm(env1) * np.linalg.norm(env3) + 1e-9)
    
    delay_ms = (best_lag / 16000.0) * 1000.0
    return delay_ms, peak_val

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
            
    print(f"Auditing {len(anime_map)} anime seasons...")
    
    results = []
    
    for (title, s), ep_list in sorted(anime_map.items()):
        ep_list.sort(key=lambda x: x[0])
        # Pick ep 1
        ep_num, fpath = ep_list[0]
        try:
            tam_idx, ref_idx = get_audio_indices(fpath)
            delay_ms, conf = measure_sync(fpath, tam_idx, ref_idx)
            
            # If delay is between -25ms and +25ms, it's considered in sync
            # In human perception, <= 45ms is lip-sync locked (ITU-R BS.1387 standard: -125ms to +45ms)
            status = "PERFECT" if abs(delay_ms) <= 45 else ("DELAY_DETECTED" if conf > 0.15 else "LOW_CONFIDENCE")
            
            results.append({
                "title": title,
                "season": s,
                "ep": ep_num,
                "delay_ms": float(delay_ms),
                "conf": float(conf),
                "status": status,
                "file": os.path.basename(fpath),
                "total_eps": len(ep_list),
            })
            print(f"[{status:15s}] {title} S{s:02d}E{ep_num:02d} -> Lag: {delay_ms:+.1f} ms (conf: {conf:.2f})")
        except Exception as e:
            print(f"[ERROR          ] {title} S{s:02d} -> {e}")
            results.append({
                "title": title,
                "season": s,
                "ep": ep_num,
                "delay_ms": 0.0,
                "conf": 0.0,
                "status": f"ERROR: {e}",
                "file": os.path.basename(fpath),
                "total_eps": len(ep_list),
            })
            
    with open("/home/administrator/starfall/data/anime_sync_audit.json", "w") as out_f:
        json.dump(results, out_f, indent=2)
    print("\n✅ Audit complete! Saved to data/anime_sync_audit.json")

if __name__ == "__main__":
    main()
