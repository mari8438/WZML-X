import asyncio
from contextlib import suppress
from os import path as ospath
from pathlib import Path
from sys import executable

from ... import LOGGER
from ...core.config_manager import Config
from .bot_utils import cmd_exec, sync_to_async
from .hstream_translation import HstreamTranslator, translate_subtitle

BRAND_CHANNEL = "@Anime_Starfall"
BRAND_TEXT = f"Join our Telegram channel [{BRAND_CHANNEL}]"


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remaining = seconds % 60
    return f"{hours}:{minutes:02d}:{remaining:05.2f}"


def write_starfall_brand_subtitle(path: str, duration: float) -> str:
    """Generate brand intro subtitle spanning the video duration."""
    end = _ass_time(max(duration, 1.0) + 1.0)
    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Starfall,Arial,52,&H00FFFFFF,&H00FF9E2D,1,3,1,2,40,40,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{end},Starfall,,0,0,0,,{{\\b1}}{BRAND_TEXT}
"""
    Path(path).write_text(content, encoding="utf-8")
    return path


async def extract_audio_wav(video_path: str, audio_path: str) -> str:
    """Extract 16kHz mono audio from video file for speech-to-text processing."""
    Path(audio_path).parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        audio_path,
    ]
    stdout, stderr, code = await cmd_exec(command)
    if code != 0 or not ospath.isfile(audio_path) or ospath.getsize(audio_path) == 0:
        raise RuntimeError(f"FFmpeg audio extraction failed: {stderr or stdout}")
    return audio_path


def _transcribe_offline_sync(audio_path: str, srt_path: str, model_name: str = "small") -> str:
    """Transcribe Japanese audio directly to English text using faster-whisper on CPU."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        LOGGER.error("faster-whisper is not installed. Please install faster-whisper>=1.0.0")
        raise RuntimeError("faster-whisper is required for offline subtitle generation.")

    LOGGER.info(f"Loading faster-whisper model '{model_name}' on CPU (int8)...")
    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        cpu_threads=2,
        num_workers=1,
    )

    LOGGER.info(f"Transcribing & translating Japanese audio from {audio_path}...")
    segments, _ = model.transcribe(
        audio_path,
        language="ja",
        task="translate",
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=600),
        beam_size=4,
    )

    import pysubs2

    subtitles = pysubs2.SSAFile()
    subtitles.styles["Default"].fontname = "Arial"
    subtitles.styles["Default"].fontsize = 44
    subtitles.styles["Default"].primarycolor = pysubs2.Color(255, 255, 255, 0)
    subtitles.styles["Default"].outlinecolor = pysubs2.Color(0, 0, 0, 0)

    count = 0
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        start_ms = int(segment.start * 1000)
        end_ms = int(segment.end * 1000)
        if end_ms <= start_ms:
            end_ms = start_ms + 1000
        event = pysubs2.SSAEvent(start=start_ms, end=end_ms, text=text)
        subtitles.events.append(event)
        count += 1

    subtitles.save(srt_path, encoding="utf-8")
    LOGGER.info(f"Whisper generated {count} English subtitle dialogue cues -> {srt_path}")
    return srt_path


async def generate_english_subtitles(audio_path: str, target_ass: str) -> str:
    """Run speech-to-text to produce English subtitle file."""
    model_name = getattr(Config, "WHISPER_MODEL", "small") or "small"
    return await sync_to_async(_transcribe_offline_sync, audio_path, target_ass, model_name)


async def translate_english_to_tamil(english_sub_path: str, tamil_sub_path: str, translator: HstreamTranslator) -> str:
    """Translate English subtitle tracks to Tamil."""
    if translator is None:
        raise RuntimeError("Tamil translator is not running.")
    return await translate_subtitle(english_sub_path, tamil_sub_path, translator)


async def generate_all_subtitles_for_video(
    video_source_path: str,
    output_dir: str,
    duration: float,
    translator: HstreamTranslator | None,
) -> tuple[list[tuple[str, str]], bool]:
    """
    Extract audio once, generate English subtitle, translate to Tamil,
    and prepare Starfall brand subtitle.
    Returns: ([(lang, file_path), ...], tamil_available: bool)
    """
    tracks: list[tuple[str, str]] = []
    audio_path = ospath.join(output_dir, "extracted_audio.wav")
    english_sub = ospath.join(output_dir, "subtitle_english.ass")
    tamil_sub = ospath.join(output_dir, "subtitle_tamil.ass")
    brand_sub = ospath.join(output_dir, "starfall_brand.ass")

    # 1. Starfall Brand subtitle
    await sync_to_async(write_starfall_brand_subtitle, brand_sub, duration)
    tracks.append(("brand", brand_sub))

    # 2. Extract audio
    try:
        await extract_audio_wav(video_source_path, audio_path)
    except Exception as error:
        LOGGER.error(f"Audio extraction failed for {video_source_path}: {error}")
        return tracks, False

    # 3. Speech-to-text: Japanese -> English Subtitles
    try:
        await generate_english_subtitles(audio_path, english_sub)
        if ospath.isfile(english_sub) and ospath.getsize(english_sub) > 0:
            tracks.append(("eng", english_sub))
    except Exception as error:
        LOGGER.error(f"English subtitle generation failed: {error}")

    # Remove temporary wav file to conserve disk space
    with suppress(OSError):
        Path(audio_path).unlink()

    # 4. English -> Tamil Translation
    tamil_available = False
    if any(lang == "eng" for lang, _ in tracks) and translator:
        try:
            await translate_english_to_tamil(english_sub, tamil_sub, translator)
            if ospath.isfile(tamil_sub) and ospath.getsize(tamil_sub) > 0:
                tracks.append(("tam", tamil_sub))
                tamil_available = True
        except Exception as error:
            LOGGER.warning(f"Tamil subtitle translation failed: {error}")

    return tracks, tamil_available
