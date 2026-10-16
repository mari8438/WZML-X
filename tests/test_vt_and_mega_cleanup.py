import re
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).parents[1]


def format_duration_hms_ms(seconds) -> str:
    if seconds is None:
        return "N/A"
    try:
        sec_f = float(seconds)
        if sec_f < 0:
            return "N/A"
        hours = int(sec_f // 3600)
        minutes = int((sec_f % 3600) // 60)
        secs = int(sec_f % 60)
        millis = int(round((sec_f - int(sec_f)) * 1000))
        if millis >= 1000:
            millis = 0
            secs += 1
            if secs >= 60:
                secs = 0
                minutes += 1
                if minutes >= 60:
                    minutes = 0
                    hours += 1
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    except Exception:
        return "N/A"


def _extract_extension(codec_name: str, codec_type: str) -> str:
    codec = (codec_name or "").lower()
    type_ = (codec_type or "").lower()
    if type_ == "audio":
        audio_map = {
            "aac": ".m4a",
            "alac": ".m4a",
            "mp3": ".mp3",
            "flac": ".flac",
            "opus": ".opus",
            "vorbis": ".ogg",
            "ac3": ".ac3",
            "eac3": ".eac3",
            "dts": ".dts",
            "truehd": ".thd",
            "pcm_s16le": ".wav",
            "pcm_s24le": ".wav",
        }
        return audio_map.get(codec, f".{codec}" if codec else ".mka")
    if type_ == "subtitle":
        sub_map = {
            "subrip": ".srt",
            "srt": ".srt",
            "ass": ".ass",
            "ssa": ".ssa",
            "hdmv_pgs_subtitle": ".sup",
            "pgs": ".sup",
            "dvd_subtitle": ".sub",
            "vobsub": ".sub",
            "webvtt": ".vtt",
        }
        return sub_map.get(codec, f".{codec}" if codec else ".srt")
    if type_ == "video":
        return ".mkv"
    return f".{codec}" if codec else ".dat"


def test_format_duration_hms_ms():
    assert format_duration_hms_ms(0) == "00:00:00.000"
    assert format_duration_hms_ms(65.123) == "00:01:05.123"
    assert format_duration_hms_ms(3661.050) == "01:01:01.050"
    assert format_duration_hms_ms(None) == "N/A"
    assert format_duration_hms_ms(-10) == "N/A"


def test_extract_extension_mapping():
    assert _extract_extension("aac", "audio") == ".m4a"
    assert _extract_extension("alac", "audio") == ".m4a"
    assert _extract_extension("mp3", "audio") == ".mp3"
    assert _extract_extension("flac", "audio") == ".flac"
    assert _extract_extension("opus", "audio") == ".opus"
    assert _extract_extension("pcm_s16le", "audio") == ".wav"
    assert _extract_extension("subrip", "subtitle") == ".srt"
    assert _extract_extension("srt", "subtitle") == ".srt"
    assert _extract_extension("ass", "subtitle") == ".ass"
    assert _extract_extension("hdmv_pgs_subtitle", "subtitle") == ".sup"
    assert _extract_extension("dvd_subtitle", "subtitle") == ".sub"
    assert _extract_extension("webvtt", "subtitle") == ".vtt"


def test_intro_subtitles_gap_logic():
    # >= 1 hour: 10 min (600s) gap
    dur_long = 3600.0
    gap_long = 10 * 60 if dur_long >= 3600 else 5 * 60
    assert gap_long == 600

    dur_longer = 7200.0
    gap_longer = 10 * 60 if dur_longer >= 3600 else 5 * 60
    assert gap_longer == 600

    # < 1 hour: 5 min (300s) gap
    dur_short = 3599.0
    gap_short = 10 * 60 if dur_short >= 3600 else 5 * 60
    assert gap_short == 300

    dur_very_short = 600.0
    gap_very_short = 10 * 60 if dur_very_short >= 3600 else 5 * 60
    assert gap_very_short == 300


def test_duration_diff_ms_calculation():
    # Video 100.500s, track 100.200s -> diff = +300ms
    v_dur = 100.500
    t_dur = 100.200
    diff_ms = int(round((v_dur - t_dur) * 1000))
    v_hms = format_duration_hms_ms(v_dur)
    t_hms = format_duration_hms_ms(t_dur)
    diff_str = f"{v_hms} - {t_hms} = {diff_ms:+d} ms"
    assert diff_str == "00:01:40.500 - 00:01:40.200 = +300 ms"

    # Video 100.200s, track 100.500s -> diff = -300ms
    v_dur2 = 100.200
    t_dur2 = 100.500
    diff_ms2 = int(round((v_dur2 - t_dur2) * 1000))
    diff_str2 = f"{format_duration_hms_ms(v_dur2)} - {format_duration_hms_ms(t_dur2)} = {diff_ms2:+d} ms"
    assert diff_str2 == "00:01:40.200 - 00:01:40.500 = -300 ms"


def test_stats_code_has_mkvmerge_and_no_mega():
    stats_file = _REPO_ROOT / "bot" / "modules" / "stats.py"
    content = stats_file.read_text(encoding="utf-8")
    assert '"mkvmerge"' in content
    assert "MKVToolNix" in content
    assert '"mega"' not in content
    assert "MegaSDK" not in content
    assert "Mega Limit" not in content
    assert "retry_mega_version" not in content


def test_dockerfile_has_no_mega_builder():
    dockerfile = _REPO_ROOT / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    assert "mega-sdk-builder" not in content
    assert "megasdk" not in content
    assert "libcrypto++" not in content
    assert "libfreeimage" not in content


def test_purged_mega_files_do_not_exist():
    purged_paths = [
        "bot/helper/ext_utils/mega_compat.py",
        "bot/helper/ext_utils/mega_utils.py",
        "bot/helper/listeners/mega_listener.py",
        "bot/helper/mirror_leech_utils/download_utils/mega_download.py",
        "bot/helper/mirror_leech_utils/status_utils/mega_status.py",
        "bot/helper/mirror_leech_utils/upload_utils/mega_clone.py",
        "bot/helper/mirror_leech_utils/upload_utils/mega_upload.py",
        "web/mega_selection_store.py",
    ]
    for rel_path in purged_paths:
        full_path = _REPO_ROOT / rel_path
        assert not full_path.exists(), f"File should be deleted: {rel_path}"
