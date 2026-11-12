#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Starfall Archival Stream Prober & Comprehensive Auto-Renamer.
Extracts real Video/Audio stream specs via mkvmerge/ffprobe and formats
Standard Episodes, OVAs/Specials, Movies, and NCOP/NCED Extras.
"""

import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ArchivalRenamer")

LANG_MAP = {
    "jpn": "Japanese",
    "ja": "Japanese",
    "eng": "English",
    "en": "English",
    "tam": "Tamil",
    "ta": "Tamil",
    "hin": "Hindi",
    "tel": "Telugu",
    "mal": "Malayalam",
    "chi": "Chinese",
    "zho": "Chinese",
    "kor": "Korean",
    "und": "Japanese",
}


def probe_media_streams(filepath: str, torrent_name: str = "") -> Dict[str, Any]:
    """
    Probes an MKV file via mkvmerge -J and ffprobe to extract exact video codec,
    resolution, bit-depth, dynamic range, audio language/codec/channels, and subtitle count.
    """
    info: Dict[str, Any] = {
        "resolution": "1080p",
        "codec": "x265",
        "codec_badge": "HEVC",
        "bit_depth": "10bit",
        "bit_num": "10",
        "dynamic_range": "SDR",
        "quality": "BluRay",
        "video_stream_main": "1080p Ultra HD",
        "video_stream_sub": "HEVC • 10-bit SDR",
        "audio_badge": "Japanese FLAC 2.0",
        "audio_file_tag": "Japanese-FLAC 2.0",
        "sub_tag": "ESub",
        "duration": 0,
        "width": 1920,
        "height": 1080,
    }

    # Detect source quality (BluRay vs WEBRip) from torrent name + filename
    combined_name = f"{torrent_name} {os.path.basename(filepath)}".upper()
    if any(w in combined_name for w in ("WEBRIP", "WEB-DL", "WEBDL", "WEB ")):
        if not any(b in combined_name for b in ("BLURAY", "BLU-RAY", "BDRIP", "BD ")):
            info["quality"] = "WEBRip"

    try:
        proc = subprocess.run(
            ["mkvmerge", "-J", filepath],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            tracks = data.get("tracks", [])

            v_tracks = [t for t in tracks if t.get("type") == "video"]
            a_tracks = [t for t in tracks if t.get("type") == "audio"]
            s_tracks = [t for t in tracks if t.get("type") == "subtitles"]

            # 1. Video Probing
            if v_tracks:
                vt = v_tracks[0]
                v_codec_raw = str(vt.get("codec", "")).upper()
                v_props = vt.get("properties", {})

                dims = str(v_props.get("pixel_dimensions", "1920x1080"))
                if "x" in dims:
                    try:
                        w_str, h_str = dims.split("x")[:2]
                        w_i, h_i = int(w_str), int(h_str)
                        info["width"], info["height"] = w_i, h_i
                        if w_i >= 3800 or h_i >= 2100:
                            info["resolution"] = "2160p"
                        elif w_i >= 1800 or h_i >= 1000:
                            info["resolution"] = "1080p"
                        elif w_i >= 1200 or h_i >= 700:
                            info["resolution"] = "720p"
                    except Exception:
                        pass

                if "AV1" in v_codec_raw:
                    info["codec"] = "AV1"
                    info["codec_badge"] = "AV1"
                elif any(k in v_codec_raw for k in ("HEVC", "H.265", "H265", "265")):
                    info["codec"] = "x265"
                    info["codec_badge"] = "HEVC"
                elif any(k in v_codec_raw for k in ("AVC", "H.264", "H264", "264")):
                    info["codec"] = "x264"
                    info["codec_badge"] = "AVC"

            # 2. Audio Probing
            if a_tracks:
                langs = []
                codecs = []
                chans = []
                for at in a_tracks:
                    a_props = at.get("properties", {})
                    lang_code = str(a_props.get("language", "jpn")).lower()
                    lang_name = LANG_MAP.get(lang_code, "Japanese")
                    if lang_name not in langs:
                        langs.append(lang_name)

                    ac_raw = str(at.get("codec", "AAC")).upper()
                    if "FLAC" in ac_raw:
                        ac_clean = "FLAC"
                    elif "OPUS" in ac_raw:
                        ac_clean = "Opus"
                    elif "E-AC-3" in ac_raw or "EAC3" in ac_raw:
                        ac_clean = "DDP"
                    elif "AC-3" in ac_raw or "AC3" in ac_raw:
                        ac_clean = "AC3"
                    elif "TRUEHD" in ac_raw:
                        ac_clean = "TrueHD"
                    elif "DTS" in ac_raw:
                        ac_clean = "DTS"
                    elif "PCM" in ac_raw:
                        ac_clean = "PCM"
                    else:
                        ac_clean = "AAC"
                    codecs.append(ac_clean)

                    ch_num = int(a_props.get("audio_channels", 2) or 2)
                    if ch_num >= 8:
                        ch_str = "7.1"
                    elif ch_num >= 6:
                        ch_str = "5.1"
                    elif ch_num == 1:
                        ch_str = "1.0"
                    else:
                        ch_str = "2.0"
                    chans.append(ch_str)

                primary_codec = codecs[0] if codecs else "AAC"
                primary_chan = chans[0] if chans else "2.0"

                if len(a_tracks) >= 2 and ("Japanese" in langs and "English" in langs):
                    lang_label = "Dual Audio"
                elif len(langs) >= 2:
                    lang_label = "Multi Audio"
                else:
                    lang_label = langs[0] if langs else "Japanese"

                info["audio_badge"] = f"{lang_label} {primary_codec} {primary_chan}"
                info["audio_file_tag"] = f"{lang_label}-{primary_codec} {primary_chan}"

            # 3. Subtitles Probing
            if len(s_tracks) > 2:
                info["sub_tag"] = "MSub"
            else:
                info["sub_tag"] = "ESub"

    except Exception as e:
        LOGGER.warning(f"mkvmerge probe warning on {filepath}: {e}")

    # Probe bit-depth via ffprobe if available
    try:
        fp = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=pix_fmt,color_transfer",
                "-of", "json",
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if fp.returncode == 0 and fp.stdout:
            fp_data = json.loads(fp.stdout)
            streams = fp_data.get("streams", [])
            if streams:
                pix_fmt = str(streams[0].get("pix_fmt", "")).lower()
                c_tr = str(streams[0].get("color_transfer", "")).lower()
                if "10" in pix_fmt or "p010" in pix_fmt:
                    info["bit_depth"] = "10bit"
                    info["bit_num"] = "10"
                elif "12" in pix_fmt:
                    info["bit_depth"] = "12bit"
                    info["bit_num"] = "12"
                else:
                    info["bit_depth"] = "8bit"
                    info["bit_num"] = "8"

                if any(h in c_tr for h in ("smpte2084", "arib-std-b67", "bt2020-10")):
                    info["dynamic_range"] = "HDR"
                else:
                    info["dynamic_range"] = "SDR"
    except Exception:
        pass

    res_label = info["resolution"]
    info["video_stream_main"] = f"{res_label} HD"
    info["video_stream_sub"] = f"{info['codec_badge']} • {info['bit_num']}-bit {info['dynamic_range']}"
    return info


def parse_episode_or_special_tag(
    filepath: str,
    default_season: int = 1,
) -> Tuple[int, int, int, str]:
    """
    Classifies a video file and returns:
      (category_rank, season_num, ep_num, formatted_tag)
    category_rank:
      1 = Standard Episode ([S01E01]) -> always sorted first!
      2 = OVA / Special ([OVA 01] / [SP 01])
      3 = Movie ([Movie] / [Movie 01])
      4 = Extras / Bonus ([NCOP 01] / [NCED 01] / [Extra 01])
    """
    fname = os.path.basename(filepath)
    parent = os.path.basename(os.path.dirname(filepath))
    stem = os.path.splitext(fname)[0]

    # Strip common hashes, brackets at start/end, resolutions
    clean = re.sub(r"\[[0-9A-Fa-f]{8}\]", "", stem)
    clean = re.sub(r"\b(1080p|720p|2160p|4K|x265|x264|HEVC|AV1|10bit|8bit|BluRay|WEBRip|BD|AAC|FLAC|Opus|5\.1|2\.0)\b", "", clean, flags=re.IGNORECASE)

    # Detect season from parent folder or filename
    season = default_season
    s_match = re.search(r"\b(?:Season|S)\s*0*(\d{1,2})\b", f"{parent} {clean}", re.IGNORECASE)
    if s_match:
        s_cand = int(s_match.group(1))
        if 1 <= s_cand <= 15:
            season = s_cand

    # Only use parent folder name for Extra/OVA/Special/Movie classification if it is a dedicated short subfolder
    # (NOT the root batch folder name like "Monster (2004) 01 ~ 74 + Extra" or "[Judas] Anime (Season 1 + OVA + Movie)")
    is_special_subfolder = bool(
        len(parent.strip()) <= 24
        and re.match(
            r"^(?:extras?|bonus(?:es)?|specials?|sp|ovas?|oads?|onas?|movies?|films?|ncop|nced|op|ed|featurettes?|pv|cm|previews?|trailers?|menus?)(?:[\s_-]*\d+)?$",
            parent.strip(),
            re.IGNORECASE,
        )
    )
    tag_ctx = f"{parent} {clean}" if is_special_subfolder else clean

    # 1. Check for NCOP / NCED / Extras
    ncop_m = re.search(r"\b(?:NCOP|Creditless\s*OP|Clean\s*OP)[.\s_-]*0*(\d{0,2})\b", tag_ctx, re.IGNORECASE)
    if ncop_m:
        num = int(ncop_m.group(1)) if ncop_m.group(1) else 1
        return (4, season, num, f"NCOP {num:02d}")

    nced_m = re.search(r"\b(?:NCED|Creditless\s*ED|Clean\s*ED)[.\s_-]*0*(\d{0,2})\b", tag_ctx, re.IGNORECASE)
    if nced_m:
        num = int(nced_m.group(1)) if nced_m.group(1) else 1
        return (4, season, num, f"NCED {num:02d}")

    if re.search(r"\b(PV|CM|Preview|Menu|Trailer|Bonus|Extra)\b", tag_ctx, re.IGNORECASE):
        ex_m = re.search(r"\b(?:PV|CM|Extra|Bonus)[.\s_-]*0*(\d{1,2})\b", clean, re.IGNORECASE)
        num = int(ex_m.group(1)) if ex_m else 1
        return (4, season, num, f"Extra {num:02d}" if ex_m else "Extra")

    # 2. Check for OVA / OAD / Special
    ova_m = re.search(r"\b(?:OVA|OAD|ONA)[.\s_-]*0*(\d{0,2})\b", tag_ctx, re.IGNORECASE)
    if ova_m and not re.search(r"\bS\d{1,2}E\d{1,3}\b", clean, re.IGNORECASE):
        num = int(ova_m.group(1)) if ova_m.group(1) else 1
        # Check if episode number exists separately in filename
        ep_in_ova = re.search(r"(?:-\s*|\bE)(\d{1,3})\b", clean)
        if ep_in_ova and not ova_m.group(1):
            num = int(ep_in_ova.group(1))
        return (2, season, num, f"OVA {num:02d}")

    sp_m = re.search(r"\b(?:Specials?|SP|S00E)[.\s_-]*0*(\d{0,2})\b", tag_ctx, re.IGNORECASE)
    if sp_m:
        num = int(sp_m.group(1)) if sp_m.group(1) else 1
        return (2, season, num, f"SP {num:02d}")

    # 3. Check for Movie
    if re.search(r"\b(Movie|Film|Gekijouban)\b", tag_ctx, re.IGNORECASE) and not re.search(r"\bS\d{1,2}E\d{1,3}\b", clean, re.IGNORECASE):
        mv_m = re.search(r"\b(?:Movie|Film)[.\s_-]*0*(\d{1,2})\b", clean, re.IGNORECASE)
        if mv_m:
            num = int(mv_m.group(1))
            return (3, season, num, f"Movie {num:02d}")
        return (3, season, 1, "Movie")

    # 4. Standard Episode Extraction
    # Pattern A: S01E01 / S1E12 / S02E01v2
    se_m = re.search(r"\bS0*(\d{1,2})E0*(\d{1,3})(?:v\d+)?\b", clean, re.IGNORECASE)
    if se_m:
        s_num = int(se_m.group(1))
        e_num = int(se_m.group(2))
        if s_num == 0:
            return (2, default_season, e_num, f"SP {e_num:02d}")
        return (1, s_num, e_num, f"S{s_num:02d}E{e_num:02d}")

    # Pattern B: 01x12 / 01x12v2
    sx_m = re.search(r"\b0*(\d{1,2})x0*(\d{1,3})(?:v\d+)?\b", clean, re.IGNORECASE)
    if sx_m:
        s_num = int(sx_m.group(1))
        e_num = int(sx_m.group(2))
        return (1, s_num, e_num, f"S{s_num:02d}E{e_num:02d}")

    # Pattern C: " - 01 " or " - 01v2" or "Ep 01" or "Episode 01" or "Chapter 01"
    ep_m = re.search(r"(?:-\s*|\b(?:Ep(?:isode)?|Ch(?:apter)?)[.\s_-]*)0*(\d{1,3})(?:v\d)?\b", clean, re.IGNORECASE)
    if ep_m:
        e_num = int(ep_m.group(1))
        return (1, season, e_num, f"S{season:02d}E{e_num:02d}")

    # Pattern D: [01] orstandalone 2-3 digit number near end
    br_m = re.search(r"\[0*(\d{1,3})(?:v\d)?\]", clean)
    if br_m:
        e_num = int(br_m.group(1))
        return (1, season, e_num, f"S{season:02d}E{e_num:02d}")

    tail_m = re.findall(r"\b0*(\d{1,3})\b", clean)
    if tail_m:
        for cand in reversed(tail_m):
            c_int = int(cand)
            if 0 <= c_int <= 500 and c_int not in (1990, 2020, 2021, 2022, 2023, 2024, 2025, 2026, 264, 265, 10, 8):
                return (1, season, c_int, f"S{season:02d}E{c_int:02d}")

    return (1, season, 1, f"S{season:02d}E01")


def format_archival_filename(
    title: str,
    tag: str,
    year: str,
    encoder: str,
    stream_info: Dict[str, Any],
) -> str:
    """
    Formats the canonical Starfall archival filename:
    🄰🅂- [S01E01] Title (Year) 1080p 10bit BluRay {Codec} [{Audio}] ESub ~ {Encoder} ~ [@Anime_Starfall🥰].mkv
    """
    clean_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
    res = stream_info.get("resolution", "1080p")
    bit = stream_info.get("bit_depth", "10bit")
    qual = stream_info.get("quality", "BluRay")
    codec = stream_info.get("codec", "x265")
    audio_tag = stream_info.get("audio_file_tag", "Japanese-FLAC 2.0")
    sub_tag = stream_info.get("sub_tag", "ESub")
    clean_enc = encoder.split("(")[0].strip() if encoder else "Judas"

    return (
        f"🄰🅂- [{tag}] {clean_title} ({year}) {res} {bit} {qual} {codec} "
        f"[{audio_tag}] {sub_tag} ~ {clean_enc} ~ [@Anime_Starfall🥰].mkv"
    )
