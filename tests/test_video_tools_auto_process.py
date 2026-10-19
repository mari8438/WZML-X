from pathlib import Path
import re
import pytest

_REPO_ROOT = Path(__file__).parents[1]

# Pure implementations matching bot/helper/video_utils
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
    combined = f"{raw_lang or ''} {raw_title or ''}"
    for pattern, iso_code, label in ISO_LANG_PATTERNS:
        if pattern.search(combined):
            suffix = " ".join(filter(None, [codec.upper(), str(channels) if channels else ""]))
            clean_title = f"{label} • {suffix}" if suffix else label
            return iso_code, clean_title
    iso_fallback = raw_lang.lower() if (raw_lang and len(raw_lang) == 3 and raw_lang.isalpha()) else "und"
    return iso_fallback, (raw_title or "Track")


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


def _ordered_track_indexes(tracks, order_value, remove_unmatched=False):
    tokens = [t.strip().lower() for t in re.split(r"[,|\s]+", str(order_value or "")) if t.strip()]
    if not tokens:
        return [t["index"] for t in tracks]
    ordered = []
    used = set()
    for token in tokens:
        for track in tracks:
            idx = track["index"]
            if idx not in used and (token in track.get("lang", "").lower() or token in track.get("title", "").lower()):
                ordered.append(idx)
                used.add(idx)
    if not remove_unmatched:
        ordered.extend(track["index"] for track in tracks if track["index"] not in used)
    return ordered


def _quiet_messages(auto_process_logs: bool = False, auto_process_message_mode: str = "quiet") -> bool:
    if not auto_process_logs:
        return True
    return str(auto_process_message_mode or "quiet").lower() == "quiet"


def _lang_set(value):
    if not value:
        return set()
    return {
        item.strip().lower()
        for item in re.split(r"[,|\s]+", str(value))
        if item.strip()
    }


def _lang_matches(track_lang, keep, track_title=""):
    if not keep:
        return False
    lang = str(track_lang or "").strip().lower()
    aliases = {
        "ta": {"ta", "tam", "tamil"},
        "tam": {"ta", "tam", "tamil"},
        "tamil": {"ta", "tam", "tamil"},
        "en": {"en", "eng", "english"},
        "eng": {"en", "eng", "english"},
        "english": {"en", "eng", "english"},
        "hi": {"hi", "hin", "hindi"},
        "hin": {"hi", "hin", "hindi"},
        "hindi": {"hi", "hin", "hindi"},
    }
    expanded_keep = set()
    for token in keep:
        expanded_keep |= aliases.get(token, {token})
    track_values = aliases.get(lang, {lang}) | {lang}
    return bool(track_values & expanded_keep)


def _merge_group_title(title, episode=None):
    value = str(title or "Unknown").strip()
    value = re.sub(r"^\[[^\]]*\]\s*", "", value)
    value = re.sub(r"\s*\[[^\]]*\].*$", " ", value)
    value = re.sub(r"\s*\([^)]*\)\s*$", " ", value)
    value = re.sub(
        r"\b(?:2160p|1080p|1440p|720p|576p|480p|4k|8k|10bit|8bit|12bit|x264|x265|h\.?264|h\.?265|hevc|av1|web[- .]?dl|web[- .]?rip|bluray|bdrip|webrip|hdr|remux|multi|dual|esub|msub|aac|ac3|ddp?|eac3)\b.*$",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:s\d{1,2}\s*e\d{1,4}|episode\s*\d{1,4}|ep\s*\d{1,4}|e\d{1,4})\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"(?:[\s._-]+\d{1,4}){1,4}\s*$", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -._")
    return value or "unknown"


# --- Unit Tests ---

def test_normalize_mkv_track_meta():
    iso, title = normalize_mkv_track_meta("tam", "Tamil Audio", codec="aac", channels="2")
    assert iso == "tam"
    assert "Tamil" in title
    assert "AAC" in title

    iso_eng, title_eng = normalize_mkv_track_meta("eng", "English ESub", codec="subrip")
    assert iso_eng == "eng"
    assert "English" in title_eng

    iso_und, title_und = normalize_mkv_track_meta("", "Commentary")
    assert iso_und == "und"
    assert title_und == "Commentary"


def test_normalize_language_tokens():
    assert _normalize_language("tamil") == "tam"
    assert _normalize_language("ta") == "tam"
    assert _normalize_language("english") == "eng"
    assert _normalize_language("hindi") == "hin"
    assert _normalize_language("japanese") == "jpn"
    assert _normalize_language("korean") == "kor"


def test_ordered_track_indexes():
    tracks = [
        {"index": 0, "lang": "eng", "title": "English"},
        {"index": 1, "lang": "tam", "title": "Tamil"},
        {"index": 2, "lang": "hin", "title": "Hindi"},
    ]
    ordered = _ordered_track_indexes(tracks, "tam,hin,eng")
    assert ordered == [1, 2, 0]

    ordered_partial = _ordered_track_indexes(tracks, "tam")
    assert ordered_partial == [1, 0, 2]


def test_quiet_messages_logic():
    assert _quiet_messages(auto_process_logs=False) is True
    assert _quiet_messages(auto_process_logs=True, auto_process_message_mode="quiet") is True
    assert _quiet_messages(auto_process_logs=True, auto_process_message_mode="verbose") is False


def test_lang_matches():
    keep = _lang_set("tam,eng")
    assert _lang_matches("ta", keep) is True
    assert _lang_matches("tam", keep) is True
    assert _lang_matches("tamil", keep) is True
    assert _lang_matches("eng", keep) is True
    assert _lang_matches("hin", keep) is False


def test_merge_group_title_normalization():
    assert _merge_group_title("Naruto Shippuden 033 1080p x265", "33") == "Naruto Shippuden"
    assert _merge_group_title("My.Show.S01E05.720p.WEB-DL", "5") == "My.Show"
    assert _merge_group_title("[SubGroup] Bleach - 12 [1080p]", "12") == "Bleach"


def test_mkvmerge_track_order_construction():
    video_tracks = [{"id": 0}]
    audio_tracks = [{"id": 1, "index": 0}, {"id": 2, "index": 1}]
    sub_tracks = [{"id": 3, "index": 0}, {"id": 4, "index": 1}]
    audio_keep = [1, 0]  # reordered: index 1 (id 2) first, then index 0 (id 1)
    sub_keep = [0]       # kept index 0 (id 3)
    extra_inputs = [("audio", "/path/ext.m4a", {"name": "External"}), ("intro_sub", "/path/intro.ass", None)]

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

    expected = ["0:0", "1:0", "0:2", "0:1", "2:0", "0:3"]
    assert track_order == expected
    order_arg = ",".join(track_order)
    assert order_arg == "0:0,1:0,0:2,0:1,2:0,0:3"


def test_video_tools_has_track_order_and_no_unclosed_brackets():
    vt_file = _REPO_ROOT / "bot" / "helper" / "video_utils" / "video_tools.py"
    content = vt_file.read_text(encoding="utf-8")
    assert "--track-order" in content
    assert "track_order.append" in content
    assert 'shutil.which("taskset")' in content


def test_auto_process_has_single_pass_pipeline():
    ap_file = _REPO_ROOT / "bot" / "helper" / "video_utils" / "auto_process.py"
    content = ap_file.read_text(encoding="utf-8")
    assert "_auto_process_media_files" in content
    assert "Single-Pass MKV / FFmpeg" in content
    assert "not _quiet_messages():" in content
