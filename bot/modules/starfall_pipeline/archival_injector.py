#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Starfall Lossless Archival Subtitle & Container Cover Injector.
Prepends 'Anime Starfall Intro' (data/intro_starfall.ass) as the FIRST subtitle track
(track-name="Anime Starfall Intro", language=eng, default=1) and attaches
data/intro_preroll.jpg as cover.jpg without re-encoding or merging video/audio.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from typing import List

LOGGER = logging.getLogger("ArchivalInjector")

INTRO_ASS_PATH = "/home/administrator/starfall/data/intro_starfall.ass"
COVER_JPG_PATH = "/home/administrator/starfall/data/intro_preroll.jpg"


def inject_starfall_archival_branding(
    input_mkv: str,
    output_mkv: str,
    intro_ass: str = INTRO_ASS_PATH,
    cover_jpg: str = COVER_JPG_PATH,
) -> bool:
    """
    Injects the Anime Starfall Intro subtitle track as the first subtitle track
    and attaches cover.jpg while retaining 100% of native video, audio, subtitles, and fonts.
    """
    if not os.path.exists(input_mkv):
        LOGGER.error(f"Input file does not exist: {input_mkv}")
        return False

    os.makedirs(os.path.dirname(os.path.abspath(output_mkv)), exist_ok=True)
    t0 = time.time()

    try:
        probe_proc = subprocess.run(
            ["mkvmerge", "-J", input_mkv],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if probe_proc.returncode != 0:
            LOGGER.warning(f"mkvmerge -J failed on {input_mkv}, copying directly.")
            shutil.copy2(input_mkv, output_mkv)
            return True

        probe = json.loads(probe_proc.stdout)
        tracks = probe.get("tracks", [])
        attachments = probe.get("attachments", [])

        v_ids: List[int] = [t["id"] for t in tracks if t.get("type") == "video"]
        a_ids: List[int] = [t["id"] for t in tracks if t.get("type") == "audio"]
        s_ids: List[int] = [t["id"] for t in tracks if t.get("type") == "subtitles"]

        # Keep all font attachments, drop old cover.jpg/png to avoid duplicate cover attachments
        font_att_ids: List[str] = []
        for att in attachments:
            att_id = att.get("id")
            fname = str(att.get("file_name", "")).lower()
            ctype = str(att.get("content_type", "")).lower()
            if "cover" in fname or ctype.startswith("image/"):
                continue
            if att_id is not None:
                font_att_ids.append(str(att_id))

        cmd: List[str] = [
            "mkvmerge",
            "--ui-language", "en_US",
            "-o", output_mkv,
            "--title", "[@Anime_Starfall🥰]",
        ]

        # Input 0: source MKV
        if attachments:
            if font_att_ids:
                cmd.extend(["--attachments", ",".join(font_att_ids)])
            else:
                cmd.append("--no-attachments")

        # Turn off default flag on existing subtitles so Starfall Intro is primary default sub
        for sid in s_ids:
            cmd.extend(["--default-track-flag", f"{sid}:no"])

        cmd.append(input_mkv)

        # Input 1: Starfall Intro ASS subtitle
        has_intro = os.path.exists(intro_ass)
        if has_intro:
            cmd.extend([
                "--track-name", "0:Anime Starfall Intro",
                "--language", "0:eng",
                "--default-track-flag", "0:yes",
                intro_ass,
            ])

        # Track ordering: Video -> Audio -> Starfall Intro Sub -> Native Subtitles
        order: List[str] = []
        for vid in v_ids:
            order.append(f"0:{vid}")
        for aid in a_ids:
            order.append(f"0:{aid}")
        if has_intro:
            order.append("1:0")
        for sid in s_ids:
            order.append(f"0:{sid}")

        if order:
            cmd.extend(["--track-order", ",".join(order)])

        # Attach cover.jpg
        if os.path.exists(cover_jpg):
            cmd.extend([
                "--attachment-name", "cover.jpg",
                "--attachment-mime-type", "image/jpeg",
                "--attach-file", cover_jpg,
            ])

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        elapsed = time.time() - t0

        if res.returncode in (0, 1) and os.path.exists(output_mkv) and os.path.getsize(output_mkv) > 1024 * 100:
            LOGGER.info(
                f"✨ Injected Starfall Intro & Cover into {os.path.basename(output_mkv)} in {elapsed:.2f}s"
            )
            return True
        else:
            LOGGER.error(f"mkvmerge returned {res.returncode}: {res.stderr[:300]}")
            shutil.copy2(input_mkv, output_mkv)
            return os.path.exists(output_mkv)

    except Exception as e:
        LOGGER.error(f"Exception in inject_starfall_archival_branding: {e}")
        try:
            shutil.copy2(input_mkv, output_mkv)
            return os.path.exists(output_mkv)
        except Exception:
            return False
