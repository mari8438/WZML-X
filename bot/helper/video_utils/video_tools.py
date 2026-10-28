import asyncio
import json
import os
import re
import shutil
from contextlib import suppress
from asyncio import Event, create_subprocess_exec, create_task, wait_for
from asyncio.subprocess import PIPE
from os import path as ospath, walk
from time import time

from aiofiles import open as aiopen
from aiofiles.os import listdir, makedirs, path as aiopath, remove, rename
from aioshutil import rmtree

from ... import LOGGER, DOWNLOAD_DIR
from ...core.config_manager import BinConfig, Config
from ..ext_utils.bot_utils import cmd_exec, sync_to_async
from ..ext_utils.ffmpeg_queue import ffmpeg_task
from ..ext_utils.files_utils import get_source_container_name
from ..ext_utils.links_utils import is_url
from ..ext_utils.performance import get_ffmpeg_cores, get_ffmpeg_threads
from ..telegram_helper.message_utils import send_message

# Extensions that are valid for video tools
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".m4v", ".3gp",
    ".ogv", ".divx", ".rmvb", ".asf",
}
AUDIO_EXTENSIONS = {".aac", ".ac3", ".eac3", ".dts", ".flac", ".m4a", ".mka", ".mp3", ".ogg", ".opus", ".wav"}
SUBTITLE_EXTENSIONS = {".ass", ".srt", ".ssa", ".vtt"}

UI_TIMEOUT = 900  # 15 minutes

# Global dict to hold active video tool sessions: {task_id: event}
_active_vt_sessions = {}

ISO_LANG_PATTERNS = [
    (re.compile(r"\b(tam|tamil|tml)\b", re.I), "tam", "Tamil"),
    (re.compile(r"\b(tel|telugu|tlg)\b", re.I), "tel", "Telugu"),
    (re.compile(r"\b(hin|hindi|hnd)\b", re.I), "hin", "Hindi"),
    (re.compile(r"\b(mal|malayalam|mlym)\b", re.I), "mal", "Malayalam"),
    (re.compile(r"\b(kan|kannada|knd)\b", re.I), "kan", "Kannada"),
    (re.compile(r"\b(jpn|jap|japanese)\b", re.I), "jpn", "Japanese"),
    (re.compile(r"\b(eng|english)\b", re.I), "eng", "English"),
]

def normalize_mkv_track_meta(raw_lang: str, raw_title: str, codec: str = "", channels: str = "") -> tuple[str, str]:
    """Normalize track language to ISO 639-2 and build a clean StarFallX track title."""
    combined = f"{raw_lang or ''} {raw_title or ''}"
    for pattern, iso_code, label in ISO_LANG_PATTERNS:
        if pattern.search(combined):
            suffix = " ".join(filter(None, [codec.upper(), str(channels) if channels else ""]))
            clean_title = f"{label} • {suffix}" if suffix else label
            return iso_code, clean_title
    iso_fallback = raw_lang.lower() if (raw_lang and len(raw_lang) == 3 and raw_lang.isalpha()) else "und"
    return iso_fallback, (raw_title or "Track")


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
    """Constructs an informative release title, e.g. Shelter (2026) 1080p BluRay [(Hindi + Tamil + Telugu) - DDP 2.0] ~ StarFall.mkv"""
    stem, ext = ospath.splitext(base_filename)
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


async def _probe_external_audio_details(path, stream_index=0):
    codec = "AUDIO"
    channels = "2.0"
    lang = "und"
    size_mb = 0.0
    with suppress(Exception):
        if await aiopath.isfile(path):
            size_mb = (await aiopath.getsize(path)) / (1024 * 1024)
    result = await cmd_exec(
        [
            "ffprobe", "-v", "error", "-select_streams", f"a:{stream_index}",
            "-show_entries", "stream=codec_name,channels,channel_layout:stream_tags=language,title",
            "-of", "json", path,
        ]
    )
    if result[2] == 0:
        with suppress(Exception):
            data = json.loads(result[0] or "{}")
            streams = data.get("streams", [])
            if streams:
                st = streams[0]
                codec = format_audio_codec(st.get("codec_name", ""))
                ch_cnt = st.get("channels", 2)
                ch_layout = st.get("channel_layout", "")
                channels = format_channels(ch_cnt, ch_layout)
                tags = st.get("tags") or {}
                if tags.get("language"):
                    lang = tags["language"]
    return {
        "codec": codec,
        "channels": channels,
        "language": lang,
        "tag": format_lang_tag(lang),
        "size_mb": round(size_mb, 2),
    }

async def execute_mkvmerge_with_fallback(
    mkv_cmd: list[str],
    primary_input: str,
    temp_output: str,
    cmd_exec_fn,
) -> tuple[bool, str]:
    """
    Tier 1: mkvmerge with --normalize-language-ietf canonical & --enable-legacy-font-mime-types
            (treats returncode 0 and 1 as success if output file > 0 bytes).
    Tier 2: If returncode == 2 (corrupt Matroska header/cues), repair via zero-reencode
            FFmpeg (-fflags +genpts+igndts -err_detect ignore_err -c copy) and retry mkvmerge.
    """
    enriched_cmd = [
        mkv_cmd[0],
        "--normalize-language-ietf", "canonical",
        "--enable-legacy-font-mime-types",
        *mkv_cmd[1:],
    ]
    stdout, stderr, rc = await cmd_exec_fn(enriched_cmd)
    if rc in (0, 1) and os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
        return True, stdout

    repaired_input = f"{primary_input}.repaired.mkv"
    try:
        repair_cmd = [
            BinConfig.FFMPEG_NAME, "-y", "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+igndts", "-err_detect", "ignore_err",
            "-i", primary_input, "-map", "0", "-c", "copy", repaired_input,
        ]
        _, rep_err, rep_rc = await cmd_exec_fn(repair_cmd)
        if rep_rc == 0 and os.path.exists(repaired_input) and os.path.getsize(repaired_input) > 0:
            retry_cmd = [repaired_input if arg == primary_input else arg for arg in enriched_cmd]
            stdout2, stderr2, rc2 = await cmd_exec_fn(retry_cmd)
            if rc2 in (0, 1) and os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
                return True, stdout2
            return False, (stderr2 or stdout2)[-1500:]
        return False, (rep_err or stderr or stdout)[-1500:]
    finally:
        if os.path.exists(repaired_input):
            try:
                os.remove(repaired_input)
            except OSError:
                pass


def _reply_timeout():
    try:
        timeout = int(getattr(Config, "VIDEO_TOOLS_REPLY_TIMEOUT", 30) or 30)
    except (TypeError, ValueError):
        timeout = 30
    return max(10, timeout)


def _video_tools_logs():
    return bool(getattr(Config, "VIDEO_TOOLS_LOGS", True))


def _lang_tokens(value):
    return [item.strip().lower() for item in re.split(r"[,|\s>]+", str(value or "")) if item.strip()]


def _lang_aliases(value):
    lang = str(value or "").strip().lower()
    aliases = {
        "ta": {"ta", "tam", "tamil"},
        "tam": {"ta", "tam", "tamil"},
        "tamil": {"ta", "tam", "tamil"},
        "te": {"te", "tel", "telugu"},
        "tel": {"te", "tel", "telugu"},
        "telugu": {"te", "tel", "telugu"},
        "en": {"en", "eng", "english"},
        "eng": {"en", "eng", "english"},
        "english": {"en", "eng", "english"},
        "hi": {"hi", "hin", "hindi"},
        "hin": {"hi", "hin", "hindi"},
        "hindi": {"hi", "hin", "hindi"},
    }
    return aliases.get(lang, {lang})


def _normalize_language(value):
    token = str(value or "und").strip().lower()
    normalized = {
        "ta": "tam", "tamil": "tam", "tam": "tam",
        "en": "eng", "english": "eng", "eng": "eng",
        "hi": "hin", "hindi": "hin", "hin": "hin",
        "te": "tel", "telugu": "tel", "tel": "tel",
        "ja": "jpn", "japanese": "jpn", "jpn": "jpn",
        "ko": "kor", "korean": "kor", "kor": "kor",
        "zh": "zho", "chinese": "zho", "mandarin": "zho", "zho": "zho",
    }
    return normalized.get(token, token[:3] or "und")


def _track_matches(track, wanted):
    if not wanted:
        return False
    values = _lang_aliases(track.get("lang")) | _lang_aliases(track.get("title"))
    return bool(values & wanted)


def _ordered_track_indexes(tracks, order_value, remove_unmatched=False):
    tokens = _lang_tokens(order_value)
    if not tokens:
        return [track["index"] for track in tracks]
    used = set()
    ordered = []
    for token in tokens:
        wanted = _lang_aliases(token)
        for track in tracks:
            idx = track["index"]
            if idx not in used and _track_matches(track, wanted):
                ordered.append(idx)
                used.add(idx)
    if not remove_unmatched:
        ordered.extend(track["index"] for track in tracks if track["index"] not in used)
    return ordered


def get_vt_event(task_id):
    session = _active_vt_sessions.get(task_id)
    return session["event"] if session else None


def get_vt_state(task_id):
    session = _active_vt_sessions.get(task_id)
    return session["state"] if session else None


def get_vt_session(task_id):
    return _active_vt_sessions.get(task_id)


async def probe_streams(file_path):
    """Use ffprobe to get audio and subtitle stream info."""
    result = await cmd_exec(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            file_path,
        ]
    )
    if not result[0] or result[2] != 0:
        return [], []

    try:
        data = json.loads(result[0])
    except (json.JSONDecodeError, Exception):
        return [], []

    audio_tracks = []
    sub_tracks = []

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type", "")
        tags = stream.get("tags", {})
        lang = tags.get("language", "Unknown")
        title = tags.get("title", "")

        if codec_type == "audio":
            idx = len(audio_tracks)
            audio_tracks.append(
                {
                    "index": idx,
                    "stream_index": stream.get("index", idx),
                    "lang": lang,
                    "title": title or f"Audio {idx + 1}",
                    "codec": stream.get("codec_name", "unknown"),
                }
            )
        elif codec_type == "subtitle":
            idx = len(sub_tracks)
            sub_tracks.append(
                {
                    "index": idx,
                    "stream_index": stream.get("index", idx),
                    "lang": lang,
                    "title": title or f"Subtitle {idx + 1}",
                    "codec": stream.get("codec_name", "unknown"),
                }
            )

    return audio_tracks, sub_tracks


def _base_state(task_id, filename, audio_tracks, sub_tracks, input_path=""):
    return {
        "task_id": task_id,
        "filename": filename,
        "input_path": input_path,
        "audio_tracks": audio_tracks,
        "sub_tracks": sub_tracks,
        "external_audio": [],
        "external_sub": [],
        "remove_audio": [],
        "remove_sub": [],
        "extract_audio": [],
        "extract_sub": [],
        "merge_audio": [],
        "merge_sub": [],
        "translate_sub": [],
        "keep_audio": [],
        "keep_sub": [],
        "audio_order": [],
        "sub_order": [],
        "default_audio": None,
        "default_sub": None,
        "intro_subtitle": False,
        "intro_text_available": False,
        "external_dir": "",
        "merge_intake": False,
        "remove_original_audio": False,
        "awaiting_input": None,
        "preview_path": "",
        "preview_stale": True,
        "busy": False,
        "video_merge": False,
        "video_duration": 0.0,
        "completed": False,
        "auto_sync": True,
        "preview_sync": True,
        "final_sample": True,
        "save_settings": True,
        "output_name": "",
        "task_mode": "#Leech",
        "file_size": 0,
    }


async def _refresh_external_tracks(state, input_path):
    saved_audio = {
        (item.get("path"), item.get("stream_index")): {
            "language": item.get("language", "und"),
            "title": item.get("title", ""),
            "delay_ms": item.get("delay_ms", 0),
            "codec": item.get("codec"),
            "channels": item.get("channels"),
            "tag": item.get("tag"),
            "size_mb": item.get("size_mb"),
        }
        for item in state.get("external_audio", [])
    }
    dir_path = ospath.dirname(input_path)
    input_name = ospath.basename(input_path)
    external_audio = []
    external_sub = []
    dirs = [dir_path]
    external_dir = state.get("external_dir")
    if external_dir and external_dir not in dirs:
        dirs.append(external_dir)

    for scan_dir in dirs:
        if not await aiopath.isdir(scan_dir):
            continue
        for name in sorted(await listdir(scan_dir)):
            if name == input_name or name.startswith("vt_") or name.startswith("intro_"):
                continue
            path = ospath.join(scan_dir, name)
            if not await aiopath.isfile(path):
                continue
            ext = ospath.splitext(name)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                external_audio.append(_external_audio_item(len(external_audio), name, path))
            elif ext in VIDEO_EXTENSIONS:
                external_audio.extend(await _external_video_audio_items(path, len(external_audio)))
            elif ext in SUBTITLE_EXTENSIONS:
                external_sub.append(
                    {"index": len(external_sub), "name": name, "path": path}
                )

    from ..ext_utils.media_utils import get_exact_duration

    for item in external_audio:
        saved = saved_audio.get((item.get("path"), item.get("stream_index")))
        if saved:
            item.update(saved)
        if "duration" not in item or not item.get("duration"):
            item["duration"] = await get_exact_duration(item["path"])
        if "codec" not in item or not item.get("codec") or item.get("codec") == "AUDIO":
            details = await _probe_external_audio_details(item["path"], item.get("stream_index", 0))
            if not item.get("language") or item.get("language") == "und":
                item["language"] = details["language"]
            item["codec"] = details["codec"]
            item["channels"] = details["channels"]
            item["tag"] = format_lang_tag(item.get("language") or details["language"])
            item["size_mb"] = details["size_mb"]

    if state.get("auto_sync", True):
        video_dur = float(state.get("video_duration", 0) or 0)
        for item in external_audio:
            track_dur = float(item.get("duration", 0) or 0)
            if video_dur and track_dur:
                item["delay_ms"] = int(round((video_dur - track_dur) * 1000))

    state["external_audio"] = external_audio
    state["external_sub"] = external_sub
    state["merge_audio"] = [
        idx for idx in state.get("merge_audio", []) if idx < len(external_audio)
    ]
    if not state["merge_audio"] and external_audio:
        state["merge_audio"] = [item["index"] for item in external_audio]

    state["merge_sub"] = [
        idx for idx in state.get("merge_sub", []) if idx < len(external_sub)
    ]
    if not state["merge_sub"] and external_sub:
        state["merge_sub"] = [item["index"] for item in external_sub]


async def _ensure_external_dir(state):
    if not state.get("external_dir"):
        state["external_dir"] = ospath.join(DOWNLOAD_DIR, f"vt_external_{state['task_id']}")
    await makedirs(state["external_dir"], exist_ok=True)
    return state["external_dir"]


def _select_all_external_tracks(state):
    state["merge_audio"] = [item["index"] for item in state.get("external_audio", [])]
    state["merge_sub"] = [item["index"] for item in state.get("external_sub", [])]


async def start_merge_track_intake(vt_msg, state):
    await _ensure_external_dir(state)
    state["merge_intake"] = True


def _external_audio_item(index, name, path, stream_index=0, language="und", title="", duration=0.0):
    return {
        "index": index,
        "name": name,
        "path": path,
        "stream_index": stream_index,
        "language": language or "und",
        "title": title or ospath.splitext(name)[0],
        "delay_ms": 0,
        "duration": duration,
    }


async def _external_video_audio_items(video_path, start_index):
    from ..ext_utils.media_utils import get_exact_duration
    video_dur = await get_exact_duration(video_path)
    result = await cmd_exec(
        [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index:stream_tags=language,title",
            "-of", "json", video_path,
        ]
    )
    items = []
    if result[2] == 0:
        with suppress(Exception):
            for audio_index, stream in enumerate(json.loads(result[0] or "{}").get("streams", [])):
                tags = stream.get("tags") or {}
                name = f"{ospath.basename(video_path)} - audio {audio_index + 1}"
                items.append(
                    _external_audio_item(
                        start_index + len(items),
                        name,
                        video_path,
                        audio_index,
                        tags.get("language", "und"),
                        tags.get("title", name),
                        duration=video_dur,
                    )
                )
    return items or [
        _external_audio_item(
            start_index,
            f"Audio from {ospath.basename(video_path)}",
            video_path,
            duration=video_dur,
        )
    ]


def _message_merge_media(message):
    return (
        message.document
        or message.audio
        or message.video
        or message.animation
        or None
    )


def _find_merge_intake_session(message):
    user = message.from_user or message.sender_chat
    if not user:
        return None
    for item in _active_vt_sessions.values():
        listener = item.get("listener")
        state = item.get("state")
        if (
            listener
            and state
            and state.get("merge_intake")
            and getattr(listener, "user_id", None) == user.id
        ):
            return item
    return None


def _find_merge_text_session(message):
    user = message.from_user or message.sender_chat
    if not user:
        return None
    for item in _active_vt_sessions.values():
        listener = item.get("listener")
        state = item.get("state")
        if (
            listener
            and state
            and state.get("awaiting_input")
            and getattr(listener, "user_id", None) == user.id
        ):
            return item
    return None


async def active_merge_track_filter(_, __, message):
    return bool(_message_merge_media(message) and _find_merge_intake_session(message))


async def active_merge_text_filter(_, __, message):
    return bool(message.text and not message.text.startswith("/") and _find_merge_text_session(message))


async def video_tools_text_collector(_, message):
    session = _find_merge_text_session(message)
    if not session:
        return
    state = session["state"]
    field, index = state.pop("awaiting_input")
    value = (message.text or "").strip()
    vt_msg = getattr(session.get("listener"), "_vt_msg", None)

    if field == "output_name":
        if value:
            if not value.lower().endswith(".mkv"):
                value += ".mkv"
            state["output_name"] = value
            state["custom_output_name"] = True
        if vt_msg:
            from ...modules.video_tool_ui import render_muxing_configuration
            await render_muxing_configuration(vt_msg, state)
        return

    item = next(
        (entry for entry in state.get("external_audio", []) if entry["index"] == index),
        None,
    )
    if not item:
        await send_message(message, "Merge track no longer exists.")
        return

    if field == "delay_ms":
        if not re.fullmatch(r"[+-]?\d+", value):
            state["awaiting_input"] = (field, index)
            await send_message(message, "Audio delay must be a signed integer in milliseconds, for example <code>-250</code> or <code>1200</code>.")
            return
        value = max(-3_600_000, min(3_600_000, int(value)))
    elif field == "language":
        value = _normalize_language(value)
        item["tag"] = format_lang_tag(value)
    else:
        value = value[:80] or item.get("title") or "External Audio"
    item[field] = value
    state["preview_stale"] = True

    if vt_msg:
        from ...modules.video_tool_ui import render_muxing_configuration
        await render_muxing_configuration(vt_msg, state)


def finish_merge_track_intake(state, event):
    if not state.get("merge_audio") and not state.get("merge_sub"):
        raise ValueError("Send and select at least one audio or subtitle track first.")
    if state.get("remove_original_audio") and not state.get("merge_audio"):
        raise ValueError("Select at least one new audio track before removing current audio.")
    state["merge_intake"] = False
    state["completed"] = True
    event.set()


async def generate_merge_preview(state):
    session = _active_vt_sessions.get(state.get("task_id"))
    listener = session.get("listener") if session else None
    input_path = state.get("input_path")
    if not listener or not input_path or not await aiopath.isfile(input_path):
        raise ValueError("Preview becomes available after the main video is downloaded.")
    if state.get("busy"):
        raise ValueError("A probe, preview, or mux operation is already running.")

    state["busy"] = True
    preview_path = ospath.join(
        ospath.dirname(input_path), f"vt_preview_{state['task_id']}.mkv"
    )
    try:
        from ..ext_utils.media_utils import get_media_info

        duration = float((await get_media_info(input_path))[0] or 0)
        preview_duration = min(60, max(5, int(duration or 60)))
        start = max(0.0, (duration - preview_duration) / 2) if duration else 0.0
        if await aiopath.exists(preview_path):
            await remove(preview_path)
        legacy_preview = ospath.splitext(preview_path)[0] + ".mp4"
        if await aiopath.exists(legacy_preview):
            await remove(legacy_preview)

        selected = [
            item
            for item in state.get("external_audio", [])
            if item["index"] in state.get("merge_audio", [])
        ]
        if not selected and state.get("external_audio"):
            selected = state.get("external_audio", [])

        if not selected:
            raise ValueError("Send and select a new audio track before creating a preview.")

        base_cmd = [
            BinConfig.FFMPEG_NAME, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", input_path,
        ]

        for item in selected:
            delay_sec = int(item.get("delay_ms", 0)) / 1000.0
            audio_seek = max(0.0, start - delay_sec)
            base_cmd.extend(["-ss", f"{audio_seek:.3f}", "-i", item["path"]])

        mapping = ["-map", "0:v:0"]
        for input_index, item in enumerate(selected, start=1):
            mapping.extend(["-map", f"{input_index}:a:{int(item.get('stream_index', 0))}?"])
        if not state.get("remove_original_audio"):
            mapping.extend(["-map", "0:a?"])

        metadata = []
        for output_index, item in enumerate(selected):
            metadata.extend(
                [
                    f"-metadata:s:a:{output_index}",
                    f"language={_normalize_language(item.get('language', 'und'))}",
                    f"-metadata:s:a:{output_index}",
                    f"title={str(item.get('title') or item.get('name') or 'External Audio')[:80]}",
                ]
            )
        if selected:
            metadata.extend(["-disposition:a", "0", "-disposition:a:0", "default"])

        # Strictly stream copy only (zero video re-encoding, zero audio re-encoding)
        trim_cmd = base_cmd + mapping + metadata + [
            "-t", str(preview_duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            preview_path,
        ]

        result = await cmd_exec(trim_cmd)
        if result[2] != 0 or not await aiopath.isfile(preview_path) or (await aiopath.getsize(preview_path) == 0):
            if await aiopath.exists(preview_path):
                await remove(preview_path)
            fallback_cmd = base_cmd + mapping + metadata + [
                "-t", str(preview_duration),
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                preview_path,
            ]
            result = await cmd_exec(fallback_cmd)

        if result[2] != 0 or not await aiopath.isfile(preview_path) or (await aiopath.getsize(preview_path) == 0):
            raise RuntimeError(f"FFmpeg lossless preview trim failed: {(result[1] or 'Unknown error')[-500:]}")

        previous = state.get("preview_message")
        if previous:
            with suppress(Exception):
                await previous.delete()

        caption = (
            f"🎬 <b>Sync Preview ({preview_duration}s • Stream-Copy)</b>\n"
            f"<code>{ospath.basename(input_path)}</code>\n"
            "<i>Zero-reencode lossless stream cut. Check audio sync.</i>"
        )
        preview_msg = await send_message(listener.message, caption, file=preview_path)
        state["preview_message"] = preview_msg
        state["preview_path"] = preview_path
        state["preview_stale"] = False
        return preview_path
    finally:
        state["busy"] = False


async def generate_final_sample(listener, output_path):
    """Generates a 45s stream-copy sample from the middle of the final output MKV and sends it to Telegram."""
    if not await aiopath.isfile(output_path):
        return
    sample_path = ospath.join(
        ospath.dirname(output_path), f"sample_{ospath.basename(output_path)}"
    )
    try:
        from ..ext_utils.media_utils import get_media_info
        duration = float((await get_media_info(output_path))[0] or 0)
        sample_dur = min(45, max(10, int(duration or 45)))
        start = max(0.0, (duration - sample_dur) / 2) if duration else 0.0

        if await aiopath.exists(sample_path):
            await remove(sample_path)

        cmd = [
            BinConfig.FFMPEG_NAME, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", output_path,
            "-t", str(sample_dur),
            "-map", "0",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            sample_path,
        ]
        result = await cmd_exec(cmd)
        if result[2] == 0 and await aiopath.isfile(sample_path) and (await aiopath.getsize(sample_path) > 0):
            caption = (
                f"🎬 <b>Final Sample ({sample_dur}s • Lossless Cut)</b>\n"
                f"<code>{ospath.basename(output_path)}</code>"
            )
            await send_message(listener.message, caption, file=sample_path)
    except Exception as e:
        LOGGER.warning(f"Video Tool: Failed generating final sample: {e}")
    finally:
        if await aiopath.exists(sample_path):
            with suppress(Exception):
                await remove(sample_path)


async def video_tools_media_collector(client, message):
    session = _find_merge_intake_session(message)
    if not session:
        return
    state = session["state"]
    media = _message_merge_media(message)
    if not media:
        return
    filename = getattr(media, "file_name", "") or f"merge_track_{int(time())}"
    ext = ospath.splitext(filename)[1].lower()
    if ext not in AUDIO_EXTENSIONS and ext not in SUBTITLE_EXTENSIONS and ext not in VIDEO_EXTENSIONS:
        await send_message(message, "Video Tools: unsupported merge-track file type.")
        return
    external_dir = await _ensure_external_dir(state)
    target = ospath.join(external_dir, filename)
    downloaded = await message.download(file_name=target)
    if not downloaded:
        await send_message(message, "Video Tools: failed to download merge-track file.")
        return
    from ..ext_utils.media_utils import get_exact_duration

    if ext in AUDIO_EXTENSIONS:
        audio_dur = await get_exact_duration(downloaded)
        state["external_audio"].append(
            _external_audio_item(
                len(state["external_audio"]),
                ospath.basename(downloaded),
                downloaded,
                duration=audio_dur,
            )
        )
        state["merge_audio"].append(len(state["external_audio"]) - 1)
        first_audio_index = len(state["external_audio"]) - 1
    elif ext in VIDEO_EXTENSIONS:
        items = await _external_video_audio_items(downloaded, len(state["external_audio"]))
        state["external_audio"].extend(items)
        state["merge_audio"].extend(item["index"] for item in items)
        first_audio_index = items[0]["index"] if items else None
    elif ext in SUBTITLE_EXTENSIONS:
        state["external_sub"].append(
            {"index": len(state["external_sub"]), "name": ospath.basename(downloaded), "path": downloaded}
        )
        state["merge_sub"].append(len(state["external_sub"]) - 1)
        first_audio_index = None
    state["preview_stale"] = True
    await _refresh_external_tracks(state, state.get("input_path") or downloaded)
    vt_msg = getattr(session.get("listener"), "_vt_msg", None)
    if vt_msg:
        from ...modules.video_tool_ui import render_muxing_configuration
        await render_muxing_configuration(vt_msg, state)


def _target_lang(listener):
    return (
        listener.user_dict.get("SUBTITLE_TRANSLATE_TARGET")
        or Config.SUBTITLE_TRANSLATE_TARGET
        or "en"
    ).strip()


def _srt_ts(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02},000"


def _ass_color(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("&H"):
        return value
    if value.startswith("#") and len(value) == 7:
        r, g, b = value[1:3], value[3:5], value[5:7]
        return f"&H00{b}{g}{r}"
    return value


def _colorize_ass_letters(text, palette):
    colors = [_ass_color(item) for item in re.split(r"[\s,|]+", str(palette or "")) if item.strip()]
    colors = [item for item in colors if item]
    if not colors:
        return text
    out = []
    index = 0
    for char in text:
        if char in {"\\", "{", "}"}:
            out.append(char)
            continue
        if char.strip():
            out.append(f"{{\\c{colors[index % len(colors)]}}}{char}")
            index += 1
        else:
            out.append(char)
    return "".join(out)


async def _create_intro_subtitle(listener, dir_path, video_path=None):
    text = (
        listener.user_dict.get("INTRO_SUBTITLE_TEXT")
        or Config.INTRO_SUBTITLE_TEXT
        or ""
    ).strip()
    if not text:
        return None
    if not dir_path:
        LOGGER.warning("Intro subtitle skipped: task directory is unavailable")
        return None
    try:
        await makedirs(dir_path, exist_ok=True)
    except OSError as error:
        LOGGER.warning(f"Intro subtitle skipped; cannot create {dir_path}: {error}")
        return None
    out_path = ospath.join(dir_path, f"intro_{listener.mid}.ass")
    duration = int(
        listener.user_dict.get("INTRO_SUBTITLE_DURATION")
        or getattr(Config, "INTRO_SUBTITLE_DURATION", 10)
        or 10
    )
    fade_ms = int(getattr(Config, "INTRO_SUBTITLE_FADE_MS", 400) or 400)
    configured_ranges = (
        listener.user_dict.get("INTRO_SUBTITLE_RANGES")
        or getattr(Config, "INTRO_SUBTITLE_RANGES", "")
    )
    if configured_ranges and (
        "00:01:20 - 00:01:25" in configured_ranges
        or "00:00:00 - 00:00:05" in configured_ranges
        or "00:00:00 - 00:00:60" in configured_ranges
    ):
        configured_ranges = ""
    ranges = _parse_intro_ranges(configured_ranges)
    # Range covers full video length with 5 min gap and 10 sec show:
    if not configured_ranges and video_path:
        try:
            from ..ext_utils.media_utils import get_exact_duration

            video_duration = await get_exact_duration(video_path)
            if video_duration <= 0:
                from ..ext_utils.media_utils import get_media_info

                video_duration = float((await get_media_info(video_path))[0] or 0)
            gap = 5 * 60
            ranges = [
                (start, min(start + duration, video_duration))
                for start in range(0, int(video_duration), gap)
            ]
        except Exception as error:
            LOGGER.warning(f"Intro subtitle duration probe failed: {error}")
    font = str(getattr(Config, "INTRO_SUBTITLE_FONT", "Arial") or "Arial")
    size = int(getattr(Config, "INTRO_SUBTITLE_FONT_SIZE", 36) or 36)
    color = str(getattr(Config, "INTRO_SUBTITLE_COLOR", "&H00FFFFFF") or "&H00FFFFFF")
    outline = str(getattr(Config, "INTRO_SUBTITLE_OUTLINE_COLOR", "&H00000000") or "&H00000000")
    palette = str(getattr(Config, "INTRO_SUBTITLE_COLOR_PALETTE", "") or "")
    safe_text = text.replace("\n", r"\N").replace("{", "(").replace("}", ")")
    safe_text = _colorize_ass_letters(safe_text, palette)
    safe_text = r"{\b1}" + safe_text
    if not ranges:
        ranges = [(0, duration)]
    dialogues = []
    for start, end in ranges:
        if end <= start:
            continue
        dialogues.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Intro,,0,0,0,,{{\\fad({fade_ms},{fade_ms})}}{safe_text}"
        )
    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Intro,{font},{size},{color},{outline},1,3,1,2,40,40,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{chr(10).join(dialogues)}
"""
    async with aiopen(out_path, "w", encoding="utf-8") as f:
        await f.write(ass)
    return out_path


def _ass_time(seconds):
    seconds = max(0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02}:{s:05.2f}"


def _parse_intro_ranges(value):
    text = str(value or "")
    ranges = []
    pattern = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*-\s*(\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?)"
    )
    for start, end in pattern.findall(text):
        ranges.append((_clock_to_seconds(start), _clock_to_seconds(end)))
    return ranges


def _clock_to_seconds(value):
    value = str(value).replace(",", ".")
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _translate_srt_text(text, target):
    url = str(getattr(Config, "LIBRE_TRANSLATE_API_URL", "") or "").strip()
    key = str(getattr(Config, "LIBRE_TRANSLATE_API_KEY", "") or "").strip()
    if not url:
        raise RuntimeError("LIBRE_TRANSLATE_API_URL is not configured")

    import requests

    lines = text.splitlines()
    output = list(lines)
    indexes = []
    payload_lines = []
    for pos, line in enumerate(lines):
        stripped = line.strip()
        if (
            not stripped
            or stripped.isdigit()
            or "-->" in stripped
            or stripped.startswith(("{\\", "["))
        ):
            continue
        indexes.append(pos)
        payload_lines.append(line)

    if not payload_lines:
        return text if text.endswith("\n") else f"{text}\n"

    endpoint = url if url.rstrip("/").endswith("/translate") else f"{url.rstrip('/')}/translate"
    for start in range(0, len(payload_lines), 25):
        chunk = payload_lines[start : start + 25]
        joined = "\n".join(chunk)
        payload = {
            "q": joined,
            "source": "auto",
            "target": target,
            "format": "text",
        }
        if key:
            payload["api_key"] = key
        resp = requests.post(endpoint, json=payload, timeout=45)
        if resp.status_code != 200:
            body = resp.text[:200]
            if "api key" in body.lower():
                raise RuntimeError(
                    "LibreTranslate requires an API key. Set LIBRE_TRANSLATE_API_KEY "
                    "or use a LibreTranslate API URL that does not require a key."
                )
            raise RuntimeError(f"LibreTranslate API error {resp.status_code}: {body}")
        translated_text = resp.json().get("translatedText", "")
        translated_lines = translated_text.splitlines()
        if len(translated_lines) != len(chunk):
            translated_lines = [
                _translate_one_line(endpoint, line, target, key) for line in chunk
            ]
        for offset, translated in enumerate(translated_lines):
            output[indexes[start + offset]] = translated or chunk[offset]
    return "\n".join(output) + "\n"


def _translate_one_line(endpoint, line, target, key):
    import requests

    payload = {"q": line, "source": "auto", "target": target, "format": "text"}
    if key:
        payload["api_key"] = key
    resp = requests.post(endpoint, json=payload, timeout=30)
    if resp.status_code != 200:
        return line
    return resp.json().get("translatedText") or line


async def _translate_srt_file(src_path, target, task_id):
    dst_path = ospath.join(
        ospath.dirname(src_path),
        f"translated_{target}_{task_id}_{ospath.basename(src_path)}",
    )
    async with aiopen(src_path, encoding="utf-8", errors="ignore") as f:
        text = await f.read()
    translated = await sync_to_async(_translate_srt_text, text, target)
    async with aiopen(dst_path, "w", encoding="utf-8") as f:
        await f.write(translated)
    return dst_path


async def process_video_tool(listener, up_path):
    """
    Main entry point for video tools. Called from task_listener after download.
    Shows UI, waits for user input, then runs FFmpeg muxing.
    Returns the (possibly modified) up_path.
    """
    if getattr(listener, "_vt_processed", False):
        state = getattr(listener, "_vt_state", None)
        listener._vt_processed = False
        listener._vt_state = None
        if state:
            if await aiopath.isdir(up_path):
                return await _process_vt_directory(listener, up_path, state)
            new_path = await _execute_vt_pipeline(listener, up_path, state)
            return new_path if new_path else up_path
        return up_path

    if await aiopath.isdir(up_path):
        return await _process_vt_directory(listener, up_path)

    # Only work on single files
    if not await aiopath.isfile(up_path):
        LOGGER.info("Video Tool: up_path is a directory, skipping.")
        await send_message(
            listener.message,
            "⚠️ <b>Video Tool:</b> Directories are not supported. Proceeding normally...",
        )
        return up_path

    ext = ospath.splitext(up_path)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        await send_message(
            listener.message,
            f"⚠️ <b>Video Tool:</b> <code>{ext}</code> is not a supported video format. Proceeding normally...",
        )
        return up_path

    # Probe the file
    audio_tracks, sub_tracks = await probe_streams(up_path)
    from ..ext_utils.media_utils import get_exact_duration

    video_dur = await get_exact_duration(up_path)
    task_id = str(listener.mid)
    state = _base_state(
        task_id, ospath.basename(up_path), audio_tracks, sub_tracks, up_path
    )
    state["video_duration"] = video_dur
    with suppress(Exception):
        state["file_size"] = await aiopath.getsize(up_path)

    task_mode = "#Leech"
    if getattr(listener, "is_gdrive", False):
        task_mode = "#GDrive"
    elif getattr(listener, "up_dest", None):
        dest = str(listener.up_dest)
        task_mode = f"#{dest}" if not dest.startswith("#") else dest
    elif not getattr(listener, "is_leech", True):
        task_mode = "#Mirror"
    state["task_mode"] = task_mode

    user_dict = getattr(listener, "user_dict", {}) or {}
    if "MUX_AUTO_SYNC" in user_dict:
        state["auto_sync"] = user_dict["MUX_AUTO_SYNC"]
    if "MUX_PREVIEW_SYNC" in user_dict:
        state["preview_sync"] = user_dict["MUX_PREVIEW_SYNC"]
    if "MUX_FINAL_SAMPLE" in user_dict:
        state["final_sample"] = user_dict["MUX_FINAL_SAMPLE"]
    if "MUX_REMOVE_ORIGINAL" in user_dict:
        state["remove_original_audio"] = user_dict["MUX_REMOVE_ORIGINAL"]

    await _refresh_external_tracks(state, up_path)

    if (
        not audio_tracks
        and not sub_tracks
        and not state["external_audio"]
        and not state["external_sub"]
    ):
        await send_message(
            listener.message,
            "⚠️ <b>Video Tool:</b> No audio or subtitle streams found. Proceeding normally...",
        )
        return up_path

    # Store the state in the listener for the callback handler
    listener._vt_state = state

    # Create an event that the callback handler will set
    done_event = Event()
    _active_vt_sessions[task_id] = {
        "event": done_event,
        "state": state,
        "listener": listener,
    }

    try:
        # Render the UI
        from ...modules.video_tool_ui import render_merge_intake, render_video_tools_main

        tag = getattr(listener, "tag", None) or (listener.message.from_user.mention if listener.message.from_user else "")
        vt_msg = await send_message(
            listener.message,
            f"{tag} ⚙️ <b>Generating Video Tools UI...</b>",
        )
        listener._vt_msg = vt_msg
        if getattr(listener, "manual_video_merge", False):
            await start_merge_track_intake(vt_msg, state)
            await render_merge_intake(vt_msg, state)
        else:
            await render_video_tools_main(vt_msg, state)

        # Wait for user to click Done/Close or timeout
        try:
            await asyncio.wait_for(done_event.wait(), timeout=UI_TIMEOUT)
        except Exception:
            if state.get("merge_intake"):
                LOGGER.info(
                    "Merge Tracks timed out for task %s; preserving original video",
                    task_id,
                )
                state["cancelled"] = True
            else:
                LOGGER.info(f"Video Tool timeout for task {task_id}, proceeding...")
                state["completed"] = True

        # If user cancelled (close), return original path
        if state.get("cancelled", False):
            return up_path

        # Execute the FFmpeg pipeline
        new_path = await _execute_vt_pipeline(listener, up_path, state)
        if new_path and state.get("final_sample", True):
            await generate_final_sample(listener, new_path)
        return new_path if new_path else up_path

    except Exception as e:
        LOGGER.error(f"Video Tool error: {e}")
        await send_message(
            listener.message,
            f"⚠️ <b>Video Tool Error:</b> <code>{e}</code>\nProceeding normally...",
        )
        return up_path
    finally:
        preview_path = state.get("preview_path")
        if preview_path and await aiopath.isfile(preview_path):
            with suppress(Exception):
                await remove(preview_path)
        external_dir = state.get("external_dir")
        if external_dir and await aiopath.isdir(external_dir):
            await rmtree(external_dir, ignore_errors=True)
        _active_vt_sessions.pop(task_id, None)
        listener._vt_state = None
        listener._vt_msg = None


async def _video_files(root):
    if await aiopath.isfile(root):
        return [root] if ospath.splitext(root)[1].lower() in VIDEO_EXTENSIONS else []
    files = []
    for dirpath, _, names in await sync_to_async(walk, root):
        for name in names:
            path = ospath.join(dirpath, name)
            if ospath.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                files.append(path)
    return sorted(files, key=lambda p: ospath.basename(p).lower())


def _video_merge_requested(listener, up_path, state=None):
    if state and state.get("video_merge"):
        return True
    folder_name = getattr(listener, "folder_name", "") or ""
    base = ospath.basename(str(up_path).rstrip("/"))
    return folder_name.startswith("/vt_video_merge_") or base.startswith("vt_video_merge_")


async def _video_merge_signature(path):
    result = await cmd_exec(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            path,
        ]
    )
    if result[2] != 0:
        return None
    try:
        data = json.loads(result[0] or "{}")
    except Exception:
        return None
    signature = []
    for stream in data.get("streams", []):
        if stream.get("codec_type") in {"video", "audio", "subtitle"}:
            signature.append(
                (
                    stream.get("codec_type"),
                    stream.get("codec_name"),
                    stream.get("width"),
                    stream.get("height"),
                    stream.get("sample_rate"),
                    stream.get("channels"),
                )
            )
    return signature


async def _validate_video_merge(videos):
    first = None
    mismatches = []
    for idx, path in enumerate(videos, start=1):
        sig = await _video_merge_signature(path)
        if not sig:
            mismatches.append(f"{idx}. {ospath.basename(path)} - unable to probe")
            continue
        if first is None:
            first = sig
        elif sig != first:
            mismatches.append(f"{idx}. {ospath.basename(path)} - stream layout mismatch")
    return mismatches


async def _send_clean_planner(listener, lines):
    await send_message(listener.message, "\n".join(lines))


async def _merge_video_directory(listener, root, videos):
    if len(videos) < 2:
        await send_message(listener.message, "Video + Video needs at least 2 video files.")
        return root

    planner = [
        "<b>Video Processing Plan</b>",
        "",
        "• Video + Video Merge: Enabled",
        f"• Total Videos: {len(videos)}",
        "• Mode: Concat demuxer, stream copy",
        "• Extra stream tools: Skipped for Video + Video",
        "",
        "<b>Order</b>",
    ]
    planner.extend(f"{idx}. <code>{ospath.basename(path)}</code>" for idx, path in enumerate(videos, start=1))
    await _send_clean_planner(listener, planner)

    mismatches = await _validate_video_merge(videos)
    if mismatches:
        await _send_clean_planner(
            listener,
            [
                "<b>Video + Video Merge cancelled</b>",
                "",
                "Inputs are not stream-copy compatible:",
                *mismatches[:10],
            ],
        )
        return root

    list_path = ospath.join(root, f"video_merge_{listener.mid}.ffconcat")
    source_name = get_source_container_name(
        getattr(listener, "merge_source_name", "")
    )
    output_name = f"{source_name or 'Video_Merge'}.mkv"
    output = ospath.join(root, output_name)
    if output in videos:
        output = ospath.join(root, f"{source_name or 'Video_Merge'} Merged.mkv")
    if await aiopath.exists(output):
        await remove(output)

    use_mkvmerge = all(p.lower().endswith(".mkv") for p in videos)
    if use_mkvmerge:
        mkv_cmd = [
            BinConfig.MKVMERGE_NAME,
            "-o", output,
            "--generate-chapters", "when-appending",
            "--generate-chapters-name-template", "Episode <NUM:2> (<START:%H:%M:%S>)",
            videos[0],
        ]
        for v in videos[1:]:
            mkv_cmd.extend(["+", v])
        LOGGER.info(
            "Video + Video merge attempting fast mkvmerge for %d videos: %s",
            len(videos),
            ospath.basename(output),
        )
        success, out_msg = await execute_mkvmerge_with_fallback(
            mkv_cmd, videos[0], output, cmd_exec
        )
        if success and await aiopath.exists(output):
            LOGGER.info("Video + Video mkvmerge succeeded: %s", output)
            for video in videos:
                if video != output and await aiopath.exists(video):
                    await remove(video)
            await _send_clean_planner(
                listener,
                [
                    "<b>Video Processing Plan</b>",
                    "",
                    "• Video + Video Merge: Enabled",
                    "• Engine: MKVMerge (lossless stream concat)",
                    "",
                    "Processing Finished Successfully",
                ],
            )
            return output
        LOGGER.warning(
            "Video + Video mkvmerge failed, falling back to FFmpeg: %s",
            (out_msg or "")[-500:],
        )
        if await aiopath.exists(output):
            await remove(output)

    async with aiopen(list_path, "w", encoding="utf-8") as f:
        await f.write("ffconcat version 1.0\n")
        for path in videos:
            safe = path.replace("\\", "/").replace("'", "'\\''")
            await f.write(f"file '{safe}'\n")

    cmd = []
    if shutil.which("taskset"):
        cmd.extend(["taskset", "-c", get_ffmpeg_cores()])
    cmd.extend([
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-c",
        "copy",
        "-threads",
        str(get_ffmpeg_threads()),
        "-max_muxing_queue_size",
        "9999",
        output,
    ])

    from ... import task_dict, task_dict_lock
    from ..ext_utils.files_utils import get_path_size
    from ..ext_utils.media_utils import FFMpeg, get_media_info
    from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus

    ffmpeg = FFMpeg(listener)
    ffmpeg.clear()
    total_duration = 0
    for video in videos:
        with suppress(Exception):
            total_duration += (await get_media_info(video))[0]
    ffmpeg._total_time = total_duration
    listener.subname = ospath.basename(output)
    listener.subsize = sum([await get_path_size(path) for path in videos])
    listener.progress = True
    async with task_dict_lock:
        task_dict[listener.mid] = FFmpegStatus(
            listener,
            ffmpeg,
            getattr(listener, "_process_gid", str(listener.mid)),
            "Video Tools",
        )

    try:
        async with ffmpeg_task(listener, "Video + Video merge"):
            listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
            stderr_task = create_task(listener.subproc.stderr.read())
            await ffmpeg._ffmpeg_progress()
            stderr = await stderr_task
            await listener.subproc.wait()
        if listener.subproc.returncode != 0 or not await aiopath.exists(output):
            err = stderr.decode(errors="ignore").strip() if stderr else "Unknown error"
            LOGGER.error(f"Video + Video merge failed: {err}")
            await send_message(listener.message, f"Video + Video merge failed: <code>{err[:300]}</code>")
            return root
        for video in videos:
            if video != output and await aiopath.exists(video):
                await remove(video)
        await _send_clean_planner(
            listener,
            [
                "<b>Video Processing Plan</b>",
                "",
                "• Video + Video Merge: Enabled",
                "• FFmpeg Queue: Completed",
                "",
                "Processing Finished Successfully",
            ],
        )
        return output
    finally:
        if await aiopath.exists(list_path):
            await remove(list_path)


def _clone_state_for_path(source, filename, audio_tracks, sub_tracks, input_path=""):
    task_id = source["task_id"]
    state = _base_state(task_id, filename, audio_tracks, sub_tracks, input_path)
    audio_len = len(audio_tracks)
    sub_len = len(sub_tracks)
    for key in ("remove_audio", "extract_audio"):
        state[key] = [idx for idx in source.get(key, []) if idx < audio_len]
    for key in ("remove_sub", "extract_sub", "translate_sub"):
        state[key] = [idx for idx in source.get(key, []) if idx < sub_len]
    state["merge_audio"] = list(source.get("merge_audio", []))
    state["merge_sub"] = list(source.get("merge_sub", []))
    state["remove_original_audio"] = bool(source.get("remove_original_audio"))
    state["default_audio"] = (
        source.get("default_audio")
        if source.get("default_audio") is not None and source.get("default_audio") < audio_len
        else None
    )
    state["default_sub"] = (
        source.get("default_sub")
        if source.get("default_sub") is not None and source.get("default_sub") < sub_len
        else None
    )
    state["keep_audio"] = [idx for idx in source.get("keep_audio", []) if idx < audio_len]
    state["keep_sub"] = [idx for idx in source.get("keep_sub", []) if idx < sub_len]
    state["audio_order"] = [idx for idx in source.get("audio_order", []) if idx < audio_len]
    state["sub_order"] = [idx for idx in source.get("sub_order", []) if idx < sub_len]
    state["intro_subtitle"] = bool(source.get("intro_subtitle"))
    state["intro_text_available"] = bool(source.get("intro_text_available"))
    state["external_dir"] = source.get("external_dir", "")
    state["video_duration"] = float(source.get("video_duration", 0.0) or 0.0)
    return state


async def _process_vt_directory(listener, up_path, pre_state=None):
    videos = await _video_files(up_path)
    if not videos:
        await send_message(
            listener.message,
            "<b>Video Tool:</b> No supported videos found after download/extract.",
        )
        return up_path

    if (
        _video_merge_requested(listener, up_path, pre_state)
        or (getattr(listener, "manual_video_merge", False) and len(videos) >= 2)
    ):
        return await _merge_video_directory(listener, up_path, videos)

    task_id = str(listener.mid)
    state = pre_state
    if state is None:
        first_video = None
        audio_tracks, sub_tracks = [], []
        for video in videos:
            audio_tracks, sub_tracks = await probe_streams(video)
            if audio_tracks or sub_tracks:
                first_video = video
                break
        if first_video is None:
            first_video = videos[0]
        state = _base_state(
            task_id, ospath.basename(first_video), audio_tracks, sub_tracks
        )
        await _refresh_external_tracks(state, first_video)

        if (
            not audio_tracks
            and not sub_tracks
            and not state["external_audio"]
            and not state["external_sub"]
        ):
            await send_message(
                listener.message,
                "<b>Video Tool:</b> No usable streams found in extracted videos.",
            )
            return up_path

        listener._vt_state = state
        done_event = Event()
        _active_vt_sessions[task_id] = {
            "event": done_event,
            "state": state,
            "listener": listener,
        }
        try:
            from ...modules.video_tool_ui import render_video_tools_main

            vt_msg = await send_message(
                listener.message,
                f"<b>Video Tools:</b> directory mode detected. Configure once; selection applies to {len(videos)} video(s).",
            )
            listener._vt_msg = vt_msg
            await render_video_tools_main(vt_msg, state)
            try:
                await asyncio.wait_for(done_event.wait(), timeout=UI_TIMEOUT)
            except Exception:
                LOGGER.info(f"Video Tool directory timeout for task {task_id}, proceeding...")
                state["completed"] = True
            if state.get("cancelled", False):
                return up_path
        finally:
            _active_vt_sessions.pop(task_id, None)
            listener._vt_state = None
            listener._vt_msg = None

    await send_message(
        listener.message,
        f"Video Tools: processing {len(videos)} video(s) after download/extract...",
    )
    for video in videos:
        audio_tracks, sub_tracks = await probe_streams(video)
        work_state = _clone_state_for_path(
            state, ospath.basename(video), audio_tracks, sub_tracks, video
        )
        await _refresh_external_tracks(work_state, video)
        await _execute_vt_pipeline(listener, video, work_state)
        if listener.is_cancelled:
            break
    return up_path


async def _send_track_merge_planner(listener, input_path, state, extra_inputs, output_path):
    merge_audio = [item for item in state.get("external_audio", []) if item["index"] in state.get("merge_audio", [])]
    merge_sub = [item for item in state.get("external_sub", []) if item["index"] in state.get("merge_sub", [])]
    if not merge_audio and not merge_sub:
        return
    lines = [
        "<b>Video Tools Merge Planner</b>",
        f"Main: <code>{ospath.basename(input_path)}</code>",
        f"Output: <code>{ospath.basename(output_path)}</code>",
        "",
        "<b>Audio to mux:</b>",
    ]
    lines.extend(
        f"- <code>{item['name']}</code>" for item in merge_audio
    )
    if not merge_audio:
        lines.append("- none")
    lines.append("")
    lines.append("<b>Subtitles to mux:</b>")
    lines.extend(
        f"- <code>{item['name']}</code>" for item in merge_sub
    )
    if not merge_sub:
        lines.append("- none")
    lines.append("")
    lines.append("Action: stream-copy mux, no video re-encode unless FFmpeg requires it.")
    await send_message(listener.message, "\n".join(lines))


async def _get_mkv_tracks(input_path):
    res = await cmd_exec([BinConfig.MKVMERGE_NAME, "-J", input_path])
    if res[2] != 0 or not res[0]:
        return None
    try:
        data = json.loads(res[0])
        return data.get("tracks", [])
    except Exception:
        return None


async def _try_mkvmerge_pipeline(listener, input_path, output_path, state, extra_inputs):
    try:
        tracks = await _get_mkv_tracks(input_path)
        if not tracks:
            return False

        audio_tracks = [t for t in tracks if t.get("type") == "audio"]
        sub_tracks = [t for t in tracks if t.get("type") == "subtitles"]
        video_tracks = [t for t in tracks if t.get("type") == "video"]

        audio_remove = set(state.get("remove_audio", []))
        if state.get("remove_original_audio"):
            audio_keep = []
        elif state.get("keep_audio"):
            audio_keep = [idx for idx in state["keep_audio"] if idx not in audio_remove]
        else:
            audio_keep = [t["index"] for t in state["audio_tracks"] if t["index"] not in audio_remove]

        if state.get("audio_order_value"):
            ordered = _ordered_track_indexes(
                [t for t in state["audio_tracks"] if t["index"] in audio_keep],
                state.get("audio_order_value"),
                remove_unmatched=False,
            )
            audio_keep = [idx for idx in ordered if idx in audio_keep]
        elif state.get("audio_order"):
            ordered = [idx for idx in state["audio_order"] if idx in audio_keep]
            ordered.extend(idx for idx in audio_keep if idx not in ordered)
            audio_keep = ordered

        sub_remove = set(state.get("remove_sub", []))
        if state.get("keep_sub"):
            sub_keep = [idx for idx in state["keep_sub"] if idx not in sub_remove]
        else:
            sub_keep = [t["index"] for t in state["sub_tracks"] if t["index"] not in sub_remove]

        if state.get("sub_order_value"):
            ordered = _ordered_track_indexes(
                [t for t in state["sub_tracks"] if t["index"] in sub_keep],
                state.get("sub_order_value"),
                remove_unmatched=bool(state.get("keep_sub")),
            )
            sub_keep = [idx for idx in ordered if idx in sub_keep]
        elif state.get("sub_order"):
            ordered = [idx for idx in state["sub_order"] if idx in sub_keep]
            ordered.extend(idx for idx in sub_keep if idx not in ordered)
            sub_keep = ordered

        user_meta = listener.user_dict.get("METADATA") or {}
        if isinstance(user_meta, str):
            from ..ext_utils.metadata_utils import MetadataProcessor
            user_meta = MetadataProcessor.parse_string(user_meta)
        meta_title = (
            user_meta.get("title")
            or listener.user_dict.get("METADATA_TITLE")
            or getattr(Config, "METADATA_TITLE", "")
            or "[@Anime_Starfall🥰]"
        )

        cmd = [BinConfig.MKVMERGE_NAME, "-o", output_path]
        if meta_title:
            cmd.extend(["--title", str(meta_title)])
        cmd.append("--no-global-tags")

        if video_tracks:
            cmd.extend(["--video-tracks", ",".join(str(t["id"]) for t in video_tracks)])
            if meta_title:
                for t in video_tracks:
                    cmd.extend(["--track-name", f"{t['id']}:{meta_title}"])

        if not audio_keep:
            cmd.append("--no-audio")
        else:
            mkv_audio_ids = [
                str(audio_tracks[i]["id"])
                for i in audio_keep
                if i < len(audio_tracks)
            ]
            if mkv_audio_ids:
                cmd.extend(["--audio-tracks", ",".join(mkv_audio_ids)])
            else:
                cmd.append("--no-audio")

        if not sub_keep:
            cmd.append("--no-subtitles")
        else:
            mkv_sub_ids = [
                str(sub_tracks[i]["id"])
                for i in sub_keep
                if i < len(sub_tracks)
            ]
            if mkv_sub_ids:
                cmd.extend(["--subtitle-tracks", ",".join(mkv_sub_ids)])
            else:
                cmd.append("--no-subtitles")

        has_external_audio = any(kind == "audio" for kind, _, _ in extra_inputs)
        default_audio = state.get("default_audio")
        primary_audio_idx = None
        if not has_external_audio and audio_keep:
            if default_audio is not None and default_audio in audio_keep:
                primary_audio_idx = default_audio
            else:
                primary_audio_idx = audio_keep[0]

        for i in audio_keep:
            if i >= len(audio_tracks):
                continue
            t = audio_tracks[i]
            tid = t["id"]
            props = t.get("properties", {})
            st_track = state["audio_tracks"][i] if i < len(state.get("audio_tracks", [])) else {}
            raw_lang = props.get("language") or st_track.get("lang") or ""
            raw_title = props.get("track_name") or st_track.get("title") or ""
            codec = t.get("codec") or st_track.get("codec") or ""
            channels = props.get("audio_channels") or ""
            iso_code, clean_title = normalize_mkv_track_meta(raw_lang, raw_title, codec=codec, channels=channels)
            cmd.extend([
                "--language", f"{tid}:{iso_code}",
                "--track-name", f"{tid}:{clean_title}",
            ])
            is_primary = (i == primary_audio_idx)
            cmd.extend(["--default-track-flag", f"{tid}:{1 if is_primary else 0}"])

        intro_sub_count = any(kind == "intro_sub" for kind, _, _ in extra_inputs)
        default_sub = state.get("default_sub")
        for i in sub_keep:
            if i >= len(sub_tracks):
                continue
            t = sub_tracks[i]
            tid = t["id"]
            props = t.get("properties", {})
            st_sub = state["sub_tracks"][i] if i < len(state.get("sub_tracks", [])) else {}
            raw_lang = props.get("language") or st_sub.get("lang") or ""
            raw_title = props.get("track_name") or st_sub.get("title") or ""
            codec = t.get("codec") or st_sub.get("codec") or ""
            iso_code, clean_title = normalize_mkv_track_meta(raw_lang, raw_title, codec=codec)
            cmd.extend([
                "--language", f"{tid}:{iso_code}",
                "--track-name", f"{tid}:{clean_title}",
            ])
            if not intro_sub_count and default_sub is not None:
                is_sub_default = (i == default_sub)
        track_order = []
        for t in video_tracks:
            track_order.append(f"0:{t['id']}")

        for ext_idx, (kind, _, _) in enumerate(extra_inputs, start=1):
            if kind == "audio":
                track_order.append(f"{ext_idx}:0")

        for i in audio_keep:
            if i < len(audio_tracks):
                track_order.append(f"0:{audio_tracks[i]['id']}")

        for ext_idx, (kind, _, _) in enumerate(extra_inputs, start=1):
            if kind == "intro_sub":
                track_order.append(f"{ext_idx}:0")

        for i in sub_keep:
            if i < len(sub_tracks):
                track_order.append(f"0:{sub_tracks[i]['id']}")

        for ext_idx, (kind, _, _) in enumerate(extra_inputs, start=1):
            if kind == "sub":
                track_order.append(f"{ext_idx}:0")

        if track_order:
            cmd.extend(["--track-order", ",".join(track_order)])

        cmd.append(input_path)

        for kind, path, item in extra_inputs:
            if kind == "audio":
                if item and item.get("delay_ms"):
                    cmd.extend(["--sync", f"0:{int(item['delay_ms'])}"])
                raw_lang = (item or {}).get("language", "")
                raw_title = str((item or {}).get("title") or (item or {}).get("name") or "External Audio")
                iso_code, clean_title = normalize_mkv_track_meta(raw_lang, raw_title)
                cmd.extend([
                    "--language", f"0:{iso_code}",
                    "--track-name", f"0:{clean_title}",
                    "--default-track-flag", "0:1",
                    path,
                ])
            elif kind == "intro_sub":
                cmd.extend([
                    "--track-name", f"0:{meta_title or 'Intro'}",
                    "--language", "0:eng",
                    "--default-track-flag", "0:1",
                    path,
                ])
            elif kind == "sub":
                raw_lang = (item or {}).get("language", "")
                raw_title = str((item or {}).get("title") or (item or {}).get("name") or "Subtitle")
                iso_code, clean_title = normalize_mkv_track_meta(raw_lang, raw_title)
                cmd.extend([
                    "--language", f"0:{iso_code}",
                    "--track-name", f"0:{clean_title}",
                    path,
                ])

        LOGGER.info(
            "Video Tool: attempting fast mkvmerge mux for %s",
            ospath.basename(input_path),
        )
        success, out_msg = await execute_mkvmerge_with_fallback(
            cmd, input_path, output_path, cmd_exec
        )
        if success and await aiopath.exists(output_path):
            LOGGER.info("mkvmerge muxing successful: %s", output_path)
            return True
        LOGGER.warning(
            "mkvmerge muxing failed: %s; falling back to FFmpeg",
            (out_msg or "")[-500:],
        )
        if await aiopath.exists(output_path):
            await remove(output_path)
        return False
    except Exception as e:
        LOGGER.warning("mkvmerge muxing exception (%s), falling back to FFmpeg", e)
        if await aiopath.exists(output_path):
            await remove(output_path)
        return False


async def _execute_vt_pipeline(listener, input_path, state):
    """Run FFmpeg based on user selections from the VT UI."""
    await _refresh_external_tracks(state, input_path)
    has_removals = (
        state.get("remove_audio")
        or state.get("remove_original_audio")
        or state.get("remove_sub")
        or state.get("keep_audio")
        or state.get("keep_sub")
        or state.get("audio_order")
        or state.get("sub_order")
        or state.get("audio_order_value")
        or state.get("sub_order_value")
    )
    has_default_audio = state.get("default_audio") is not None
    has_default_sub = state.get("default_sub") is not None
    has_extractions = state.get("extract_audio") or state.get("extract_sub")
    has_merge_audio = state.get("merge_audio")
    has_merge_sub = state.get("merge_sub")
    has_translate = state.get("translate_sub")
    has_intro = state.get("intro_subtitle") and (
        listener.user_dict.get("INTRO_SUBTITLE_TEXT")
        or Config.INTRO_SUBTITLE_TEXT
    )
    extract_only = bool(has_extractions) and not (
        has_removals
        or has_default_audio
        or has_default_sub
        or has_merge_audio
        or has_merge_sub
        or has_translate
        or has_intro
    )

    # Extract-only tasks return a small folder so the normal uploader sends only
    if has_extractions:
        extracted_paths, extract_dir = await _extract_streams(listener, input_path, state)
        if extract_only:
            listener._vt_extract_only = True
            if extracted_paths:
                with suppress(Exception):
                    await remove(input_path)
                if len(extracted_paths) == 1:
                    return extracted_paths[0]
                return extract_dir
            LOGGER.warning("All stream extractions failed; preserving original video")
            return input_path

    dir_path = ospath.dirname(input_path)
    extra_inputs = []
    cleanup_paths = []

    for idx in state.get("merge_audio", []):
        for item in state.get("external_audio", []):
            if item["index"] == idx:
                extra_inputs.append(("audio", item["path"], item))
                cleanup_paths.append(item["path"])

    for idx in state.get("merge_sub", []):
        for item in state.get("external_sub", []):
            if item["index"] == idx:
                extra_inputs.append(("sub", item["path"], item))
                cleanup_paths.append(item["path"])

    if has_translate:
        target = _target_lang(listener)
        all_sub = {t["index"]: t for t in state.get("sub_tracks", [])}
        for idx in state.get("translate_sub", []):
            sub_path = ospath.join(dir_path, f"subtitle_{listener.mid}_{idx}.srt")
            track = all_sub.get(idx)
            if track and await _extract_sub_as_srt(input_path, track, sub_path):
                try:
                    translated = await _translate_srt_file(
                        sub_path, target, listener.mid
                    )
                    extra_inputs.append(("sub", translated, None))
                    cleanup_paths.extend([sub_path, translated])
                except Exception as e:
                    LOGGER.warning(f"Subtitle translate failed: {e}")
                    await send_message(
                        listener.message,
                        f"Subtitle translate skipped: <code>{e}</code>",
                    )
                    if await aiopath.exists(sub_path):
                        await remove(sub_path)

    if has_intro:
        intro_path = await _create_intro_subtitle(listener, dir_path, input_path)
        if intro_path:
            extra_inputs.insert(0, ("intro_sub", intro_path, None))
            cleanup_paths.append(intro_path)

    # If no mux operations needed, return original
    if not (
        has_removals
        or has_default_audio
        or has_default_sub
        or extra_inputs
    ):
        return input_path

    # Build mux output target
    base_name = state.get("output_name") or ospath.basename(input_path)
    stem, ext = ospath.splitext(base_name)
    output_ext = ".mkv" if (extra_inputs or base_name.endswith(".mkv")) else ext
    output_path = ospath.join(dir_path, f"vt_{stem}{output_ext}")
    await _send_track_merge_planner(listener, input_path, state, extra_inputs, output_path)

    # Try mkvmerge fast remux if input is MKV or output is MKV
    if input_path.lower().endswith(".mkv") or output_ext == ".mkv":
        mkv_success = await _try_mkvmerge_pipeline(
            listener, input_path, output_path, state, extra_inputs
        )
        if mkv_success:
            await remove(input_path)
            for path in cleanup_paths:
                if await aiopath.exists(path):
                    await remove(path)
            if output_ext == ext:
                await rename(output_path, input_path)
                return input_path
            final_path = ospath.join(dir_path, f"{stem}{output_ext}")
            if await aiopath.exists(final_path):
                await remove(final_path)
            await rename(output_path, final_path)
            return final_path

    # Fallback to FFmpeg mux command
    cmd = [
        BinConfig.FFMPEG_NAME,
        "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1",
        "-y", "-i", input_path,
    ]

    for kind, path, item in extra_inputs:
        if kind == "audio" and item and item.get("delay_ms"):
            cmd.extend(["-itsoffset", f"{int(item['delay_ms']) / 1000:.3f}"])
        cmd.extend(["-i", path])

    cmd.extend(["-map", "0:v"])

    all_audio = {t["index"]: t for t in state["audio_tracks"]}
    all_sub = {t["index"]: t for t in state["sub_tracks"]}

    # External audio is intentionally first in the output. This also makes a
    # manually added dub the default track while retaining original audio.
    merged_audio = []
    for input_idx, (kind, _, item) in enumerate(extra_inputs, start=1):
        if kind == "audio":
            stream_index = int((item or {}).get("stream_index", 0))
            cmd.extend(["-map", f"{input_idx}:a:{stream_index}?"])
            merged_audio.append(item or {})

    # Audio tracks to keep (respecting removals, keep-only, and order)
    audio_remove = set(state.get("remove_audio", []))
    if state.get("remove_original_audio"):
        audio_keep = []
    elif state.get("keep_audio"):
        audio_keep = [idx for idx in state["keep_audio"] if idx not in audio_remove]
    else:
        audio_keep = [t["index"] for t in state["audio_tracks"] if t["index"] not in audio_remove]
    if state.get("audio_order_value"):
        ordered = _ordered_track_indexes(
            [t for t in state["audio_tracks"] if t["index"] in audio_keep],
            state.get("audio_order_value"),
            # Keep filters decide which tracks survive. Ordering must never
            # discard another selected language that is not named first.
            remove_unmatched=False,
        )
        audio_keep = [idx for idx in ordered if idx in audio_keep]
    elif state.get("audio_order"):
        ordered = [idx for idx in state["audio_order"] if idx in audio_keep]
        ordered.extend(idx for idx in audio_keep if idx not in ordered)
        audio_keep = ordered

    for idx in audio_keep:
        track = all_audio.get(idx)
        if track:
            cmd.extend(["-map", f"0:{track.get('stream_index', idx)}"])

    # Intro subtitles are mapped before existing subtitle streams so they open first.
    intro_sub_count = 0
    for input_idx, (kind, _, _) in enumerate(extra_inputs, start=1):
        if kind == "intro_sub":
            cmd.extend(["-map", f"{input_idx}:s?"])
            intro_sub_count += 1

    # Subtitle tracks to keep (respecting removals, keep-only, and order)
    sub_remove = set(state.get("remove_sub", []))
    if state.get("keep_sub"):
        sub_keep = [idx for idx in state["keep_sub"] if idx not in sub_remove]
    else:
        sub_keep = [t["index"] for t in state["sub_tracks"] if t["index"] not in sub_remove]
    if state.get("sub_order_value"):
        ordered = _ordered_track_indexes(
            [t for t in state["sub_tracks"] if t["index"] in sub_keep],
            state.get("sub_order_value"),
            remove_unmatched=bool(state.get("keep_sub")),
        )
        sub_keep = [idx for idx in ordered if idx in sub_keep]
    elif state.get("sub_order"):
        ordered = [idx for idx in state["sub_order"] if idx in sub_keep]
        ordered.extend(idx for idx in sub_keep if idx not in ordered)
        sub_keep = ordered

    for idx in sub_keep:
        track = all_sub.get(idx)
        if track:
            cmd.extend(["-map", f"0:{track.get('stream_index', idx)}"])

    for input_idx, (kind, _, item) in enumerate(extra_inputs, start=1):
        if kind == "sub":
            cmd.extend(["-map", f"{input_idx}:s?"])

    # Keep attachments and data streams
    cmd.extend(["-map", "0:t?", "-map", "0:d?"])

    # Copy all streams
    cmd.extend(
        ["-c", "copy", "-threads", str(get_ffmpeg_threads()), "-max_muxing_queue_size", "9999"]
    )

    # Reset all dispositions first
    cmd.extend(["-disposition:a", "0", "-disposition:s", "0"])

    # Set default audio
    for offset, item in enumerate(merged_audio):
        language = _normalize_language(item.get("language", "und"))
        title = str(item.get("title") or item.get("name") or "External Audio")[:80]
        cmd.extend(
            [
                f"-metadata:s:a:{offset}", f"language={language}",
                f"-metadata:s:a:{offset}", f"title={title}",
            ]
        )

    default_audio = state.get("default_audio")
    if merged_audio:
        cmd.extend(["-disposition:a:0", "default"])
    elif default_audio is not None and default_audio in audio_keep:
        out_idx = audio_keep.index(default_audio)
        cmd.extend([f"-disposition:a:{out_idx}", "default"])

    if intro_sub_count:
        cmd.extend(["-disposition:s:0", "default", "-metadata:s:s:0", "title=Intro"])

    # Set default subtitle
    default_sub = state.get("default_sub")
    if not intro_sub_count and default_sub is not None and default_sub in sub_keep:
        out_idx = sub_keep.index(default_sub)
        cmd.extend([f"-disposition:s:{out_idx}", "default"])

    cmd.append(output_path)

    LOGGER.info(f"Video Tool: Muxing {ospath.basename(input_path)}...")

    try:
        from ... import task_dict, task_dict_lock
        from ..ext_utils.files_utils import get_path_size
        from ..ext_utils.media_utils import FFMpeg, get_media_info
        from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus

        ffmpeg = FFMpeg(listener)
        ffmpeg.clear()
        duration = (await get_media_info(input_path))[0]
        ffmpeg._total_time = duration
        listener.subname = ospath.basename(input_path)
        listener.subsize = await get_path_size(input_path)
        listener.progress = True
        async with task_dict_lock:
            task_dict[listener.mid] = FFmpegStatus(
                listener,
                ffmpeg,
                getattr(listener, "_process_gid", str(listener.mid)),
                "Auto Process",
            )
        async with ffmpeg_task(listener, "Video Tools mux"):
            listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
            stderr_task = create_task(listener.subproc.stderr.read())
            await ffmpeg._ffmpeg_progress()
            stderr = await stderr_task
            await listener.subproc.wait()
        process = listener.subproc

        if process.returncode != 0:
            err = stderr.decode(errors="replace").strip() if stderr else "Unknown error"
            LOGGER.error(f"FFmpeg muxing error: {err}")
            if await aiopath.exists(output_path):
                await remove(output_path)
            for path in cleanup_paths:
                if ospath.basename(path).startswith(("subtitle_", "translated_", "intro_")) and await aiopath.exists(path):
                    await remove(path)
            return None

        if not await aiopath.exists(output_path):
            LOGGER.error("FFmpeg muxing: output file not found")
            for path in cleanup_paths:
                if ospath.basename(path).startswith(("subtitle_", "translated_", "intro_")) and await aiopath.exists(path):
                    await remove(path)
            return None

        await remove(input_path)
        for path in cleanup_paths:
            if await aiopath.exists(path):
                await remove(path)
        if output_ext == ext:
            await rename(output_path, input_path)
            return input_path
        final_path = ospath.join(dir_path, f"{stem}{output_ext}")
        if await aiopath.exists(final_path):
            await remove(final_path)
        await rename(output_path, final_path)
        return final_path

    except Exception as e:
        LOGGER.error(f"FFmpeg muxing exception: {e}")
        if await aiopath.exists(output_path):
            await remove(output_path)
        for path in cleanup_paths:
            if ospath.basename(path).startswith(("subtitle_", "translated_", "intro_")) and await aiopath.exists(path):
                await remove(path)
        return None


async def _extract_streams(listener, input_path, state):
    """Extract selected audio/subtitle streams and return their output folder."""
    all_audio = {t["index"]: t for t in state["audio_tracks"]}
    all_sub = {t["index"]: t for t in state["sub_tracks"]}
    dir_path = ospath.dirname(input_path)
    stem = ospath.splitext(ospath.basename(input_path))[0]
    extract_dir = ospath.join(dir_path, f"vt_extract_{listener.mid}_{stem[:40]}")
    await makedirs(extract_dir, exist_ok=True)
    extracted = []
    failed = False

    for idx in state.get("extract_audio", []):
        track = all_audio.get(idx)
        if not track:
            continue
        ext = _extract_extension(track, "audio")
        out_name = f"Audio_{track['lang']}_{idx + 1}{ext}"
        out_path = ospath.join(extract_dir, out_name)
        if await _extract_single(input_path, track, out_path):
            extracted.append(out_path)
        else:
            failed = True

    for idx in state.get("extract_sub", []):
        track = all_sub.get(idx)
        if not track:
            continue
        ext = _extract_extension(track, "subtitle")
        out_name = f"Subtitle_{track['lang']}_{idx + 1}{ext}"
        out_path = ospath.join(extract_dir, out_name)
        if await _extract_single(input_path, track, out_path):
            extracted.append(out_path)
        else:
            failed = True
    if failed:
        await send_message(
            listener.message,
            "Extract failed: selected stream could not be copied. Codec/container mismatch or unsupported stream.",
        )
    return extracted, extract_dir


def _extract_extension(track, kind):
    codec = str(track.get("codec") or track.get("codec_name") or "").lower()
    if kind == "audio":
        return {
            "aac": ".m4a",
            "eac3": ".eac3",
            "ac3": ".ac3",
            "dts": ".dts",
            "dts-hd": ".dts",
            "truehd": ".thd",
            "flac": ".flac",
            "opus": ".opus",
            "mp3": ".mp3",
            "vorbis": ".ogg",
            "alac": ".m4a",
            "pcm_s16le": ".wav",
            "pcm_s24le": ".wav",
            "pcm_s32le": ".wav",
            "pcm_u8": ".wav",
        }.get(codec, f".{codec or 'mka'}")
    return {
        "subrip": ".srt",
        "srt": ".srt",
        "ass": ".ass",
        "ssa": ".ass",
        "webvtt": ".vtt",
        "hdmv_pgs_subtitle": ".sup",
        "pgs": ".sup",
        "dvd_subtitle": ".sub",
        "mov_text": ".srt",
    }.get(codec, f".{codec or 'srt'}")


async def _extract_single(input_path, track, out_path):
    """Extract a single stream using native mkvextract or FFmpeg."""
    stream_index = track.get("stream_index", track.get("index"))
    codec = str(track.get("codec") or track.get("codec_name") or "unknown").lower()
    ext = ospath.splitext(out_path)[1].lower()
    map_spec = f"0:{stream_index}"
    LOGGER.info(
        "VT extract stream: index=%s codec=%s ext=%s output=%s",
        stream_index,
        codec,
        ext,
        out_path,
    )

    # 1. If source is MKV, try fast native mkvextract first
    if input_path.lower().endswith(".mkv"):
        try:
            mkv_cmd = ["mkvextract", "tracks", input_path, f"{stream_index}:{out_path}"]
            async with ffmpeg_task(label="Extract stream mkvextract"):
                proc = await create_subprocess_exec(*mkv_cmd, stdout=PIPE, stderr=PIPE)
                _, _ = await proc.communicate()
            if proc.returncode == 0 and await aiopath.exists(out_path) and await aiopath.getsize(out_path) > 0:
                LOGGER.info("VT extract via mkvextract succeeded: %s", out_path)
                return True
        except Exception as e:
            LOGGER.debug("mkvextract not available or failed (%s); trying FFmpeg", e)
        if await aiopath.exists(out_path):
            with suppress(Exception):
                await remove(out_path)

    # 2. FFmpeg extraction with codec-appropriate arguments
    threads = str(get_ffmpeg_threads())
    base_ffmpeg = [
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        input_path,
        "-map",
        map_spec,
        "-threads",
        threads,
    ]

    cmd_codec = []
    if ext == ".srt":
        cmd_codec = ["-c:s", "srt"]
    elif ext == ".vtt":
        cmd_codec = ["-c:s", "webvtt"]
    elif ext == ".ass":
        cmd_codec = ["-c:s", "copy"]
    elif ext == ".sup":
        cmd_codec = ["-c:s", "copy"]
    elif ext == ".m4a":
        if codec == "aac":
            cmd_codec = ["-c:a", "copy"]
        else:
            cmd_codec = ["-c:a", "aac", "-b:a", "256k"]
    elif ext in {".ac3", ".eac3", ".dts", ".flac", ".mp3", ".opus", ".ogg", ".thd"}:
        cmd_codec = ["-c:a", "copy"]
    else:
        cmd_codec = ["-c", "copy"]

    full_cmd = base_ffmpeg + cmd_codec + [out_path]

    async with ffmpeg_task(label="Extract stream"):
        process = await create_subprocess_exec(*full_cmd, stdout=PIPE, stderr=PIPE)
        _, stderr = await process.communicate()

    LOGGER.info("VT extract stream return code: %s", process.returncode)
    valid_output = False
    if process.returncode == 0 and await aiopath.exists(out_path):
        try:
            valid_output = await aiopath.getsize(out_path) > 0
        except Exception:
            valid_output = False

    # 3. Fallback transcode if copy failed
    if not valid_output:
        if await aiopath.exists(out_path):
            with suppress(Exception):
                await remove(out_path)
        LOGGER.info("Stream copy failed for %s (codec=%s), trying fallback conversion", map_spec, codec)
        fallback_codec = []
        if ext in {".m4a", ".aac"}:
            fallback_codec = ["-c:a", "aac", "-b:a", "256k"]
        elif ext == ".mp3":
            fallback_codec = ["-c:a", "libmp3lame", "-q:a", "2"]
        elif ext in {".opus", ".ogg"}:
            fallback_codec = ["-c:a", "libopus", "-b:a", "160k"]
        elif ext == ".wav":
            fallback_codec = ["-c:a", "pcm_s16le"]
        elif ext == ".flac":
            fallback_codec = ["-c:a", "flac"]
        elif ext == ".ass":
            fallback_codec = ["-c:s", "ass"]
        elif ext == ".srt":
            fallback_codec = ["-c:s", "srt"]

        if fallback_codec:
            fallback_cmd = base_ffmpeg + fallback_codec + [out_path]
            async with ffmpeg_task(label="Extract stream fallback"):
                process2 = await create_subprocess_exec(*fallback_cmd, stdout=PIPE, stderr=PIPE)
                _, stderr2 = await process2.communicate()
            if process2.returncode == 0 and await aiopath.exists(out_path):
                try:
                    valid_output = await aiopath.getsize(out_path) > 0
                except Exception:
                    valid_output = False
            if not valid_output and await aiopath.exists(out_path):
                with suppress(Exception):
                    await remove(out_path)

    if not valid_output:
        LOGGER.warning(
            "Stream extraction failed for %s codec=%s rc=%s stderr=%s",
            map_spec,
            codec,
            process.returncode,
            stderr.decode(errors="ignore")[-500:] if stderr else "",
        )
        return False
    return True


async def _extract_sub_as_srt(input_path, track, out_path):
    stream_index = track.get("stream_index", track.get("index"))
    map_spec = f"0:{stream_index}"
    cmd = []
    if shutil.which("taskset"):
        cmd.extend(["taskset", "-c", get_ffmpeg_cores()])
    cmd.extend([
        BinConfig.FFMPEG_NAME,
        "-y", "-i", input_path,
        "-map", map_spec,
        "-c:s", "srt",
        "-threads", str(get_ffmpeg_threads()),
        out_path,
    ])
    async with ffmpeg_task(label="Extract subtitle for translate"):
        process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        _, stderr = await process.communicate()
    if process.returncode == 0 and await aiopath.exists(out_path):
        try:
            if await aiopath.getsize(out_path) > 0:
                return True
        except Exception:
            pass
    with suppress(Exception):
        if await aiopath.exists(out_path):
            await remove(out_path)
    LOGGER.warning(
        "Subtitle SRT extraction failed for %s codec=%s rc=%s stderr=%s",
        map_spec,
        track.get("codec"),
        process.returncode,
        stderr.decode(errors="ignore")[-500:] if stderr else "",
    )
    return False


async def pre_probe_and_show_ui(listener, file_, reply_to):
    """
    Tries to probe stream info before download starts (via streaming or chunks).
    If successful, shows the VT UI to the user to configure.
    """
    task_id = str(listener.mid)
    temp_dir = ospath.join(DOWNLOAD_DIR, f"vt_probe_{task_id}")
    temp_path = None

    async def _do_pre_probe():
        nonlocal temp_path
        audio_tracks, sub_tracks = [], []
        filename = "video"

        # 1. Telegram Media
        if file_ is not None:
            filename = file_.file_name or "video"
            await makedirs(temp_dir, exist_ok=True)
            temp_path = ospath.join(temp_dir, filename)

            from ...core.tg_client import TgClient
            async for chunk in TgClient.bot.stream_media(file_, limit=5):
                async with aiopen(temp_path, "ab") as f:
                    await f.write(chunk)

            audio_tracks, sub_tracks = await probe_streams(temp_path)

        # 2. Direct Link
        elif listener.link and is_url(listener.link):
            filename = ospath.basename(listener.link.split("?")[0]) or "video"

            # Try direct probe on link first
            audio_tracks, sub_tracks = await probe_streams(listener.link)

            # Fallback to downloading a 10MB chunk
            if not audio_tracks and not sub_tracks:
                await makedirs(temp_dir, exist_ok=True)
                temp_path = ospath.join(temp_dir, filename)
                headers = {
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
                from aiohttp import ClientSession
                async with ClientSession() as session:
                    async with session.get(listener.link, headers=headers) as response:
                        async with aiopen(temp_path, "wb") as f:
                            async for chunk in response.content.iter_chunked(10000000):
                                await f.write(chunk)
                                break

                audio_tracks, sub_tracks = await probe_streams(temp_path)

        if not audio_tracks and not sub_tracks:
            raise ValueError("No audio or subtitle streams found in chunk")

        # Clean up temporary chunk file
        if temp_path and await aiopath.exists(temp_path):
            await remove(temp_path)

        state = _base_state(task_id, filename, audio_tracks, sub_tracks)
        state["pre_probed"] = True
        listener._vt_state = state
        listener._vt_processed = True

        done_event = Event()
        _active_vt_sessions[task_id] = {
            "event": done_event,
            "state": state,
            "listener": listener,
        }

        try:
            from ...modules.video_tool_ui import render_video_tools_main
            tag = getattr(listener, "tag", None) or (listener.message.from_user.mention if listener.message.from_user else "")
            vt_msg = await send_message(listener.message, f"{tag} ⚙️ <b>Generating Video Tools UI...</b>")
            listener._vt_msg = vt_msg
            await render_video_tools_main(vt_msg, state)

            try:
                await asyncio.wait_for(done_event.wait(), timeout=UI_TIMEOUT)
            except Exception:
                LOGGER.info(f"Video Tool pre-probe interaction timeout for task {task_id}")
                state["completed"] = True

            if state.get("cancelled", False):
                listener._vt_state = None
                listener._vt_processed = False
            else:
                await vt_msg.delete()
        finally:
            _active_vt_sessions.pop(task_id, None)
            if state.get("cancelled", False):
                listener._vt_state = None
                listener._vt_processed = False
            listener._vt_msg = None
            if temp_dir and await aiopath.exists(temp_dir):
                with suppress(Exception):
                    await rmtree(temp_dir, ignore_errors=True)

    try:
        await wait_for(_do_pre_probe(), timeout=60.0)
    except Exception as e:
        LOGGER.warning(f"Video Tool: Pre-probing failed or timed out for task {task_id}: {e}. Falling back to full download first.")
        await send_message(
            listener.message,
            "Please wait for download to finish before opening Video Tools.",
        )
        if temp_dir and await aiopath.exists(temp_dir):
            with suppress(Exception):
                await rmtree(temp_dir, ignore_errors=True)
