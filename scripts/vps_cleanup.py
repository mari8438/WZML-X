#!/usr/bin/env python3
import os
import shutil
import glob

def clean_vps():
    # 1. Clear downloaded torrent files & folders
    torrent_dir = "/home/administrator/starfall/data/torrents"
    saved_mux_dir = "/home/administrator/starfall/data/saved_muxes"
    tmp_mux_dir = "/home/administrator/starfall/data/tmp_mux"
    shm_mux = "/dev/shm/starfall_mux"

    print("--- 1. Cleaning Storage ---")
    for d in [torrent_dir, saved_mux_dir, tmp_mux_dir, shm_mux]:
        if os.path.exists(d):
            for item in os.listdir(d):
                p = os.path.join(d, item)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                    print(f"Removed: {p}")
                except Exception as e:
                    print(f"Error removing {p}: {e}")

    # 2. Selective Audio Bank Cleanup
    # Retain ONLY My Hero Academia, Attack on Titan, Fire Force
    print("\n--- 2. Cleaning Audio Bank ---")
    keep_kw = ["hero", "titan", "fire"]
    audio_dirs = [
        "/home/administrator/starfall/data/tam_audios",
        "/home/administrator/starfall/downloads/vt_audio_bank"
    ]
    
    deleted_count = 0
    kept_count = 0
    for adir in audio_dirs:
        if not os.path.exists(adir):
            continue
        for root, dirs, files in os.walk(adir):
            for f in files:
                if f == ".gitkeep":
                    continue
                fl = f.lower()
                fpath = os.path.join(root, f)
                if any(k in fl for k in keep_kw):
                    kept_count += 1
                else:
                    try:
                        os.remove(fpath)
                        deleted_count += 1
                        print(f"Deleted unwanted audio: {f}")
                    except Exception as e:
                        print(f"Error deleting {fpath}: {e}")

    print(f"\nCleanup Finished! Kept: {kept_count} target audio tracks, Deleted: {deleted_count} unwanted audio tracks.")

if __name__ == "__main__":
    clean_vps()
