import asyncio
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
    Sub-2s 4-Anchor GCC-PHAT Acoustic Delay Engine.
    Uses Mid/Side spatial vocal stripping and streaming float32 PCM in memory.
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

    async def extract_audio_window(
        self,
        media_path: str,
        start_sec: float,
        duration_sec: float,
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
            "-vn", "-sn", "-dn",
            "-ac", "2",
            "-ar", str(self.sample_rate),
            "-f", "f32le",
            "pipe:1"
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            raw_bytes, stderr = await proc.communicate()
            if proc.returncode != 0 or not raw_bytes:
                LOGGER.error(f"FFmpeg window extract failed for {media_path}: {stderr.decode()}")
                return None

            pcm = np.frombuffer(raw_bytes, dtype=np.float32)
            # Ensure even number of samples for stereo
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
        Compute GCC-PHAT cross-correlation between reference (e.g. Japanese audio)
        and target (e.g. Tamil audio).
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
        # Exclude peak neighborhood (+/- 50 ms = +/- 400 samples)
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

    async def analyze_sync(
        self,
        ref_video_path: str,
        target_audio_path: str,
    ) -> Dict[str, Any]:
        """
        Evaluate 4 anchors (20%, 40%, 60%, 80%) across the episode.
        Classify sync mode: CONSTANT, EYECATCH_STEP, PAL_DRIFT, or FAILED.
        """
        ref_dur = self.get_duration_sec(ref_video_path)
        tgt_dur = self.get_duration_sec(target_audio_path)
        dur = min(ref_dur, tgt_dur)

        if dur <= self.window_sec:
            LOGGER.warning(f"Audio too short for 4 anchors: {dur:.1f}s")
            return {
                "sync_mode": "FAILED",
                "calculated_delay_ms": 0,
                "speed_ratio": 1.0,
                "min_psr_score": 0.0,
                "anchor_telemetry": [],
            }

        anchors_data = []

        for anchor_frac in self.anchors:
            center_sec = dur * anchor_frac
            start_sec = max(0.0, center_sec - (self.window_sec / 2.0))

            ref_pcm = await self.extract_audio_window(ref_video_path, start_sec, self.window_sec)
            tgt_pcm = await self.extract_audio_window(target_audio_path, start_sec, self.window_sec)

            if ref_pcm is None or tgt_pcm is None:
                LOGGER.error(f"Failed extracting window at {start_sec:.1f}s")
                continue

            ref_stripped = self.strip_vocals(ref_pcm)
            tgt_stripped = self.strip_vocals(tgt_pcm)

            delay_ms, psr = self.calculate_gcc_phat(ref_stripped, tgt_stripped)
            anchors_data.append({
                "anchor": anchor_frac,
                "timestamp_sec": round(center_sec, 2),
                "delay_ms": round(delay_ms, 2),
                "psr": round(psr, 2),
            })
            LOGGER.info(
                f"Anchor {int(anchor_frac*100)}% ({center_sec:.1f}s): "
                f"delay={delay_ms:+.2f} ms, PSR={psr:.2f}"
            )

        if len(anchors_data) < 3:
            return {
                "sync_mode": "FAILED",
                "calculated_delay_ms": 0,
                "speed_ratio": 1.0,
                "min_psr_score": 0.0,
                "anchor_telemetry": anchors_data,
            }

        delays = [a["delay_ms"] for a in anchors_data]
        psrs = [a["psr"] for a in anchors_data]
        min_psr = min(psrs)
        max_spread = max(delays) - min(delays)

        # 1. CONSTANT Offset
        if min_psr >= self.min_psr and max_spread <= 8.0:
            final_delay = int(round(np.mean(delays)))
            LOGGER.info(f"✅ Acoustic Sync: CONSTANT delay = {final_delay} ms (Spread: {max_spread:.2f} ms, Min PSR: {min_psr:.1f})")
            return {
                "sync_mode": "CONSTANT",
                "calculated_delay_ms": final_delay,
                "speed_ratio": 1.0,
                "min_psr_score": round(min_psr, 2),
                "anchor_telemetry": anchors_data,
            }

        # 2. EYECATCH_STEP Check (step jump between anchor 2 (~40%) and anchor 3 (~60%))
        if len(anchors_data) == 4:
            d1, d2, d3, d4 = delays
            if abs(d1 - d2) <= 10.0 and abs(d3 - d4) <= 10.0 and abs(d3 - d2) > 12.0:
                jump_ms = round(d3 - d2, 2)
                LOGGER.warning(f"⚠️ Acoustic Sync: EYECATCH_STEP detected (Jump: {jump_ms} ms)")
                return {
                    "sync_mode": "EYECATCH_STEP",
                    "calculated_delay_ms": int(round(d1)),
                    "speed_ratio": 1.0,
                    "min_psr_score": round(min_psr, 2),
                    "anchor_telemetry": anchors_data,
                    "step_jump_ms": jump_ms,
                }

        # 3. PAL_DRIFT Check (linear speed drift between TV 25fps and BD 23.976fps)
        times = [a["timestamp_sec"] for a in anchors_data]
        if len(times) >= 3:
            slope, intercept = np.polyfit(times, delays, 1)  # ms per second
            if abs(slope) > 5.0 and min_psr >= 8.0:
                # Drift detected
                speed_ratio = 1.0 + (slope / 1000.0)
                LOGGER.warning(f"⚠️ Acoustic Sync: PAL_DRIFT detected (Slope: {slope:.3f} ms/s, Speed ratio: {speed_ratio:.6f})")
                return {
                    "sync_mode": "PAL_DRIFT",
                    "calculated_delay_ms": int(round(intercept)),
                    "speed_ratio": round(speed_ratio, 6),
                    "min_psr_score": round(min_psr, 2),
                    "anchor_telemetry": anchors_data,
                }

        # Fallback or Quarantine
        if min_psr >= 6.0:
            best_anchor = max(anchors_data, key=lambda a: a["psr"])
            LOGGER.info(f"Acoustic Sync: Moderate confidence, using best anchor delay = {best_anchor['delay_ms']} ms (PSR: {best_anchor['psr']})")
            return {
                "sync_mode": "CONSTANT",
                "calculated_delay_ms": int(round(best_anchor["delay_ms"])),
                "speed_ratio": 1.0,
                "min_psr_score": round(min_psr, 2),
                "anchor_telemetry": anchors_data,
            }

        LOGGER.error(f"❌ Acoustic Sync: FAILED confidence (Min PSR: {min_psr:.2f} < 6.0)")
        return {
            "sync_mode": "FAILED",
            "calculated_delay_ms": 0,
            "speed_ratio": 1.0,
            "min_psr_score": round(min_psr, 2),
            "anchor_telemetry": anchors_data,
        }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== AcousticSyncEngine Module Ready ===")
