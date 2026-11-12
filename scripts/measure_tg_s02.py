#!/usr/bin/env python3
import glob
import os
import re
import subprocess
import numpy as np
import scipy.signal

S02_DIR = "/home/administrator/starfall/downloads/muxed_output"
files = sorted([os.path.join(S02_DIR, f) for f in os.listdir(S02_DIR) if "Tokyo Ghoul" in f and "S02" in f and f.endswith(".mkv")])

print(f"Found {len(files)} S02 files.")

results = {}

for f in files:
    m = re.search(r'S02E(\d+)', f)
    if not m:
        continue
    ep_num = int(m.group(1))
    
    t1_raw = f"/tmp/tg2_t1_{ep_num}.raw"
    t3_raw = f"/tmp/tg2_t3_{ep_num}.raw"
    
    # 60s sample from 2m in
    cmd1 = ["ffmpeg", "-y", "-ss", "120", "-i", f, "-t", "60", "-map", "0:1", "-ac", "1", "-ar", "16000", "-f", "f32le", t1_raw]
    cmd2 = ["ffmpeg", "-y", "-ss", "120", "-i", f, "-t", "60", "-map", "0:3", "-ac", "1", "-ar", "16000", "-f", "f32le", t3_raw]
    
    subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    
    t1 = np.fromfile(t1_raw, dtype=np.float32)
    t3 = np.fromfile(t3_raw, dtype=np.float32)
    
    min_len = min(len(t1), len(t3))
    t1 = t1[:min_len]
    t3 = t3[:min_len]
    
    # Envelope correlation
    env1 = np.abs(scipy.signal.hilbert(t1))
    env3 = np.abs(scipy.signal.hilbert(t3))
    
    corr = scipy.signal.correlate(env1, env3, mode='full')
    lags = scipy.signal.correlation_lags(len(env1), len(env3), mode='full')
    best_lag = lags[np.argmax(corr)]
    delay_ms = (best_lag / 16000.0) * 1000.0
    
    # If delay_ms is negative, track 1 is early, so it needs positive delay
    needed_sync = -delay_ms
    results[ep_num] = needed_sync
    print(f"E{ep_num:02d}: Measured Lag = {delay_ms:+.1f} ms -> Needed Sync = {needed_sync:+.1f} ms")
    
    if os.path.exists(t1_raw): os.remove(t1_raw)
    if os.path.exists(t3_raw): os.remove(t3_raw)

print("\nSUMMARY S02:")
print(results)
