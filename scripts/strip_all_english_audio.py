#!/usr/bin/env python3
"""
High-Speed Parallel English Audio Stripper
=========================================
Uses 8 parallel workers to scan all MKV files in muxed_output.
Detects English audio tracks and losslessly strips them out.
Retains strictly Tamil and Japanese audio tracks.
"""

import concurrent.futures
import glob
import json
import logging
import os
import shutil
import subprocess
import time

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
)
LOGGER = logging.getLogger("EnglishStripper")

TARGET_DIR = "/home/administrator/starfall/downloads/muxed_output"


def process_file(fpath: str) -> bool:
    fname = os.path.basename(fpath)
    cmd_info = ["mkvmerge", "-J", fpath]
    res = subprocess.run(cmd_info, capture_output=True, text=True)
    if res.returncode != 0:
        return False
    try:
        meta = json.loads(res.stdout)
    except Exception:
        return False

    keep_audio_ids = []
    has_english = False
    for tr in meta.get("tracks", []):
        if tr.get("type") == "audio":
            props = tr.get("properties", {})
            lang = (props.get("language") or "").lower()
            name = (props.get("track_name") or "").lower()
            if lang in ["eng", "en"] or "eng" in name:
                has_english = True
            else:
                keep_audio_ids.append(tr.get("id"))

    if not has_english:
        return False

    tmp_out = fpath + ".strip.mkv"
    cmd_strip = ["mkvmerge", "--ui-language", "en_US", "-o", tmp_out]
    if keep_audio_ids:
        cmd_strip.extend(["-a", ",".join(str(i) for i in keep_audio_ids)])
    else:
        cmd_strip.append("-A")
    cmd_strip.append(fpath)

    proc = subprocess.run(cmd_strip, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode <= 1 and os.path.exists(tmp_out):
        old_size = os.path.getsize(fpath)
        new_size = os.path.getsize(tmp_out)
        shutil.move(tmp_out, fpath)
        diff_mb = (old_size - new_size) / (1024 * 1024)
        LOGGER.info(f"🚫 [Stripped English] {fname[:70]}... (saved {diff_mb:.1f} MB, kept {keep_audio_ids})")
        return True
    else:
        if os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass
        return False


def main():
    files = sorted(glob.glob(os.path.join(TARGET_DIR, "*.mkv")))
    LOGGER.info(f"🚀 Scanning {len(files)} files in muxed_output for English audio removal...")
    t0 = time.time()
    stripped_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(process_file, files))
        stripped_count = sum(1 for r in results if r)

    elapsed = time.time() - t0
    LOGGER.info("=" * 70)
    LOGGER.info(f"🎉 COMPLETED: Stripped English audio from {stripped_count} files in {elapsed:.1f}s!")
    LOGGER.info("=" * 70)


if __name__ == "__main__":
    main()
