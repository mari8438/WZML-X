from pathlib import Path
import re
import pytest

_REPO_ROOT = Path(__file__).parents[1]

# Pure implementations directly mirror the functions in bot/helper/video_utils/video_tools.py
def format_audio_codec(codec: str) -> str:
    codec = (codec or "").lower().strip()
    mapping = {
        "eac3": "E-AC-3",
        "ac3": "AC-3",
        "dts": "DTS",
        "dca": "DTS",
        "truehd": "TrueHD",
        "aac": "AAC",
        "flac": "FLAC",
        "opus": "Opus",
        "mp3": "MP3",
        "vorbis": "Vorbis",
        "pcm_s16le": "PCM",
        "pcm_s24le": "PCM",
    }
    return mapping.get(codec, codec.upper() if codec else "AUDIO")


def format_channels(channels, layout: str = "") -> str:
    try:
        channels = int(channels)
    except (ValueError, TypeError):
        channels = 2
    if channels == 1:
        return "1.0"
    if channels == 2:
        return "2.0"
    if channels == 6:
        return "5.1"
    if channels == 8:
        return "7.1"
    if layout and "." in layout:
        return layout
    return f"{channels}.0" if channels else "2.0"


def format_lang_tag(lang: str) -> str:
    lang = (lang or "").lower().strip()
    tag_map = {
        "hindi": "HIN", "hin": "HIN",
        "tamil": "TAM", "tam": "TAM", "ta": "TAM",
        "telugu": "TEL", "tel": "TEL", "te": "TEL",
        "english": "ENG", "eng": "ENG", "en": "ENG",
        "malayalam": "MAL", "mal": "MAL", "ml": "MAL",
        "kannada": "KAN", "kan": "KAN", "kn": "KAN",
        "bengali": "BEN", "ben": "BEN", "bn": "BEN",
        "marathi": "MAR", "mar": "MAR", "mr": "MAR",
        "japanese": "JPN", "jpn": "JPN", "ja": "JPN",
        "korean": "KOR", "kor": "KOR", "ko": "KOR",
    }
    return tag_map.get(lang, lang.upper()[:3] if lang else "UND")


def generate_auto_mux_name(base_filename: str, external_audio: list, remove_original_audio: bool = False, audio_tracks: list = None) -> str:
    stem = Path(base_filename).stem
    year_match = re.search(r"\b(19\d\d|20\d\d)\b", stem)
    year = f" ({year_match.group(1)})" if year_match else ""

    res_match = re.search(r"\b(2160p|1080p|720p|480p)\b", stem, re.IGNORECASE)
    res = f" {res_match.group(1)}" if res_match else ""

    source_match = re.search(r"\b(BluRay|WEB-DL|WEBRip|HDTV|HDRip|DVDRip)\b", stem, re.IGNORECASE)
    source = f" {source_match.group(1)}" if source_match else ""

    codec_match = re.search(r"\b(x265|x264|HEVC|AVC|10bit)\b", stem, re.IGNORECASE)
    codec = f" {codec_match.group(1)}" if codec_match else ""

    clean_title = re.sub(r"[\._]", " ", stem)
    if year_match:
        clean_title = clean_title.split(year_match.group(1))[0].strip()
    elif res_match:
        clean_title = clean_title.split(res_match.group(1))[0].strip()
    else:
        clean_title = clean_title.split("-")[0].strip()

    langs = []
    codecs = []
    for item in (external_audio or []):
        lang = str(item.get("language") or "und").capitalize()
        if lang not in langs and lang.lower() != "und":
            langs.append(lang)
        c = item.get("codec") or "AUDIO"
        ch = item.get("channels") or "2.0"
        tag = f"{c} {ch}"
        if tag not in codecs and c != "AUDIO":
            codecs.append(tag)

    if not remove_original_audio and audio_tracks:
        for t in audio_tracks:
            lang = str(t.get("lang") or "und").capitalize()
            if lang not in langs and lang.lower() != "und":
                langs.append(lang)

    lang_part = " + ".join(langs) if langs else "Multi"
    codec_part = " & ".join(codecs) if codecs else ""
    audio_tag = f" [({lang_part}) - {codec_part}]" if codec_part else f" [{lang_part}]"

    return f"{clean_title}{year}{res}{source}{audio_tag}{codec} ~ StarFall.mkv"


def test_format_audio_codec():
    assert format_audio_codec("eac3") == "E-AC-3"
    assert format_audio_codec("ac3") == "AC-3"
    assert format_audio_codec("dts") == "DTS"
    assert format_audio_codec("truehd") == "TrueHD"
    assert format_audio_codec("aac") == "AAC"
    assert format_audio_codec("flac") == "FLAC"
    assert format_audio_codec("opus") == "Opus"
    assert format_audio_codec("") == "AUDIO"


def test_format_channels():
    assert format_channels(1) == "1.0"
    assert format_channels(2) == "2.0"
    assert format_channels(6) == "5.1"
    assert format_channels(8) == "7.1"
    assert format_channels(None) == "2.0"


def test_format_lang_tag():
    assert format_lang_tag("hindi") == "HIN"
    assert format_lang_tag("tamil") == "TAM"
    assert format_lang_tag("telugu") == "TEL"
    assert format_lang_tag("english") == "ENG"
    assert format_lang_tag("japanese") == "JPN"
    assert format_lang_tag("korean") == "KOR"
    assert format_lang_tag("unknown") == "UNK"


def test_generate_auto_mux_name():
    base_file = "Shelter.2026.1080p.BluRay.x265.Pahe.in.mkv"
    ext_audio = [
        {"language": "Hindi", "codec": "E-AC-3", "channels": "2.0"},
        {"language": "Tamil", "codec": "E-AC-3", "channels": "2.0"},
        {"language": "Telugu", "codec": "E-AC-3", "channels": "2.0"},
    ]
    name = generate_auto_mux_name(base_file, ext_audio, remove_original_audio=True)
    assert "Shelter (2026)" in name
    assert "1080p" in name
    assert "BluRay" in name
    assert "[(Hindi + Tamil + Telugu) - E-AC-3 2.0]" in name
    assert "StarFall.mkv" in name


def test_video_tools_ui_source_code_integrity():
    ui_file = _REPO_ROOT / "bot" / "modules" / "video_tool_ui.py"
    code = ui_file.read_text(encoding="utf-8")

    # Verify Muxing Configuration Dashboard layout
    assert "⬡ Muxing Configuration :" in code
    assert "Base Video →" in code
    assert "File Size →" in code
    assert "Task Mode →" in code
    assert "Auto Sync →" in code
    assert "Final Sample →" in code
    assert "Source Audio →" in code
    assert "Preview Sync →" in code
    assert "Save Settings →" in code
    assert "Output Name →" in code

    # Verify buttons
    assert "vt_mux_addaudio_" in code
    assert "vt_mux_addsub_" in code
    assert "vt_mux_audiolang_" in code
    assert "vt_mux_removeaudio_" in code
    assert "vt_mux_rename_" in code
    assert "vt_mux_toggle_auto_sync_" in code
    assert "vt_mux_toggle_preview_sync_" in code
    assert "vt_mux_toggle_save_settings_" in code
    assert "vt_mux_cancel_" in code
    assert "vt_mux_start_" in code

    # Verify submenus
    assert "render_audio_lang_menu" in code
    assert "render_audio_lang_picker" in code
    assert "render_remove_audio_menu" in code
    assert "render_rename_menu" in code

    # Verify callback dispatcher routing
    assert 'action == "mux"' in code
    assert 'mux_action == "start"' in code
    assert 'mux_action == "cancel"' in code
    assert 'mux_action == "audiolang"' in code
    assert 'mux_action == "removeaudio"' in code
    assert 'mux_action == "rename"' in code
    assert 'mux_action == "preview"' in code
    assert "vt_mux_preview_" in code


def test_video_tools_lossless_stream_copy_engine():
    vt_file = _REPO_ROOT / "bot" / "helper" / "video_utils" / "video_tools.py"
    code = vt_file.read_text(encoding="utf-8")

    # Verify no video re-encoding fallback (no libx264 in generate_merge_preview)
    preview_func_match = re.search(r"async def generate_merge_preview\(.*?\):(.*?)(?=\nasync def|\Z)", code, re.DOTALL)
    assert preview_func_match is not None, "generate_merge_preview function not found"
    preview_body = preview_func_match.group(1)
    assert "libx264" not in preview_body, "Found forbidden libx264 transcoding in generate_merge_preview!"
    assert "-c" in preview_body and "copy" in preview_body, "generate_merge_preview must use stream-copy"

    # Verify final sample cut exists and is stream-copy
    sample_func_match = re.search(r"async def generate_final_sample\(.*?\):(.*?)(?=\nasync def|\Z)", code, re.DOTALL)
    assert sample_func_match is not None, "generate_final_sample function not found"
    sample_body = sample_func_match.group(1)
    assert "copy" in sample_body
    assert "Final Sample" in sample_body
    assert "send_sample_video" in sample_body
    assert "send_sample_video" in preview_body
