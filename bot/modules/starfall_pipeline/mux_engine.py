import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from typing import Dict, Any, List, Optional

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        RAM_MUX_DIR,
        TG_SPLIT_THRESHOLD_MIB,
        TG_MAX_USER_LIMIT_MIB,
    )
else:
    from .config import (
        RAM_MUX_DIR,
        TG_SPLIT_THRESHOLD_MIB,
        TG_MAX_USER_LIMIT_MIB,
    )

LOGGER = logging.getLogger("StarfallMuxEngine")

INTRO_ASS_TEMPLATE = """[Script Info]
Title: Starfall Credit Banner
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: StarfallTag,Arial,42,&H00FFFFFF,&H000000FF,&H00111111,&H80000000,-1,0,0,0,100,100,0,0,1,2.5,1.5,8,20,20,25,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,StarfallTag,,0,0,0,,{\\an8\\fad(400,600)}[@Anime_Starfall🥰]
"""

class MuxEngine:
    """
    MKVToolNix Lossless Stream Muxer & Intro Subtitle Injector.
    Performs lossless stream copy with zero video re-encoding.
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

    def create_intro_subtitle(self, output_ass_path: str) -> str:
        """Create a 5-second intro subtitle banner in ASS format."""
        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write(INTRO_ASS_TEMPLATE)
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
        # Clean anime title for filename
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
        Losslessly mux video + synced Tamil audio + subtitles + intro subtitle + cover.
        - Video: copied
        - Audio: Japanese dropped, Tamil AAC added with delay
        - Subtitles: preserved from source
        - Intro subtitle: 5s [@Anime_Starfall🥰] injected
        - Cover art: season poster attached as cover.jpg
        """
        if not os.path.exists(video_path) or not os.path.exists(tamil_audio_path):
            LOGGER.error(f"Source files missing: video={video_path}, audio={tamil_audio_path}")
            return False

        # Prepare 5s intro sub banner in RAM
        intro_sub_file = os.path.join(self.workspace_dir, f"intro_{os.path.basename(output_mkv_path)}.ass")
        self.create_intro_subtitle(intro_sub_file)

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

        # Build mkvmerge command
        cmd = [
            "mkvmerge",
            "-o", output_mkv_path,
            # Video from source: keep only video and subtitles (drop Japanese audio -A)
            "-d", str(video_track_id),
            "-A",
        ]

        if sub_track_ids:
            cmd.extend(["-s", ",".join(sub_track_ids)])
        else:
            cmd.append("-S")

        cmd.append(video_path)

        # Tamil audio track
        cmd.extend([
            "--language", "0:tam",
            "--track-name", "0:Tamil AAC 2.0 [Starfall]",
            "--sync", f"0:{calculated_delay_ms}",
            tamil_audio_path
        ])

        # Intro subtitle credit banner
        cmd.extend([
            "--language", "0:eng",
            "--track-name", "0:[@Anime_Starfall🥰] Intro Credit",
            intro_sub_file
        ])

        # Cover poster attachment
        if poster_path and os.path.exists(poster_path):
            cmd.extend([
                "--attach-file", poster_path,
                "--attachment-mime-type", "image/jpeg",
                "--attachment-name", "cover.jpg"
            ])

        # Container title
        if title_tag:
            cmd.extend(["--title", title_tag])

        LOGGER.info(f"Executing mkvmerge for {os.path.basename(output_mkv_path)} (delay={calculated_delay_ms} ms)")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            # mkvmerge exit code 0 = ok, 1 = warnings (still succeeded), >= 2 = error
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
            if os.path.exists(intro_sub_file):
                try:
                    os.remove(intro_sub_file)
                except Exception:
                    pass

    async def merge_season(
        self,
        episode_paths: List[str],
        output_season_path: str,
        split_size_mb: int = TG_MAX_USER_LIMIT_MIB,
    ) -> List[str]:
        """
        Merge full season using `mkvmerge --append-mode file`.
        Generates automatic chapter markers and splits if > split_size_mb (3,950 MiB).
        Returns list of resulting merged parts.
        """
        if not episode_paths:
            return []

        sorted_eps = sorted(episode_paths)
        first_ep = sorted_eps[0]
        rest_eps = sorted_eps[1:]

        cmd = [
            "mkvmerge",
            "-o", output_season_path,
            "--generate-chapters", "when-appending",
            "--split", f"size:{split_size_mb}M",
            first_ep
        ]

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

            # Detect output files (single file or split files -001, -002, etc.)
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
