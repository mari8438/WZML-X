#!/usr/bin/env python3
"""
Starfall v1.6 High-Throughput Lossless MKV Muxing & Season Merge Engine (`mux_engine.py`)
=========================================================================================
Target Workspace: 16 GB tmpfs RAM Disk (`/dev/shm/starfall_mux`)
Target Hardware : 16 vCPU Cores, 32 GB Physical RAM, Ubuntu 24.04 LTS

Key Enhancements over Starfall v1.2/v1.6:
1. Strict `BluRay` Naming Enforcement:
   - Eliminates the legacy `quality: str = "BDRip"` default.
   - Sanitizes any incoming `BDRip`, `BD-Rip`, `BRRip`, `Blu-Ray`, or `BDRemux` token to
     **`BluRay`** across both single-episode and full-season merged filenames and track tags.
2. Non-Blocking Async Subprocess Execution + Multi-Threaded `mkvmerge`:
   - Passes `--ui-language en_US --threads 4` to `mkvmerge` so 4 concurrent mux workers
     saturate the 16 vCPUs without thread thrashing.
   - Provides both async (`inspect_file_async`, `probe_video_properties_async`,
     `generate_16_9_thumbnail_async`) and drop-in compatible synchronous methods.
3. Linear Drift (`PAL_DRIFT`) & Constant Offset (`CONSTANT`) Acoustic Sync Support:
   - Passes `--sync 0:{delay_ms}` when `speed_ratio == 1.0`, and automatically formats
     `--sync 0:{delay_ms},{speed_ratio}` when `PAL_DRIFT` (`abs(speed_ratio - 1.0) > 1e-5`)
     is detected by the 4-Anchor GCC-PHAT engine.
4. Dynamic Periodic Promo Subtitle & Full Container/Track Branding:
   - Generates an ASS subtitle track displaying `Join our Telegram channel [@Anime_Starfall🥰]`
     for 10 seconds every 5 minutes (300s) across the exact probed duration.
   - Preserves English subtitle tracks (renaming them to `[@Anime_Starfall🥰] - English`),
     drops Japanese dialogue (`-A` on source video), and attaches the Season Poster as `cover.jpg`.
5. Full-Season Merged Video Generation (`mkvmerge --append-mode file`):
   - Appends all muxed episodes in sequential order with `--append-mode file` and
     `--generate-chapters when-appending`, auto-splitting at `3950M` (`TG_MAX_USER_LIMIT_MIB`)
     for `@Hsin_The_Moon_Fox` Premium User upload.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        RAM_MUX_DIR,
        TG_MAX_USER_LIMIT_MIB,
        TG_SPLIT_THRESHOLD_MIB,
        USER_SETTINGS,
    )
else:
    from .config import (
        RAM_MUX_DIR,
        TG_MAX_USER_LIMIT_MIB,
        TG_SPLIT_THRESHOLD_MIB,
        USER_SETTINGS,
    )

LOGGER = logging.getLogger("StarfallMuxEngine")

BLURAY_SANITIZE_RE = re.compile(
    r"\b(?:BDRip|BD-Rip|BRRip|Blu-Ray|BDRemux)\b",
    re.IGNORECASE,
)


def sanitize_bluray_tag(text: str) -> str:
    """Strictly replace any occurrence of BDRip, BD-Rip, or BRRip with `BluRay`."""
    if not text:
        return "BluRay"
    cleaned = BLURAY_SANITIZE_RE.sub("BluRay", text)
    if cleaned.strip().upper() == "BD":
        return "BluRay"
    return cleaned


def format_ass_time(seconds: float) -> str:
    """Format seconds into ASS timestamp format H:MM:SS.cs."""
    seconds = max(0.0, float(seconds))
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    csecs = int(round((seconds - int(seconds)) * 100))
    if csecs >= 100:
        csecs = 0
        secs += 1
        if secs >= 60:
            secs = 0
            mins += 1
            if mins >= 60:
                mins = 0
                hrs += 1
    return f"{hrs}:{mins:02d}:{secs:02d}.{csecs:02d}"


class MuxEngine:
    """
    MKVToolNix Lossless Stream Muxer, Periodic Subtitle Injector & Full-Season Merger.
    Executes entirely inside `/dev/shm/starfall_mux` (16 GB tmpfs) for zero NVMe write wear.
    """

    def __init__(self, workspace_dir: str = RAM_MUX_DIR, threads_per_job: int = 4):
        self.workspace_dir = workspace_dir
        self.threads_per_job = threads_per_job
        os.makedirs(self.workspace_dir, exist_ok=True)

    @staticmethod
    def inspect_file(filepath: str) -> Dict[str, Any]:
        """Run `mkvmerge -J` synchronously to inspect track IDs, codecs, and languages."""
        cmd = ["mkvmerge", "-J", filepath]
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            return json.loads(out)
        except Exception as e:
            LOGGER.error(f"mkvmerge inspect failed on {filepath}: {e}")
            return {}

    @staticmethod
    async def inspect_file_async(filepath: str) -> Dict[str, Any]:
        """Non-blocking `mkvmerge -J` track inspection."""
        cmd = ["mkvmerge", "-J", filepath]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and stdout:
                return json.loads(stdout.decode("utf-8", errors="replace"))
        except Exception as e:
            LOGGER.error(f"Async mkvmerge inspect failed on {filepath}: {e}")
        return {}

    @staticmethod
    def probe_video_properties(filepath: str) -> Dict[str, Any]:
        """
        Probe exact video duration (seconds) and dimensions (`1920x1080`) via `ffprobe`
        to eliminate the Telegram `00:00` video player duration bug.
        """
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-show_entries", "stream=width,height,duration,codec_type",
            "-of", "json",
            filepath,
        ]
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            dur = float(data.get("format", {}).get("duration") or 0.0)
            width = 1920
            height = 1080
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" or ("width" in s and "height" in s):
                    width = int(s.get("width") or 1920)
                    height = int(s.get("height") or 1080)
                    if not dur and s.get("duration"):
                        dur = float(s["duration"])
                    break
            return {
                "duration": dur,
                "duration_sec": max(1, int(round(dur))),
                "width": width,
                "height": height,
            }
        except Exception as e:
            LOGGER.error(f"Failed to probe video properties for {filepath}: {e}")
            return {"duration": 1440.0, "duration_sec": 1440, "width": 1920, "height": 1080}

    @classmethod
    async def probe_video_properties_async(cls, filepath: str) -> Dict[str, Any]:
        """Non-blocking wrapper for `probe_video_properties`."""
        return await asyncio.to_thread(cls.probe_video_properties, filepath)

    @staticmethod
    def generate_16_9_thumbnail(
        video_path: str,
        output_thumb_path: str,
        duration_sec: float,
    ) -> Optional[str]:
        """
        Fallback 16:9 widescreen landscape thumbnail generator (`1280x720`) at 20% timestamp
        when source channel message thumbnail (`msg.video.thumbs[-1]`) is not cached.
        """
        if os.path.exists(output_thumb_path) and os.path.getsize(output_thumb_path) > 1000:
            return output_thumb_path

        os.makedirs(os.path.dirname(output_thumb_path), exist_ok=True)
        t_sec = max(1.0, round((duration_sec or 1440.0) * 0.20, 2))
        cmd = [
            "ffmpeg",
            "-y",
            "-v", "error",
            "-ss", str(t_sec),
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-q:v", "2",
            output_thumb_path,
        ]
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(output_thumb_path) and os.path.getsize(output_thumb_path) > 1000:
                LOGGER.info(f"Generated 16:9 landscape thumbnail at {t_sec}s: {output_thumb_path}")
                return output_thumb_path
        except Exception as e:
            LOGGER.error(f"Failed to generate 16:9 thumbnail for {video_path}: {e}")
        return None

    def create_recurring_promo_subtitle(
        self,
        output_ass_path: str,
        duration_sec: float,
        show_duration: float = 10.0,
        gap_sec: float = 300.0,
    ) -> str:
        """
        Generate dynamic recurring ASS watermark subtitle track across the full video duration:
        `Join our Telegram channel [@Anime_Starfall🥰]` (10 seconds display, 5 minutes gap).
        """
        header = """[Script Info]
Title: Starfall Periodic Promo
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: StarfallPromo,Arial,42,&H00FFFFFF,&H000000FF,&H00111111,&H80000000,-1,0,0,0,100,100,0,0,1,2.5,1.5,8,20,20,25,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        total_dur = max(60.0, float(duration_sec or 1440.0))
        events: List[str] = []
        t = 0.0
        while t < total_dur:
            start_str = format_ass_time(t)
            end_str = format_ass_time(min(t + show_duration, total_dur))
            events.append(
                f"Dialogue: 0,{start_str},{end_str},StarfallPromo,,0,0,0,,"
                f"{{\\an8\\fad(400,600)}}Join our Telegram channel [@Anime_Starfall🥰]"
            )
            t += gap_sec

        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write(header + "\n".join(events) + "\n")
        return output_ass_path

    @staticmethod
    def format_output_filename(
        anime_name: str,
        season_num: int,
        episode_num: int,
        year: int = 2024,
        resolution: str = "1080p",
        bit: str = "10bit",
        ott: str = "CR",
        quality: str = "BluRay",
        codec: str = "x265",
        release_group: str = "Starfall",
    ) -> str:
        """
        Generate standardized Starfall v1.6 episode filename (strictly `BluRay`, never `BDRip`):
        `🄰🅂-[S01E01] {name} {year} {resolution} {bit} {ott} BluRay {codec} [Tamil-AAC 2.0] ESub ~ {release_group} ~ [@Anime_Starfall🥰].mkv`
        """
        clean_name = re.sub(r"[^\w\s-]", "", anime_name).strip()
        safe_quality = sanitize_bluray_tag(quality)
        safe_codec = "x265" if codec.upper() == "HEVC" else codec
        raw_filename = (
            f"🄰🅂-[S{season_num:02d}E{episode_num:02d}] {clean_name} {year} "
            f"{resolution} {bit} {ott} {safe_quality} {safe_codec} [Tamil-AAC 2.0] ESub ~ "
            f"{release_group} ~ [@Anime_Starfall🥰].mkv"
        )
        return sanitize_bluray_tag(raw_filename)

    async def mux_single_episode(
        self,
        video_path: str,
        tamil_audio_path: str,
        output_mkv_path: str,
        calculated_delay_ms: int = 0,
        speed_ratio: float = 1.0,
        poster_path: Optional[str] = None,
        release_group: str = "Starfall",
        title_tag: Optional[str] = None,
    ) -> bool:
        """
        Losslessly mux 1080p video + synced Tamil AAC + preserved English subtitles +
        periodic 5-minute promo subtitle + attached season poster in `/dev/shm/starfall_mux`.
        """
        if not os.path.exists(video_path) or not os.path.exists(tamil_audio_path):
            LOGGER.error(f"Source files missing: video={video_path}, audio={tamil_audio_path}")
            return False

        # Ensure output filename never contains BDRip
        output_mkv_path = os.path.join(
            os.path.dirname(output_mkv_path),
            sanitize_bluray_tag(os.path.basename(output_mkv_path)),
        )

        prop = await self.probe_video_properties_async(video_path)
        duration_sec = prop["duration"] or 1440.0

        # Generate dynamic recurring ASS promo subtitle in RAM workspace
        promo_sub_file = os.path.join(
            self.workspace_dir,
            f"promo_{os.getpid()}_{os.path.basename(output_mkv_path)}.ass",
        )
        await asyncio.to_thread(self.create_recurring_promo_subtitle, promo_sub_file, duration_sec)

        # Inspect source MKV tracks asynchronously
        meta = await self.inspect_file_async(video_path)
        tracks = meta.get("tracks", [])

        video_track_id = 0
        sub_track_ids: List[str] = []
        for tr in tracks:
            tr_type = tr.get("type")
            tr_id = tr.get("id", 0)
            if tr_type == "video":
                video_track_id = tr_id
            elif tr_type == "subtitles":
                # Keep English or undetermined subtitle tracks; drop signs-only if needed
                lang = str(tr.get("properties", {}).get("language", "eng")).lower()
                if lang in ("eng", "en", "und", ""):
                    sub_track_ids.append(str(tr_id))

        # Fallback: if no subtitle matched language filter, keep all subtitle tracks from source
        if not sub_track_ids:
            for tr in tracks:
                if tr.get("type") == "subtitles":
                    sub_track_ids.append(str(tr.get("id", 0)))

        container_title = USER_SETTINGS.get("METADATA_TITLE", "[@Anime_Starfall🥰]")
        video_track_name = USER_SETTINGS.get("VIDEO_METADATA", {}).get("title", "[@Anime_Starfall🥰]")
        audio_track_name = (
            USER_SETTINGS.get("AUDIO_METADATA", {})
            .get("title", "[@Anime_Starfall🥰] - {audiolang}")
            .format(audiolang="Tamil")
        )
        sub_track_name = (
            USER_SETTINGS.get("SUBTITLE_METADATA", {})
            .get("title", "[@Anime_Starfall🥰] - {sublang}")
            .format(sublang="English")
        )

        # Format mkvmerge `--sync` argument (supports both CONSTANT delay and PAL_DRIFT ratio)
        if abs(speed_ratio - 1.0) > 1e-5:
            sync_arg = f"0:{int(calculated_delay_ms)},{speed_ratio:.6f}"
        else:
            sync_arg = f"0:{int(calculated_delay_ms)}"

        cmd: List[str] = [
            "mkvmerge",
            "--ui-language", "en_US",
            "--threads", str(self.threads_per_job),
            "-o", output_mkv_path,
            "--title", container_title,
            "-d", str(video_track_id),
            "--track-name", f"{video_track_id}:{video_track_name}",
            "-A",  # Drop Japanese/original audio tracks from source video
        ]

        if sub_track_ids:
            cmd.extend(["-s", ",".join(sub_track_ids)])
            for sid in sub_track_ids:
                cmd.extend([
                    "--track-name", f"{sid}:{sub_track_name}",
                    "--language", f"{sid}:eng",
                ])
        else:
            cmd.append("-S")

        cmd.append(video_path)

        # Input #2: Synced Tamil AAC Audio Track (made default audio track)
        cmd.extend([
            "--language", "0:tam",
            "--track-name", f"0:{audio_track_name}",
            "--default-track-flag", "0:yes",
            "--sync", sync_arg,
            tamil_audio_path,
        ])

        # Input #3: Periodic 5-minute Channel Promo Subtitle Track
        cmd.extend([
            "--language", "0:eng",
            "--track-name", "0:Join our Telegram channel [@Anime_Starfall🥰]",
            "--default-track-flag", "0:yes",
            promo_sub_file,
        ])

        # Attachment: Official Season Poster as cover.jpg
        if poster_path and os.path.exists(poster_path):
            cmd.extend([
                "--attach-file", poster_path,
                "--attachment-mime-type", "image/jpeg",
                "--attachment-name", "cover.jpg",
            ])

        LOGGER.info(
            f"🔧 Executing lossless mkvmerge in RAM: {os.path.basename(output_mkv_path)} "
            f"(--sync {sync_arg})"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            # mkvmerge exit code 0 = success, 1 = warnings generated (still valid output), 2 = error
            if proc.returncode > 1:
                LOGGER.error(
                    f"mkvmerge failed (code {proc.returncode}): "
                    f"{stderr.decode(errors='replace')} | {stdout.decode(errors='replace')}"
                )
                return False

            if not os.path.exists(output_mkv_path) or os.path.getsize(output_mkv_path) == 0:
                LOGGER.error(f"Output file {output_mkv_path} missing or empty after muxing")
                return False

            size_mib = os.path.getsize(output_mkv_path) / (1024 ** 2)
            LOGGER.info(f"✅ Muxed {os.path.basename(output_mkv_path)} ({size_mib:.2f} MiB)")
            return True
        except Exception as e:
            LOGGER.error(f"Exception during mux_single_episode: {e}")
            return False
        finally:
            if os.path.exists(promo_sub_file):
                try:
                    os.remove(promo_sub_file)
                except Exception:
                    pass

    async def merge_season(
        self,
        episode_paths: List[str],
        output_season_path: str,
        split_size_mb: int = TG_MAX_USER_LIMIT_MIB,
        poster_path: Optional[str] = None,
    ) -> List[str]:
        """
        Merge all muxed episodes of a season into a Full-Season Merged Video using:
          `mkvmerge --append-mode file --generate-chapters when-appending --split size:3950M`
        Automatically enforces the `BluRay` filename rule and attaches `cover.jpg`.
        """
        valid_eps = [p for p in sorted(episode_paths) if os.path.exists(p)]
        if not valid_eps:
            return []

        output_season_path = os.path.join(
            os.path.dirname(output_season_path),
            sanitize_bluray_tag(os.path.basename(output_season_path)),
        )

        first_ep = valid_eps[0]
        rest_eps = valid_eps[1:]
        container_title = USER_SETTINGS.get("METADATA_TITLE", "[@Anime_Starfall🥰]")

        cmd: List[str] = [
            "mkvmerge",
            "--ui-language", "en_US",
            "--threads", str(self.threads_per_job),
            "-o", output_season_path,
            "--title", container_title,
            "--append-mode", "file",
            "--generate-chapters", "when-appending",
            "--split", f"size:{int(split_size_mb)}M",
        ]

        if poster_path and os.path.exists(poster_path):
            cmd.extend([
                "--attach-file", poster_path,
                "--attachment-mime-type", "image/jpeg",
                "--attachment-name", "cover.jpg",
            ])

        cmd.append(first_ep)
        for ep in rest_eps:
            cmd.extend(["+", "--no-attachments", ep])

        LOGGER.info(
            f"📦 Merging {len(valid_eps)} episodes via `mkvmerge --append-mode file` -> "
            f"{os.path.basename(output_season_path)} (split cap={split_size_mb} MiB)"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode > 1:
                LOGGER.error(
                    f"mkvmerge season merge failed (code {proc.returncode}): "
                    f"{stderr.decode(errors='replace')} | {stdout.decode(errors='replace')}"
                )
                return []

            out_dir = os.path.dirname(output_season_path)
            base_stem = os.path.splitext(os.path.basename(output_season_path))[0]

            created_files: List[str] = []
            if os.path.exists(output_season_path):
                created_files.append(output_season_path)
            else:
                for fn in sorted(os.listdir(out_dir)):
                    if fn.startswith(base_stem) and fn.endswith(".mkv"):
                        created_files.append(os.path.join(out_dir, fn))

            LOGGER.info(f"✅ Full-Season Merge complete: {len(created_files)} part(s) generated.")
            return created_files
        except Exception as e:
            LOGGER.error(f"Error merging season: {e}")
            return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample_fn = MuxEngine.format_output_filename(
        anime_name="Tomb Raider King",
        season_num=1,
        episode_num=1,
        year=2026,
        quality="BDRip",
        codec="HEVC",
        release_group="YURASUKA",
    )
    assert "BluRay" in sample_fn and "BDRip" not in sample_fn
    print("✅ MuxEngine verified:", sample_fn)