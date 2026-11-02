import os
import re
import json
import hmac
import hashlib
import asyncio
from time import time
from contextlib import suppress

try:
    from aiofiles.os import path as aiopath, makedirs as aiomakedirs, remove as aioremove, listdir as aiolistdir
    from aioshutil import copyfile as aiocopyfile, rmtree as aiormtree
except ImportError:
    import shutil
    import asyncio
    class _AsyncOS:
        @staticmethod
        async def exists(p): return os.path.exists(p)
        @staticmethod
        async def isfile(p): return os.path.isfile(p)
        @staticmethod
        async def isdir(p): return os.path.isdir(p)
        @staticmethod
        async def getsize(p): return os.path.getsize(p)
    aiopath = _AsyncOS()
    async def aiomakedirs(p, exist_ok=True): os.makedirs(p, exist_ok=exist_ok)
    async def aioremove(p): os.remove(p)
    async def aiolistdir(p): return os.listdir(p)
    async def aiocopyfile(src, dst): return await asyncio.to_thread(shutil.copyfile, src, dst)
    async def aiormtree(p, ignore_errors=False): return await asyncio.to_thread(shutil.rmtree, p, ignore_errors=ignore_errors)

try:
    from ... import LOGGER, DOWNLOAD_DIR
    from ...core.config_manager import Config
except Exception:
    import logging
    LOGGER = logging.getLogger(__name__)
    DOWNLOAD_DIR = "downloads/"
    class Config:
        BOT_TOKEN = "TEST_TOKEN"
        BASE_URL = ""

LANG_PATTERN_MAP = [
    (re.compile(r"\b(tam|tamil|tml|ta)\b", re.I), "Tamil", "tam"),
    (re.compile(r"\b(hin|hindi|hnd|hi)\b", re.I), "Hindi", "hin"),
    (re.compile(r"\b(tel|telugu|tlg|te)\b", re.I), "Telugu", "tel"),
    (re.compile(r"\b(mal|malayalam|mlym|ml)\b", re.I), "Malayalam", "mal"),
    (re.compile(r"\b(kan|kannada|knd|kn)\b", re.I), "Kannada", "kan"),
    (re.compile(r"\b(eng|english|en)\b", re.I), "English", "eng"),
    (re.compile(r"\b(jpn|japanese|jap|ja)\b", re.I), "Japanese", "jpn"),
    (re.compile(r"\b(kor|korean|ko)\b", re.I), "Korean", "kor"),
    (re.compile(r"\b(ben|bengali|bn)\b", re.I), "Bengali", "ben"),
    (re.compile(r"\b(mar|marathi|mr)\b", re.I), "Marathi", "mar"),
    (re.compile(r"\b(spa|spanish|es)\b", re.I), "Spanish", "spa"),
    (re.compile(r"\b(fra|french|fr)\b", re.I), "French", "fra"),
    (re.compile(r"\b(deu|german|ger|de)\b", re.I), "German", "deu"),
    (re.compile(r"\b(zho|chinese|chi|zh|mandarin|cantonese)\b", re.I), "Chinese", "zho"),
]

def resolve_full_language_name(lang_code: str, title: str = "") -> tuple[str, str]:
    """
    Returns (Full Language Name, ISO-639-2 code).
    Example: ('Tamil', 'tam'), ('Hindi', 'hin'), etc.
    Inspects both track lang code and metadata title.
    """
    combined = f"{lang_code or ''} {title or ''}".strip()
    for regex, full_name, iso in LANG_PATTERN_MAP:
        if regex.search(combined):
            return full_name, iso
    raw = (lang_code or "").strip().capitalize()
    if raw and len(raw) <= 12 and raw.isalpha() and raw.lower() not in {"und", "unknown"}:
        return raw, (lang_code or "und")[:3].lower()
    return "Audio", "und"


def build_extracted_filename(track: dict, video_path: str, kind: str = "audio", duplicate_index: int = 1) -> str:
    """
    Formats extracted filename strictly according to requirement:
    {audio_language} - {filename}.{ext}
    e.g. Tamil - Solo.Leveling.S02E01.720p.eac3
    """
    stem, _ = os.path.splitext(os.path.basename(video_path))
    lang = track.get("lang") or track.get("language") or ""
    title = track.get("title") or ""
    full_lang, _ = resolve_full_language_name(lang, title)

    codec = str(track.get("codec") or track.get("codec_name") or "").lower()
    if kind == "audio":
        ext = {
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
    else:
        ext = {
            "subrip": ".srt",
            "srt": ".srt",
            "ass": ".ass",
            "ssa": ".ass",
            "webvtt": ".vtt",
            "hdmv_pgs_subtitle": ".sup",
            "pgs": ".sup",
        }.get(codec, f".{codec or 'srt'}")

    clean_stem = re.sub(r'[\\/*?:"<>|]', "_", stem).strip()
    dup_suffix = f" ({duplicate_index})" if duplicate_index > 1 else ""
    return f"{full_lang} - {clean_stem}{dup_suffix}{ext}"


def get_audio_bank_dir(user_id: int | str) -> str:
    """Directory where user's extracted audio streams are safely persisted on the VPS."""
    return os.path.join(DOWNLOAD_DIR, "vt_audio_bank", str(user_id))


def get_web_sessions_dir() -> str:
    """Directory holding active Video Tool web sessions and IPC commands."""
    return os.path.join(DOWNLOAD_DIR, "vt_web_sessions")


async def save_to_audio_bank(user_id: int | str, src_path: str, custom_name: str = "") -> str:
    """Saves an extracted audio file into the persistent VPS audio bank."""
    bank_dir = get_audio_bank_dir(user_id)
    await aiomakedirs(bank_dir, exist_ok=True)
    target_name = custom_name or os.path.basename(src_path)
    target_path = os.path.join(bank_dir, target_name)
    if os.path.abspath(src_path) != os.path.abspath(target_path):
        if await aiopath.exists(target_path):
            await aioremove(target_path)
        await aiocopyfile(src_path, target_path)
    return target_path


def parse_season_episode(name: str) -> tuple[int | None, int | None]:
    """
    Extracts (season, episode) number from a media filename.
    Supports S02E05, 2x05, Episode 5, Ep 05, - 05, [05], etc.
    """
    clean = os.path.splitext(os.path.basename(name))[0]
    # 1. Standard S02E05 or s2e5
    m = re.search(r"[sS](\d{1,2})[\s._-]*[eE](\d{1,4})", clean)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 2. 2x05
    m = re.search(r"\b(\d{1,2})[xX](\d{1,4})\b", clean)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 3. Season X with or without episode
    s_num = None
    s_match = re.search(r"\b(?:season|s)[\s._-]*(\d{1,2})\b", clean, re.I)
    if s_match:
        s_num = int(s_match.group(1))

    # 4. Episode standalone
    m = re.search(r"\b(?:ep|episode|e)[\s._-]*(\d{1,4})\b", clean, re.I)
    if m:
        return s_num, int(m.group(1))

    # 5. Bracketed [05] or delimiter separated - 05 -
    m = re.search(r"(?:\[|\b|\s|_|-)(\d{2,4})(?:\]|\b|\s|_|-)", clean)
    if m:
        num = int(m.group(1))
        # Exclude common video resolutions and years
        if num not in {480, 576, 720, 1080, 2160} and not (1920 <= num <= 2040):
            return s_num, num

    # 6. Fallback any number
    nums = re.findall(r"\d+", clean)
    for num_str in reversed(nums):
        num = int(num_str)
        if num not in {480, 576, 720, 1080, 2160} and not (1920 <= num <= 2040):
            return s_num, num

    return s_num, None


async def list_audio_bank(user_id: int | str) -> dict:
    """Lists all audio/sub streams stored in the user's VPS audio bank."""
    bank_dir = get_audio_bank_dir(user_id)
    if not await aiopath.isdir(bank_dir):
        return {
            "user_id": str(user_id),
            "bank_dir": bank_dir,
            "total_files": 0,
            "total_size": 0,
            "languages": {},
            "files": [],
        }

    files = []
    languages = {}
    total_size = 0

    entries = sorted(await aiolistdir(bank_dir))
    for name in entries:
        full_path = os.path.join(bank_dir, name)
        if not await aiopath.isfile(full_path):
            continue
        size = await aiopath.getsize(full_path)
        total_size += size

        # Detect language prefix "{Language} - {filename}"
        lang = "Unknown"
        if " - " in name:
            candidate = name.split(" - ", 1)[0].strip()
            full_l, _ = resolve_full_language_name(candidate)
            lang = full_l
        else:
            full_l, _ = resolve_full_language_name("", name)
            lang = full_l

        s_num, ep_num = parse_season_episode(name.split(" - ", 1)[-1] if " - " in name else name)
        ext = os.path.splitext(name)[1].lower()

        item = {
            "name": name,
            "path": full_path,
            "size": size,
            "language": lang,
            "ext": ext,
            "season": s_num,
            "episode": ep_num,
        }
        files.append(item)
        languages.setdefault(lang, []).append(item)

    return {
        "user_id": str(user_id),
        "bank_dir": bank_dir,
        "total_files": len(files),
        "total_size": total_size,
        "languages": languages,
        "files": files,
    }


async def clear_audio_bank(user_id: int | str, language: str = None) -> tuple[int, int]:
    """
    Clears audio bank files for the user.
    If language is given, deletes only tracks of that language.
    Returns (deleted_count, freed_bytes).
    """
    bank_dir = get_audio_bank_dir(user_id)
    if not await aiopath.isdir(bank_dir):
        return 0, 0

    deleted_count = 0
    freed_bytes = 0

    if not language:
        for name in await aiolistdir(bank_dir):
            p = os.path.join(bank_dir, name)
            if await aiopath.isfile(p):
                freed_bytes += await aiopath.getsize(p)
                await aioremove(p)
                deleted_count += 1
        return deleted_count, freed_bytes

    lang_lower = language.strip().lower()
    for name in await aiolistdir(bank_dir):
        if name.lower().startswith(f"{lang_lower} - "):
            p = os.path.join(bank_dir, name)
            if await aiopath.isfile(p):
                freed_bytes += await aiopath.getsize(p)
                await aioremove(p)
                deleted_count += 1

    return deleted_count, freed_bytes


def _extract_title_tokens(name: str) -> set[str]:
    clean = re.sub(r"\[.*?\]|\(.*?\)", " ", name)
    clean = re.sub(
        r"\b(1080p|720p|480p|2160p|4k|bluray|brrip|web-dl|webrip|hevc|x265|x264|aac|ddp|dts|remux|pahe|amzn|breeze|sub|dub|multi)\b",
        " ",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"[_\W]+", " ", clean)
    tokens = set(clean.lower().split())
    tokens.difference_update({
        "season", "episode", "s1", "s01", "s2", "s02", "e1", "e01", "e2", "e02",
        "tamil", "hindi", "telugu", "english", "japanese", "the", "a", "an", "and",
        "or", "to", "in", "of", "mkv", "mp4", "eac3", "aac", "part", "vol"
    })
    return tokens


def match_videos_with_bank(video_paths: list[str], bank_files: list[dict], selected_languages: list[str] = None) -> dict:
    """
    Matches target videos (e.g. 12 season episodes) against extracted bank audio files.
    Pairs by SxxExx or Episode number, falling back to natural order index if counts match.
    """
    filtered_bank = bank_files
    if selected_languages:
        allowed = {s.strip().lower() for s in selected_languages if s.strip()}
        filtered_bank = [f for f in bank_files if f.get("language", "").lower() in allowed]

    # Group bank files by language
    bank_by_lang = {}
    for f in filtered_bank:
        bank_by_lang.setdefault(f.get("language", "Unknown"), []).append(f)

    # Sort video paths naturally
    sorted_videos = sorted(video_paths, key=lambda p: os.path.basename(p).lower())
    matched_results = []
    matched_episodes_count = 0

    for idx, v_path in enumerate(sorted_videos):
        v_name = os.path.basename(v_path)
        v_s, v_ep = parse_season_episode(v_name)
        v_tokens = _extract_title_tokens(v_name)
        track_matches = []

        for lang, lang_files in bank_by_lang.items():
            best_match = None
            match_type = "none"

            # 1. Exact Season + Episode Match (prioritizing title overlap)
            if v_s is not None and v_ep is not None:
                for af in lang_files:
                    if af.get("season") == v_s and af.get("episode") == v_ep:
                        af_tokens = _extract_title_tokens(af.get("name", ""))
                        has_overlap = bool(v_tokens & af_tokens)
                        if has_overlap or not best_match:
                            best_match = af
                            match_type = "exact_se"
                            if has_overlap:
                                break

            # 2. Episode number match
            if not best_match and v_ep is not None:
                for af in lang_files:
                    if af.get("episode") == v_ep:
                        af_tokens = _extract_title_tokens(af.get("name", ""))
                        has_overlap = bool(v_tokens & af_tokens)
                        if has_overlap or not best_match:
                            best_match = af
                            match_type = "episode_title" if has_overlap else "episode_only"
                            if has_overlap:
                                break

            # 3. Title token match when single bank file exists for this language
            if not best_match and len(lang_files) == 1:
                af = lang_files[0]
                af_tokens = _extract_title_tokens(af.get("name", ""))
                if bool(v_tokens & af_tokens) or not af_tokens:
                    best_match = af
                    match_type = "title_single"

            # 4. 1:1 Index order fallback when counts match
            if not best_match and len(lang_files) == len(sorted_videos):
                best_match = lang_files[idx]
                match_type = "index_order"

            if best_match:
                track_matches.append({
                    "language": lang,
                    "audio_name": best_match["name"],
                    "audio_path": best_match["path"],
                    "match_type": match_type,
                    "size": best_match["size"],
                    "delay_ms": 0,
                })

        is_matched = len(track_matches) > 0
        if is_matched:
            matched_episodes_count += 1

        matched_results.append({
            "index": idx + 1,
            "video_path": v_path,
            "video_name": v_name,
            "season": v_s,
            "episode": v_ep,
            "audio_tracks": track_matches,
            "status": "matched" if is_matched else "unmatched",
        })

    return {
        "matched": matched_results,
        "total_episodes": len(sorted_videos),
        "matched_episodes": matched_episodes_count,
        "unmatched_episodes": len(sorted_videos) - matched_episodes_count,
        "available_languages": list(bank_by_lang.keys()),
    }


def make_vt_studio_token(task_id: str, user_id: int | str) -> str:
    """Generates an HMAC authentication token for web studio access."""
    secret = (Config.BOT_TOKEN or "starfall_vt_secret").encode()
    msg = f"{task_id}:{user_id}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:24]


def verify_vt_studio_token(task_id: str, token_or_user: str | int, token_optional: str | int | None = None) -> bool:
    """
    Validates web studio HMAC authentication token.
    Supports:
      verify_vt_studio_token(task_id, token) -> validates token against session file
      verify_vt_studio_token(task_id, token, user_id) -> validates token against user_id
      verify_vt_studio_token(task_id, user_id, token) -> validates token against user_id
    """
    if not task_id or not token_or_user:
        return False

    if token_optional is not None:
        if isinstance(token_or_user, (int, str)) and (str(token_or_user).isdigit() or isinstance(token_or_user, int)):
            user_id = token_or_user
            token = token_optional
        else:
            token = token_or_user
            user_id = token_optional
        expected = make_vt_studio_token(task_id, user_id)
        return hmac.compare_digest(expected, str(token).strip())

    token = str(token_or_user).strip()
    session_file = os.path.join(get_web_sessions_dir(), f"{task_id}.json")
    if os.path.isfile(session_file):
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                sess = json.load(f)
            if sess.get("token") and hmac.compare_digest(sess["token"], token):
                return True
            if sess.get("user_id"):
                expected = make_vt_studio_token(task_id, sess["user_id"])
                return hmac.compare_digest(expected, token)
        except Exception:
            pass
    return False


async def save_web_session(task_id: str, data: dict):
    """Writes session info to disk so the Web FastAPI process can access bot task state."""
    sessions_dir = get_web_sessions_dir()
    await aiomakedirs(sessions_dir, exist_ok=True)
    session_file = os.path.join(sessions_dir, f"{task_id}.json")
    async with open_async_tmp(session_file) as f:
        await f.write(json.dumps(data, indent=2))


async def load_web_session(task_id: str) -> dict | None:
    """Loads session info from disk for the Web FastAPI process."""
    session_file = os.path.join(get_web_sessions_dir(), f"{task_id}.json")
    if not await aiopath.isfile(session_file):
        return None
    try:
        try:
            from aiofiles import open as aiopen
            async with aiopen(session_file, "r", encoding="utf-8") as f:
                return json.loads(await f.read())
        except ImportError:
            with open(session_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        LOGGER.warning(f"Error reading web session {task_id}: {e}")
        return None


async def delete_web_session(task_id: str):
    """Cleans up task session and IPC files."""
    sessions_dir = get_web_sessions_dir()
    for suffix in [".json", ".cmd.json", ".submit.json"]:
        p = os.path.join(sessions_dir, f"{task_id}{suffix}")
        if await aiopath.exists(p):
            with suppress(Exception):
                await aioremove(p)


async def write_web_cmd(task_id: str, cmd_name: str, payload: dict = None):
    """Web process writes an IPC command to the bot (e.g. preview, clear bank)."""
    sessions_dir = get_web_sessions_dir()
    await aiomakedirs(sessions_dir, exist_ok=True)
    cmd_file = os.path.join(sessions_dir, f"{task_id}.cmd.json")
    data = {"cmd": cmd_name, "payload": payload or {}, "timestamp": time()}
    async with open_async_tmp(cmd_file) as f:
        await f.write(json.dumps(data))


async def read_and_clear_web_cmd(task_id: str) -> dict | None:
    """Bot process checks and consumes pending web commands."""
    cmd_file = os.path.join(get_web_sessions_dir(), f"{task_id}.cmd.json")
    if not await aiopath.isfile(cmd_file):
        return None
    try:
        try:
            from aiofiles import open as aiopen
            async with aiopen(cmd_file, "r", encoding="utf-8") as f:
                data = json.loads(await f.read())
        except ImportError:
            with open(cmd_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        await aioremove(cmd_file)
        return data
    except Exception:
        return None


async def write_web_submission(task_id: str, submission: dict):
    """Web process writes the final user-approved mux plan."""
    sessions_dir = get_web_sessions_dir()
    await aiomakedirs(sessions_dir, exist_ok=True)
    submit_file = os.path.join(sessions_dir, f"{task_id}.submit.json")
    async with open_async_tmp(submit_file) as f:
        await f.write(json.dumps(submission, indent=2))


async def read_and_clear_web_submission(task_id: str) -> dict | None:
    """Bot process reads and consumes the submitted web mux plan."""
    submit_file = os.path.join(get_web_sessions_dir(), f"{task_id}.submit.json")
    if not await aiopath.isfile(submit_file):
        return None
    try:
        try:
            from aiofiles import open as aiopen
            async with aiopen(submit_file, "r", encoding="utf-8") as f:
                data = json.loads(await f.read())
        except ImportError:
            with open(submit_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        await aioremove(submit_file)
        return data
    except Exception:
        return None


class open_async_tmp:
    def __init__(self, target_path: str):
        self.target_path = target_path
        self.tmp_path = f"{target_path}.tmp"
        self._cm = None
        self._file = None

    async def __aenter__(self):
        try:
            from aiofiles import open as aiopen
            self._cm = aiopen(self.tmp_path, "w", encoding="utf-8")
            self._file = await self._cm.__aenter__()
            return self._file
        except ImportError:
            self._file = open(self.tmp_path, "w", encoding="utf-8")
            class _SyncWrapper:
                def __init__(self, f): self._f = f
                async def write(self, s): self._f.write(s)
            return _SyncWrapper(self._file)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._cm:
            await self._cm.__aexit__(exc_type, exc_val, exc_tb)
        elif self._file:
            self._file.close()
        if exc_type is None:
            try:
                from aiofiles.os import rename as aiorename
                if await aiopath.exists(self.target_path):
                    with suppress(Exception):
                        await aioremove(self.target_path)
                await aiorename(self.tmp_path, self.target_path)
            except Exception:
                import os
                if os.path.exists(self.target_path):
                    with suppress(Exception):
                        os.remove(self.target_path)
                os.replace(self.tmp_path, self.target_path)
        else:
            try:
                if await aiopath.exists(self.tmp_path):
                    with suppress(Exception):
                        await aioremove(self.tmp_path)
            except Exception:
                if os.path.exists(self.tmp_path):
                    with suppress(Exception):
                        os.remove(self.tmp_path)
