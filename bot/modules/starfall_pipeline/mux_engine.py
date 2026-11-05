import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from typing import Dict, Any, List, Optional, Tuple

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        RAM_MUX_DIR,
        TG_SPLIT_THRESHOLD_MIB,
        TG_MAX_USER_LIMIT_MIB,
        USER_SETTINGS,
    )
else:
    from .config import (
        RAM_MUX_DIR,
        TG_SPLIT_THRESHOLD_MIB,
        TG_MAX_USER_LIMIT_MIB,
        USER_SETTINGS,
    )

LOGGER = logging.getLogger("StarfallMuxEngine")

def format_ass_time(seconds: float) -> str:
    """Format seconds into ASS timestamp format H:MM:SS.cs"""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    csecs = int(round((seconds - int(seconds)) * 100))
    if csecs >= 100:
        csecs = 0
        secs += 1
    return f"{hrs}:{mins:02d}:{secs:02d}.{csecs:02d}"

class MuxEngine:
    """
    MKVToolNix Lossless Stream Muxer & Periodic Subtitle Injector.
    - Lossless stream copy with zero video re-encoding.
    - Generates dynamic recurring subtitle 'Join our Telegram channel [@Anime_Starfall🥰]' (10s show, 5m gap).
    - Probes exact video duration and dimensions (1920x1080).
    - Generates STRICT 16:9 landscape thumbnails at 20% timestamp.
    - Applies container & track metadata tags from user_settings.json.
    """

    def __init__(self, workspace_dir: str = RAM_MUX_DIR):
        self.workspace_dir = workspace_dir
        os.makedirs(self.workspace_dir, exist_ok=True)

    @staticmethod
    def inspect_file(filepath: str) -> Dict[str, Any]:
        """Run mkvmerge -J to get track and container metadata."""
        cmd = ["mkvmerge", "-J", filepath]
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            return json.loads(out)
        except Exception as e:
            LOGGER.error(f"mkvmerge inspect failed on {filepath}: {e}")
            return {}

    @staticmethod
    def probe_video_properties(filepath: str) -> Dict[str, Any]:
        """
        Probe exact video duration (seconds) and dimensions (width x height) via ffprobe.
        """
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-show_entries", "stream=width,height,duration",
            "-of", "json",
            filepath
        ]
        try:
            out = subprocess.check_output(cmd, text=True)
            data = json.loads(out)
            dur = float(data.get("format", {}).get("duration", 0))
            width = 1920
            height = 1080
            for s in data.get("streams", []):
                if "width" in s and "height" in s:
                    width = int(s["width"])
                    height = int(s["height"])
                    if not dur and "duration" in s:
                        dur = float(s["duration"])
                    break
            return {
                "duration": dur,
                "duration_sec": int(round(dur)),
                "width": width,
                "height": height,
            }
        except Exception as e:
            LOGGER.error(f"Failed to probe video properties for {filepath}: {e}")
            return {"duration": 0.0, "duration_sec": 0, "width": 1920, "height": 1080}

    @staticmethod
    def generate_16_9_thumbnail(video_path: str, output_thumb_path: str, duration_sec: float) -> Optional[str]:
        """
        Generate STRICT 16:9 landscape thumbnail (1280x720) at 20% timestamp.
        """
        os.makedirs(os.path.dirname(output_thumb_path), exist_ok=True)
        t_sec = max(1.0, round(duration_sec * 0.20, 2))
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(t_sec),
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-q:v", "2",
            output_thumb_path
        ]
        try:
            subprocess.check_call(cmd)
            if os.path.exists(output_thumb_path) and os.path.getsize(output_thumb_path) > 1000:
                LOGGER.info(f"Generated STRICT 16:9 thumbnail at {t_sec}s: {output_thumb_path}")
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
        Generate dynamic recurring subtitle:
        'Join our Telegram channel [@Anime_Starfall🥰]' (10s show, 5m gap) across full video duration.
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
        events = []
        t = 0.0
        while t < duration_sec:
            start_str = format_ass_time(t)
            end_str = format_ass_time(min(t + show_duration, duration_sec))
            events.append(
                f"Dialogue: 0,{start_str},{end_str},StarfallPromo,,0,0,0,,{{\\an8\\fad(400,600)}}Join our Telegram channel [@Anime_Starfall🥰]"
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
        quality: str = "BDRip",
        codec: str = "x265",
        release_group: str = "Starfall",
    ) -> str:
        """
        Generate standardized Starfall filename:
        🄰🅂-[S{season}E{episode}] {name} {year} {resolution} {bit} {ott} {quality} {codec} [Tamil-AAC 2.0] ESub ~ {group} ~ [@Anime_Starfall🥰].mkv
        """
        clean_name = re.sub(r"[^\w\s-]", "", anime_name).strip()
        filename = (
            f"🄰🅂-[S{season_num:02d}E{episode_num:02d}] {clean_name} {year} "
            f"{resolution} {bit} {ott} {quality} {codec} [Tamil-AAC 2.0] ESub ~ {release_group} ~ [@Anime_Starfall🥰].mkv"
        )
        return filename

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
        Losslessly mux video + synced Tamil audio + subtitles + periodic subtitle + cover.
        - Video: copied with track name [@Anime_Starfall🥰]
        - Audio: Japanese dropped, Tamil AAC added with delay and name [@Anime_Starfall🥰] - Tamil
        - Subtitles: preserved from source with name [@Anime_Starfall🥰] - English
        - Periodic subtitle: 'Join our Telegram channel [@Anime_Starfall🥰]' (10s show, 5m gap)
        - Cover art: season poster attached as cover.jpg
        - Metadata: Container title [@Anime_Starfall🥰] from user_settings.json
        """
        if not os.path.exists(video_path) or not os.path.exists(tamil_audio_path):
            LOGGER.error(f"Source files missing: video={video_path}, audio={tamil_audio_path}")
            return False

        # Probe duration and dimensions
        prop = self.probe_video_properties(video_path)
        duration_sec = prop["duration"] or 1440.0

        # Generate dynamic recurring subtitle
        promo_sub_file = os.path.join(self.workspace_dir, f"promo_{os.path.basename(output_mkv_path)}.ass")
        self.create_recurring_promo_subtitle(promo_sub_file, duration_sec)

        # Inspect source video tracks
        meta = self.inspect_file(video_path)
        tracks = meta.get("tracks", [])

        # Find video track id and subtitle track ids
        video_track_id = 0
        sub_track_ids = []
        for tr in tracks:
            tr_type = tr.get("type")
            tr_id = tr.get("id", 0)
            if tr_type == "video":
                video_track_id = tr_id
            elif tr_type == "subtitles":
                sub_track_ids.append(str(tr_id))

        # Metadata tags from user_settings.json
        container_title = USER_SETTINGS.get("METADATA_TITLE", "[@Anime_Starfall🥰]")
        video_track_name = USER_SETTINGS.get("VIDEO_METADATA", {}).get("title", "[@Anime_Starfall🥰]")
        audio_track_name = USER_SETTINGS.get("AUDIO_METADATA", {}).get("title", "[@Anime_Starfall🥰] - {audiolang}").format(audiolang="Tamil")
        sub_track_name = USER_SETTINGS.get("SUBTITLE_METADATA", {}).get("title", "[@Anime_Starfall🥰] - {sublang}").format(sublang="English")

        # Build mkvmerge command
        cmd = [
            "mkvmerge",
            "-o", output_mkv_path,
            "--title", container_title,
            # Video from source: keep only video and subtitles (drop Japanese audio -A)
            "-d", str(video_track_id),
            "--track-name", f"{video_track_id}:{video_track_name}",
            "-A",
        ]

        if sub_track_ids:
            cmd.extend(["-s", ",".join(sub_track_ids)])
            for sid in sub_track_ids:
                cmd.extend(["--track-name", f"{sid}:{sub_track_name}"])
        else:
            cmd.append("-S")

        cmd.append(video_path)

        # Synced Tamil audio track
        cmd.extend([
            "--language", "0:tam",
            "--track-name", f"0:{audio_track_name}",
            "--sync", f"0:{calculated_delay_ms}",
            tamil_audio_path
        ])

        # Periodic promo subtitle track
        cmd.extend([
            "--language", "0:eng",
            "--track-name", "0:Join our Telegram channel [@Anime_Starfall🥰]",
            promo_sub_file
        ])

        # Cover poster attachment
        if poster_path and os.path.exists(poster_path):
            cmd.extend([
                "--attach-file", poster_path,
                "--attachment-mime-type", "image/jpeg",
                "--attachment-name", "cover.jpg"
            ])

        LOGGER.info(f"Executing mkvmerge for {os.path.basename(output_mkv_path)} (delay={calculated_delay_ms} ms)")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode > 1:
                LOGGER.error(f"mkvmerge failed (code {proc.returncode}): {stderr.decode()} | {stdout.decode()}")
                return False

            if not os.path.exists(output_mkv_path):
                LOGGER.error(f"Output file {output_mkv_path} not found after muxing")
                return False

            size_mb = os.path.getsize(output_mkv_path) / (1024 ** 2)
            LOGGER.info(f"🎉 Successfully muxed {os.path.basename(output_mkv_path)} ({size_mb:.2f} MiB)")
            return True
        except Exception as e:
            LOGGER.error(f"Error during muxing: {e}")
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
        Merge full season using `mkvmerge --append-mode file`.
        Generates automatic chapter markers and splits if > split_size_mb (3,950 MiB).
        Applies container metadata tags and attaches season poster.
        """
        if not episode_paths:
            return []

        sorted_eps = sorted(episode_paths)
        first_ep = sorted_eps[0]
        rest_eps = sorted_eps[1:]

        container_title = USER_SETTINGS.get("METADATA_TITLE", "[@Anime_Starfall🥰]")

        cmd = [
            "mkvmerge",
            "-o", output_season_path,
            "--title", container_title,
            "--generate-chapters", "when-appending",
            "--split", f"size:{split_size_mb}M",
        ]

        # Attach cover poster
        if poster_path and os.path.exists(poster_path):
            cmd.extend([
                "--attach-file", poster_path,
                "--attachment-mime-type", "image/jpeg",
                "--attachment-name", "cover.jpg"
            ])

        cmd.append(first_ep)
        for ep in rest_eps:
            cmd.extend(["+", ep])

        LOGGER.info(f"Merging {len(sorted_eps)} episodes into {output_season_path}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode > 1:
                LOGGER.error(f"mkvmerge season merge failed: {stderr.decode()}")
                return []

            out_dir = os.path.dirname(output_season_path)
            base_name = os.path.splitext(os.path.basename(output_season_path))[0]

            created_files = []
            if os.path.exists(output_season_path):
                created_files.append(output_season_path)
            else:
                for f in sorted(os.listdir(out_dir)):
                    if f.startswith(base_name) and f.endswith(".mkv"):
                        created_files.append(os.path.join(out_dir, f))

            LOGGER.info(f"✅ Season merge completed: created {len(created_files)} parts.")
            return created_files
        except Exception as e:
            LOGGER.error(f"Error merging season: {e}")
            return []

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== MuxEngine Ready ===")
