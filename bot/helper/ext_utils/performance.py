from collections import deque
from os import cpu_count
import shutil
import time

import psutil
from psutil import cpu_percent, virtual_memory

from ...core.config_manager import Config

SAFE_FREE_RAM_MB = 600
MAX_PARALLEL_TASKS = 2
SAFE_CPU_PERCENT = 88.0
FFMPEG_THREADS = 2

_cpu_samples = deque(maxlen=3)
_last_sample_ts = 0.0
_cached_telemetry = None


def get_smoothed_telemetry(download_dir: str = ".") -> dict:
    global _last_sample_ts, _cached_telemetry
    now = time.monotonic()
    if _cached_telemetry is not None and (now - _last_sample_ts) < 2.5:
        return _cached_telemetry

    _cpu_samples.append(psutil.cpu_percent(interval=None))
    _last_sample_ts = now

    vm = psutil.virtual_memory()
    avail_mb = vm.available / (1024 * 1024)
    used_gb = (vm.total - vm.available) / (1024 ** 3)
    total_gb = vm.total / (1024 ** 3)
    avg_cpu = sum(_cpu_samples) / len(_cpu_samples)

    try:
        free_disk_gb = shutil.disk_usage(download_dir).free / (1024 ** 3)
    except OSError:
        free_disk_gb = 0.0

    _cached_telemetry = {
        "avail_ram_mb": round(avail_mb, 1),
        "used_ram_gb": round(used_gb, 2),
        "total_ram_gb": round(total_gb, 2),
        "ram_percent": round(vm.percent, 1),
        "cpu_percent": round(avg_cpu, 1),
        "free_disk_gb": round(free_disk_gb, 2),
        "is_safe": avail_mb >= SAFE_FREE_RAM_MB and avg_cpu <= SAFE_CPU_PERCENT and free_disk_gb >= 5.0,
    }
    return _cached_telemetry


def is_system_safe(download_dir: str = ".") -> bool:
    return get_smoothed_telemetry(download_dir)["is_safe"]



def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_performance_profile():
    profile = str(getattr(Config, "PERFORMANCE_PROFILE", "auto") or "auto")
    profile = profile.strip().lower()
    if profile != "auto":
        return profile
    cpu_total = max(1, cpu_count() or 1)
    ram_mb = virtual_memory().total // (1024 * 1024)
    if cpu_total <= 2 or ram_mb <= 4096:
        return "safe"
    if cpu_total <= 4 or ram_mb <= 6144:
        return "balanced"
    return "max_speed"


def get_ffmpeg_threads():
    cpu_total = max(1, cpu_count() or 1)
    configured = _safe_int(getattr(Config, "FFMPEG_THREADS", 0))
    if configured > 0:
        return max(1, min(configured, cpu_total))

    profile = get_performance_profile()
    if profile == "safe":
        return min(2, cpu_total)
    if profile == "balanced":
        return max(1, min(2, cpu_total // 2 or 1))
    return max(1, min(3, cpu_total // 2 or 1))


def get_ffmpeg_cores():
    configured = str(getattr(Config, "FFMPEG_CPU_CORES", "") or "").strip()
    if configured:
        return configured
    return ",".join(str(i) for i in range(get_ffmpeg_threads()))


def get_tg_copy_delay():
    delay = _safe_float(getattr(Config, "TG_COPY_DELAY", 0.15), 0.15)
    return max(0.0, min(delay, 5.0))


def get_tg_flood_wait_multiplier():
    multiplier = _safe_float(getattr(Config, "TG_FLOOD_WAIT_MULTIPLIER", 1.1), 1.1)
    return max(1.0, min(multiplier, 3.0))


def get_max_parallel_tasks():
    configured = _safe_int(getattr(Config, "MAX_PARALLEL_TASKS", 0))
    if configured > 0:
        profile_cap = {"safe": 2, "balanced": 6}.get(get_performance_profile())
        return min(configured, profile_cap) if profile_cap else configured

    profile = get_performance_profile()
    if profile == "safe":
        return 2
    if profile == "balanced":
        return 3
    return 4


def get_premium_upload_workers():
    configured = max(
        1,
        min(3, _safe_int(getattr(Config, "PREMIUM_UPLOAD_WORKERS", 2), 2)),
    )
    profile = get_performance_profile()
    if profile == "safe":
        return 1
    if profile == "balanced":
        return min(configured, 2)
    return configured


def get_safe_cpu_percent():
    return max(0, min(_safe_int(getattr(Config, "SAFE_CPU_PERCENT", SAFE_CPU_PERCENT), SAFE_CPU_PERCENT), 100))


def get_safe_free_ram_mb():
    return max(0, _safe_int(getattr(Config, "SAFE_FREE_RAM_MB", SAFE_FREE_RAM_MB), SAFE_FREE_RAM_MB))


def resources_overloaded():
    cpu_limit = get_safe_cpu_percent()
    ram_limit_mb = get_safe_free_ram_mb()
    reasons = []

    telem = get_smoothed_telemetry()
    if cpu_limit and telem["cpu_percent"] >= cpu_limit:
        reasons.append(f"CPU {telem['cpu_percent']:.1f}% >= {cpu_limit}%")

    if ram_limit_mb and telem["avail_ram_mb"] <= ram_limit_mb:
        reasons.append(f"free RAM {int(telem['avail_ram_mb'])}MB <= {ram_limit_mb}MB")

    return bool(reasons), ", ".join(reasons)
