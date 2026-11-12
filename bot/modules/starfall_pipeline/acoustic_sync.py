import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import scipy.signal

if __package__ is None or __package__ == "":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent.parent))
    from bot.modules.starfall_pipeline.config import (
        SAMPLE_RATE,
        WINDOW_SEC,
        MAX_LAG_SEC,
        ANCHOR_POSITIONS,
        MIN_CONFIDENCE_PSR,
    )
else:
    from .config import (
        SAMPLE_RATE,
        WINDOW_SEC,
        MAX_LAG_SEC,
        ANCHOR_POSITIONS,
        MIN_CONFIDENCE_PSR,
    )

LOGGER = logging.getLogger("StarfallAcousticSync")


class AcousticSyncEngine:
    """
    Autonomous Adaptive Multi-Anchor GCC-PHAT Acoustic Delay Engine.
    Features:
    - Adaptive anchor mesh covering 0% to 100% (Prologue, Opening, Part A, Midpoint, Part B, Climax).
    - Chapter boundary inspection & sub-second jump point pinpointing.
    - Autonomous audio healing (sponsor/eyecatch gap trimming & silence padding).
    - Strict PAL drift validation (preventing false-positive pitch/speed distortions).
    - Mid/Side spatial vocal stripping and streaming float32 PCM in memory.
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        window_sec: float = WINDOW_SEC,
        max_lag_sec: float = MAX_LAG_SEC,
        anchors: Tuple[float, ...] = ANCHOR_POSITIONS,
        min_psr: float = MIN_CONFIDENCE_PSR,
    ):
        self.sample_rate = sample_rate
        self.window_sec = window_sec
        self.max_lag_sec = max_lag_sec
        self.anchors = anchors
        self.min_psr = min_psr

    @staticmethod
    def get_duration_sec(media_path: str) -> float:
        """Get file duration in seconds via ffprobe."""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_path
        ]
        try:
            out = subprocess.check_output(cmd, text=True).strip()
            return float(out)
        except Exception as e:
            LOGGER.error(f"Failed to probe duration for {media_path}: {e}")
            return 0.0

    @staticmethod
    def get_chapters(media_path: str) -> List[Dict[str, Any]]:
        """Extract chapter entries from video container."""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_chapters",
            "-of", "json",
            media_path
        ]
        try:
            out = subprocess.check_output(cmd, text=True)
            return json.loads(out).get("chapters", [])
        except Exception:
            return []

    @staticmethod
    def get_japanese_audio_index(video_path: str) -> int:
        """Probe stream index for Japanese audio track, defaulting to first audio stream."""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=index:stream_tags=language",
            "-select_streams", "a",
            "-of", "json",
            video_path
        ]
        try:
            out = subprocess.check_output(cmd, text=True)
            streams = json.loads(out).get("streams", [])
            for s in streams:
                lang = s.get("tags", {}).get("language", "").lower()
                if lang in ("jpn", "ja", "japanese"):
                    return int(s.get("index"))
            if streams:
                return int(streams[0].get("index"))
        except Exception:
            pass
        return 1

    async def extract_audio_window(
        self,
        media_path: str,
        start_sec: float,
        duration_sec: float,
        audio_stream_idx: Optional[int] = None,
    ) -> Optional[np.ndarray]:
        """
        Extract float32 stereo PCM window via FFmpeg pipe (zero disk I/O).
        Returns numpy array shape (N, 2).
        """
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start_sec:.3f}",
            "-t", f"{duration_sec:.3f}",
            "-i", media_path,
        ]
        if audio_stream_idx is not None:
            cmd.extend(["-map", f"0:{audio_stream_idx}"])
        else:
            cmd.extend(["-vn", "-sn", "-dn"])

        cmd.extend([
            "-ac", "2",
            "-ar", str(self.sample_rate),
            "-f", "f32le",
            "pipe:1"
        ])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            raw_bytes, stderr = await proc.communicate()
            if proc.returncode != 0 or not raw_bytes:
                LOGGER.error(f"FFmpeg window extract failed for {media_path} at {start_sec:.1f}s: {stderr.decode()}")
                return None

            pcm = np.frombuffer(raw_bytes, dtype=np.float32)
            if len(pcm) % 2 != 0:
                pcm = pcm[:-1]
            return pcm.reshape(-1, 2)
        except Exception as e:
            LOGGER.error(f"Exception extracting audio window: {e}")
            return None

    def strip_vocals(self, stereo_audio: np.ndarray) -> np.ndarray:
        """
        1. Mid/Side Spatial Vocal Stripping:
           Side(t) = 0.5 * Left(t) - 0.5 * Right(t)
        2. Dual-Mono Fallback:
           If Side energy < 1e-4, use bandstop filter rejecting 320-3100 Hz formants.
        """
        left = stereo_audio[:, 0]
        right = stereo_audio[:, 1]
        side = 0.5 * (left - right)
        sigma_side = float(np.std(side))

        if sigma_side >= 1e-4:
            return side

        # Dual-mono fallback: center sum + vocal formant rejection
        center = 0.5 * (left + right)
        try:
            sos = scipy.signal.butter(4, [320, 3100], btype="bandstop", fs=self.sample_rate, output="sos")
            filtered = scipy.signal.sosfilt(sos, center)
            return filtered
        except Exception:
            return center

    def calculate_gcc_phat(
        self,
        ref_signal: np.ndarray,
        target_signal: np.ndarray,
    ) -> Tuple[float, float]:
        """
        Compute GCC-PHAT cross-correlation between reference (Japanese audio)
        and target (Tamil audio).
        Returns:
            delay_ms: positive if target audio starts later than reference
            psr: Peak-to-Sidelobe Ratio confidence score
        """
        n_ref = len(ref_signal)
        n_tgt = len(target_signal)
        min_len = min(n_ref, n_tgt)
        if min_len < self.sample_rate:
            return 0.0, 0.0

        x = ref_signal[:min_len]
        y = target_signal[:min_len]

        # Fast FFT length
        n_fft = scipy.fft.next_fast_len(2 * min_len - 1)
        X = np.fft.rfft(x, n_fft)
        Y = np.fft.rfft(y, n_fft)

        # Cross power spectrum with phase transform (PHAT)
        R = Y * np.conj(X)
        P = R / (np.abs(R) + 1e-12)
        cc = np.fft.irfft(P, n_fft)
        cc = np.fft.fftshift(cc)

        # Zero lag index in shifted correlation
        mid = len(cc) // 2
        max_samples = int(self.max_lag_sec * self.sample_rate)
        search_start = max(0, mid - max_samples)
        search_end = min(len(cc), mid + max_samples)

        window_cc = cc[search_start:search_end]
        if len(window_cc) == 0:
            return 0.0, 0.0

        local_peak_idx = int(np.argmax(window_cc))
        global_peak_idx = search_start + local_peak_idx
        lag_samples = global_peak_idx - mid

        # 3-point parabolic peak interpolation
        refined_lag_samples = float(lag_samples)
        if 0 < global_peak_idx < len(cc) - 1:
            alpha = float(cc[global_peak_idx - 1])
            beta = float(cc[global_peak_idx])
            gamma = float(cc[global_peak_idx + 1])
            denom = 2.0 * (2.0 * beta - alpha - gamma)
            if abs(denom) > 1e-9:
                delta = (gamma - alpha) / denom
                refined_lag_samples += delta

        delay_ms = (refined_lag_samples / self.sample_rate) * 1000.0

        # Peak-to-Sidelobe Ratio (PSR)
        peak_guard = int(0.05 * self.sample_rate)
        sidelobes = np.concatenate([
            window_cc[:max(0, local_peak_idx - peak_guard)],
            window_cc[min(len(window_cc), local_peak_idx + peak_guard):]
        ])

        if len(sidelobes) > 10:
            mu = float(np.mean(sidelobes))
            sigma = float(np.std(sidelobes))
            peak_val = float(cc[global_peak_idx])
            psr = (peak_val - mu) / (sigma + 1e-12)
        else:
            psr = 0.0

        return delay_ms, psr

    async def find_jump_point(
        self,
        ref_video_path: str,
        target_audio_path: str,
        t_start: float,
        t_end: float,
        d_early: float,
        d_late: float,
        jap_stream_idx: int,
        chapters: Optional[List[Dict[str, Any]]] = None,
    ) -> float:
        """
        Pinpoints the exact timestamp where delay switches from d_early to d_late.
        First tests chapter boundaries falling between t_start and t_end.
        Falls back to fast binary search.
        """
        # 1. Check chapter boundaries between t_start and t_end
        if chapters:
            for ch in chapters:
                ch_end = float(ch.get("end_time", 0))
                if t_start <= ch_end <= t_end and ch_end > 10.0:
                    t_pre = max(0.0, ch_end - 15.0)
                    t_post = ch_end + 1.0
                    ref_pre = await self.extract_audio_window(ref_video_path, t_pre, 14.0, jap_stream_idx)
                    tgt_pre = await self.extract_audio_window(target_audio_path, t_pre, 14.0)
                    ref_post = await self.extract_audio_window(ref_video_path, t_post, 14.0, jap_stream_idx)
                    tgt_post = await self.extract_audio_window(target_audio_path, t_post, 14.0)

                    if ref_pre is not None and tgt_pre is not None and ref_post is not None and tgt_post is not None:
                        d_pre, _ = self.calculate_gcc_phat(self.strip_vocals(ref_pre), self.strip_vocals(tgt_pre))
                        d_post, _ = self.calculate_gcc_phat(self.strip_vocals(ref_post), self.strip_vocals(tgt_post))
                        if abs(d_pre - d_early) < abs(d_pre - d_late) and abs(d_post - d_late) < abs(d_post - d_early):
                            LOGGER.info(f"📍 Jump point confirmed at chapter boundary '{ch.get('tags',{}).get('title','')}' ({ch_end:.2f}s)")
                            return ch_end

        # 2. Binary search fallback
        low = t_start
        high = t_end
        for _ in range(6):
            mid = (low + high) / 2.0
            ref_mid = await self.extract_audio_window(ref_video_path, mid, 14.0, jap_stream_idx)
            tgt_mid = await self.extract_audio_window(target_audio_path, mid, 14.0)
            if ref_mid is None or tgt_mid is None:
                break
            d_mid, _ = self.calculate_gcc_phat(self.strip_vocals(ref_mid), self.strip_vocals(tgt_mid))
            if abs(d_mid - d_early) < abs(d_mid - d_late):
                low = mid
            else:
                high = mid
        return round((low + high) / 2.0, 2)

    async def heal_audio(
        self,
        target_audio_path: str,
        t_jump: float,
        gap_sec: float,
    ) -> Optional[str]:
        """
        Heals audio with a step jump:
        - If gap_sec > 0: trims extra sponsor/eyecatch segment [t_jump .. t_jump + gap_sec].
        - If gap_sec < 0: pads missing segment with silence at t_jump.
        Returns path to the healed audio file.
        """
        healed_dir = "/dev/shm/starfall_healed" if os.path.exists("/dev/shm") else "/home/administrator/starfall/data/tmp_healed"
        os.makedirs(healed_dir, exist_ok=True)

        base_name = os.path.basename(target_audio_path)
        ext = os.path.splitext(base_name)[1].lower() or ".opus"
        healed_file = os.path.join(healed_dir, f"healed_{os.getpid()}_{base_name}")

        if ext == ".opus":
            codec_args = ["-c:a", "libopus", "-b:a", "128k", "-vbr", "on"]
        elif ext in (".eac3", ".ac3"):
            codec_args = ["-c:a", "eac3", "-b:a", "224k"]
        else:
            codec_args = ["-c:a", "aac", "-b:a", "192k"]

        if gap_sec > 0.05:
            # Cut extra segment
            t_resume = t_jump + gap_sec
            filter_str = (
                f"[0:a]atrim=0:{t_jump:.3f},asetpts=PTS-STARTPTS[a1];"
                f"[0:a]atrim={t_resume:.3f},asetpts=PTS-STARTPTS[a2];"
                f"[a1][a2]concat=n=2:v=0:a=1[out]"
            )
            cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-i", target_audio_path,
                "-filter_complex", filter_str,
                "-map", "[out]",
                *codec_args,
                healed_file
            ]
        elif gap_sec < -0.05:
            # Insert silence pad
            pad_dur = abs(gap_sec)
            filter_str = (
                f"[0:a]atrim=0:{t_jump:.3f},asetpts=PTS-STARTPTS[a1];"
                f"aevalsrc=0:d={pad_dur:.3f}:s=48000[silence];"
                f"[0:a]atrim={t_jump:.3f},asetpts=PTS-STARTPTS[a2];"
                f"[a1][silence][a2]concat=n=3:v=0:a=1[out]"
            )
            cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-i", target_audio_path,
                "-filter_complex", filter_str,
                "-map", "[out]",
                *codec_args,
                healed_file
            ]
        else:
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                LOGGER.error(f"FFmpeg audio heal failed: {stderr.decode()}")
                return None
            LOGGER.info(f"✨ Audio successfully healed (gap={gap_sec:+.3f}s at {t_jump:.2f}s) -> {healed_file}")
            return healed_file
        except Exception as e:
            LOGGER.error(f"Exception during audio healing: {e}")
            return None

    async def analyze_sync(
        self,
        ref_video_path: str,
        target_audio_path: str,
    ) -> Dict[str, Any]:
        """
        Evaluate adaptive anchors across the episode.
        Classify sync mode: CONSTANT, HEALED_STEP_JUMP, PAL_DRIFT, or FAILED.
        Automatically heals step jumps (sponsor cards, eyecatches).
        """
        ref_dur = self.get_duration_sec(ref_video_path)
        tgt_dur = self.get_duration_sec(target_audio_path)
        dur = min(ref_dur, tgt_dur)

        if dur <= self.window_sec:
            LOGGER.warning(f"Audio too short for anchor analysis: {dur:.1f}s")
            return {
                "sync_mode": "FAILED",
                "calculated_delay_ms": 0,
                "speed_ratio": 1.0,
                "min_psr_score": 0.0,
                "anchor_telemetry": [],
                "healed_audio_path": None,
            }

        # 1. Probe Japanese stream index from video
        jap_stream_idx = await asyncio.to_thread(self.get_japanese_audio_index, ref_video_path)

        # 2. Extract chapters to inform anchor positioning
        chapters = await asyncio.to_thread(self.get_chapters, ref_video_path)

        # 3. Build anchor timestamps
        anchor_times = []
        op_end = None
        for ch in chapters:
            title_lower = ch.get("tags", {}).get("title", "").lower()
            if "opening" in title_lower or "op" == title_lower:
                op_end = float(ch.get("end_time", 0))
                break

        if op_end and op_end > 30.0:
            # Place an anchor inside Opening/Prologue
            anchor_times.append(max(20.0, op_end / 2.0))
            # Place an anchor in Part A right after Opening
            anchor_times.append(min(dur - 30.0, op_end + 90.0))

        # Add fractional anchors from self.anchors
        for a_frac in self.anchors:
            t = dur * a_frac
            # Ensure not too close to existing anchor (minimum 45s separation)
            if not any(abs(t - existing) < 45.0 for existing in anchor_times):
                if 15.0 <= t <= dur - 20.0:
                    anchor_times.append(t)

        anchor_times.sort()

        async def evaluate_anchor(center_sec: float):
            start_sec = max(0.0, center_sec - (self.window_sec / 2.0))

            ref_task = self.extract_audio_window(ref_video_path, start_sec, self.window_sec, jap_stream_idx)
            tgt_task = self.extract_audio_window(target_audio_path, start_sec, self.window_sec)
            ref_pcm, tgt_pcm = await asyncio.gather(ref_task, tgt_task)

            if ref_pcm is None or tgt_pcm is None:
                LOGGER.warning(f"Failed extracting window at {start_sec:.1f}s")
                return None

            ref_stripped = await asyncio.to_thread(self.strip_vocals, ref_pcm)
            tgt_stripped = await asyncio.to_thread(self.strip_vocals, tgt_pcm)

            delay_ms, psr = await asyncio.to_thread(self.calculate_gcc_phat, ref_stripped, tgt_stripped)
            LOGGER.info(
                f"Anchor ({center_sec:.1f}s / {int((center_sec/dur)*100)}%): "
                f"delay={delay_ms:+.2f} ms, PSR={psr:.2f}"
            )
            return {
                "timestamp_sec": round(center_sec, 2),
                "delay_ms": round(delay_ms, 2),
                "psr": round(psr, 2),
            }

        # Concurrently evaluate all anchors
        raw_results = await asyncio.gather(*(evaluate_anchor(t) for t in anchor_times))
        anchors_data = [r for r in raw_results if r is not None]
        anchors_data.sort(key=lambda x: x["timestamp_sec"])

        confident_anchors = [a for a in anchors_data if a["psr"] >= self.min_psr]

        if len(confident_anchors) < 2:
            LOGGER.error(f"❌ Acoustic Sync: Insufficient confident anchors ({len(confident_anchors)})")
            return {
                "sync_mode": "FAILED",
                "calculated_delay_ms": 0,
                "speed_ratio": 1.0,
                "min_psr_score": 0.0,
                "anchor_telemetry": anchors_data,
                "healed_audio_path": None,
            }

        delays = [a["delay_ms"] for a in confident_anchors]
        times = [a["timestamp_sec"] for a in confident_anchors]
        psrs = [a["psr"] for a in confident_anchors]
        min_psr = min(psrs)
        max_spread = max(delays) - min(delays)

        # CASE 1: Pure Constant Offset across all confident anchors
        if max_spread <= 15.0:
            final_delay = int(round(np.mean(delays)))
            mkv_offset = -final_delay
            LOGGER.info(f"✅ Acoustic Sync: CONSTANT delay = {final_delay} ms ({len(confident_anchors)} anchors, Spread: {max_spread:.2f} ms, Min PSR: {min_psr:.1f})")
            return {
                "sync_mode": "CONSTANT",
                "calculated_delay_ms": final_delay,
                "mkvmerge_sync_offset_ms": mkv_offset,
                "mkvmerge_sync_arg": f"0:{mkv_offset}" if abs(mkv_offset) >= 15 else "",
                "speed_ratio": 1.0,
                "min_psr_score": round(min_psr, 2),
                "anchor_telemetry": anchors_data,
                "healed_audio_path": None,
            }

        # CASE 2: Step Jump Detection & Autonomous Audio Healing
        # Search for a partition point dividing confident anchors into two distinct clusters
        best_split = None
        for i in range(1, len(confident_anchors)):
            early_d = [a["delay_ms"] for a in confident_anchors[:i]]
            late_d = [a["delay_ms"] for a in confident_anchors[i:]]
            spread_early = max(early_d) - min(early_d)
            spread_late = max(late_d) - min(late_d)
            mean_early = np.mean(early_d)
            mean_late = np.mean(late_d)
            jump_diff = mean_late - mean_early

            if spread_early <= 20.0 and spread_late <= 25.0 and abs(jump_diff) >= 250.0:
                best_split = (i, mean_early, mean_late, jump_diff)
                break

        if best_split is not None:
            split_idx, d_early, d_late, jump_ms = best_split
            t_early = confident_anchors[split_idx - 1]["timestamp_sec"]
            t_late = confident_anchors[split_idx]["timestamp_sec"]
            LOGGER.warning(f"⚠️ Step Jump detected: early={d_early:+.1f}ms, late={d_late:+.1f}ms (Jump: {jump_ms:+.1f}ms between {t_early:.1f}s and {t_late:.1f}s)")

            # Pinpoint exact jump location
            t_jump = await self.find_jump_point(
                ref_video_path=ref_video_path,
                target_audio_path=target_audio_path,
                t_start=t_early,
                t_end=t_late,
                d_early=d_early,
                d_late=d_late,
                jap_stream_idx=jap_stream_idx,
                chapters=chapters,
            )

            gap_sec = jump_ms / 1000.0
            healed_audio = await self.heal_audio(target_audio_path, t_jump, gap_sec)

            if healed_audio and os.path.exists(healed_audio):
                final_delay = int(round(d_early))
                mkv_offset = -final_delay
                LOGGER.info(f"✅ Acoustic Sync: HEALED_STEP_JUMP applied (Base lag: {final_delay}ms, mkvmerge --sync 0:{mkv_offset})")
                return {
                    "sync_mode": "HEALED_STEP_JUMP",
                    "calculated_delay_ms": final_delay,
                    "mkvmerge_sync_offset_ms": mkv_offset,
                    "mkvmerge_sync_arg": f"0:{mkv_offset}" if abs(mkv_offset) >= 15 else "",
                    "speed_ratio": 1.0,
                    "min_psr_score": round(min_psr, 2),
                    "anchor_telemetry": anchors_data,
                    "healed_audio_path": healed_audio,
                    "step_jump_sec": round(gap_sec, 3),
                    "jump_timestamp_sec": t_jump,
                }

        # CASE 3: Strict PAL Drift Validation (23.976 <-> 25.0 fps)
        if len(times) >= 3:
            slope, intercept = np.polyfit(times, delays, 1)  # ms per second
            speed_ratio = 1.0 + (slope / 1000.0)
            is_true_pal = (abs(speed_ratio - 1.042708) < 0.005) or (abs(speed_ratio - 0.959040) < 0.005)

            if is_true_pal and min_psr >= 15.0:
                LOGGER.warning(f"⚠️ Acoustic Sync: TRUE PAL_DRIFT verified (Slope: {slope:.3f} ms/s, Speed ratio: {speed_ratio:.6f})")
                final_d = int(round(intercept))
                mkv_offset = -final_d
                return {
                    "sync_mode": "PAL_DRIFT",
                    "calculated_delay_ms": final_d,
                    "mkvmerge_sync_offset_ms": mkv_offset,
                    "mkvmerge_sync_arg": f"0:{mkv_offset},{speed_ratio:.6f}" if abs(mkv_offset) >= 15 else f"0:0,{speed_ratio:.6f}",
                    "speed_ratio": round(speed_ratio, 6),
                    "min_psr_score": round(min_psr, 2),
                    "anchor_telemetry": anchors_data,
                    "healed_audio_path": None,
                }

        # CASE 4: Robust Median Fallback
        median_delay = int(round(float(np.median(delays))))
        mkv_offset = -median_delay
        LOGGER.info(f"Acoustic Sync: Median delay = {median_delay} ms across {len(confident_anchors)} anchors (Spread: {max_spread:.1f} ms)")
        return {
            "sync_mode": "CONSTANT",
            "calculated_delay_ms": median_delay,
            "mkvmerge_sync_offset_ms": mkv_offset,
            "mkvmerge_sync_arg": f"0:{mkv_offset}" if abs(mkv_offset) >= 15 else "",
            "speed_ratio": 1.0,
            "min_psr_score": round(min_psr, 2),
            "anchor_telemetry": anchors_data,
            "healed_audio_path": None,
        }

    def analyze_sync_sync(
        self,
        ref_video_path: str,
        target_audio_path: str,
    ) -> Dict[str, Any]:
        """
        Synchronous thread-safe entry point for thread pool workers.
        Runs analyze_sync in an isolated event loop if none exists in current thread.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, self.analyze_sync(ref_video_path, target_audio_path)).result()
            else:
                return loop.run_until_complete(self.analyze_sync(ref_video_path, target_audio_path))
        except RuntimeError:
            return asyncio.run(self.analyze_sync(ref_video_path, target_audio_path))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== Upgraded AcousticSyncEngine Module Ready ===")
