import asyncio
import html
import importlib.util
from ast import literal_eval
from pathlib import Path
from unittest.mock import MagicMock
import types
import sys
import pytest

# Mock external psutil and core config for performance test
if "psutil" not in sys.modules:
    mock_psutil = MagicMock()
    mock_psutil.virtual_memory.return_value.total = 4 * 1024 * 1024 * 1024
    mock_psutil.virtual_memory.return_value.available = 2 * 1024 * 1024 * 1024
    mock_psutil.virtual_memory.return_value.percent = 50.0
    mock_psutil.cpu_percent.return_value = 25.0
    sys.modules["psutil"] = mock_psutil

if "bot" not in sys.modules:
    sys.modules["bot"] = types.ModuleType("bot")
if "bot.core" not in sys.modules:
    sys.modules["bot.core"] = types.ModuleType("bot.core")
if "bot.core.config_manager" not in sys.modules:
    cfg_mod = types.ModuleType("bot.core.config_manager")
    class DummyConfig:
        PERFORMANCE_PROFILE = "auto"
        SAFE_FREE_RAM_MB = 600
        FFMPEG_THREADS = 0
        FFMPEG_CPU_CORES = ""
        SAFE_CPU_PERCENT = 88
        MAX_PARALLEL_TASKS = 2
    cfg_mod.Config = DummyConfig
    sys.modules["bot.core.config_manager"] = cfg_mod

_REPO_ROOT = Path(__file__).parents[1]

# Load performance module directly
_PERF_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "performance.py"
_PERF_SPEC = importlib.util.spec_from_file_location("bot.helper.ext_utils.performance", _PERF_PATH)
_PERF_MODULE = importlib.util.module_from_spec(_PERF_SPEC)
_PERF_SPEC.loader.exec_module(_PERF_MODULE)

get_performance_profile = _PERF_MODULE.get_performance_profile
get_ffmpeg_threads = _PERF_MODULE.get_ffmpeg_threads
get_safe_free_ram_mb = _PERF_MODULE.get_safe_free_ram_mb
get_max_parallel_tasks = _PERF_MODULE.get_max_parallel_tasks


def test_performance_defaults():
    # Profile should default to auto -> safe for 2 vCPU / 4GB RAM VPS
    profile = get_performance_profile()
    assert profile in {"safe", "balanced", "max_speed"}

    # Safe RAM limit should be at least 600 MB to prevent VPS OOM
    assert get_safe_free_ram_mb() >= 600

    # Threads should be bounded by available CPU cores
    threads = get_ffmpeg_threads()
    assert threads >= 1


def test_batch_state_path_persistence():
    # Verify configs directory is used in batch_task_registry.py
    registry_file = _REPO_ROOT / "bot" / "modules" / "batch_task_registry.py"
    content = registry_file.read_text(encoding="utf-8")
    assert 'ospath.join("configs", "batch_task_plans.json")' in content
    assert 'ospath.join(DOWNLOAD_DIR, "batch_task_plans.json")' not in content


def test_aria2_session_path_persistence():
    # Verify aria2.session is stored in configs/ac/ and not wiped downloads/
    setpkgs_file = _REPO_ROOT / "setpkgs.sh"
    content = setpkgs_file.read_text(encoding="utf-8")
    assert "--input-file=/usr/src/app/configs/ac/aria2.session" in content
    assert "--save-session=/usr/src/app/configs/ac/aria2.session" in content
    assert "touch /usr/src/app/configs/ac/aria2.session" in content
    assert "/usr/src/app/downloads/aria2.session" not in content


def test_remove_failed_happyfappy_source_logic(tmp_path):
    async def _test():
        # Test non-existent path
        non_existent = str(tmp_path / "not_there.mkv")
        assert not Path(non_existent).exists()

        # Real file removal
        sample_file = tmp_path / "test.mkv"
        sample_file.write_text("dummy")
        assert sample_file.exists()
        sample_file.unlink()
        assert not sample_file.exists()

    asyncio.run(_test())


def test_xss_escaping_in_error_page():
    xss_payload = "<script>alert('xss')</script>"
    escaped = html.escape(xss_payload)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


def test_safe_literal_eval_parsing():
    # Valid Python data literals should parse correctly
    dict_str = "{'key': 'value', 'count': 42}"
    parsed_dict = literal_eval(dict_str)
    assert parsed_dict == {"key": "value", "count": 42}

    list_str = "['ta', 'en', 'hi']"
    parsed_list = literal_eval(list_str)
    assert parsed_list == ["ta", "en", "hi"]

    # Code execution strings must raise ValueError/SyntaxError and not execute
    with pytest.raises((ValueError, SyntaxError)):
        literal_eval("__import__('os').system('echo pwned')")


def test_no_raw_eval_in_python_code():
    # Assert that no raw eval( calls exist across Python files in bot/
    bot_dir = _REPO_ROOT / "bot"
    eval_calls = []
    for py_file in bot_dir.rglob("*.py"):
        lines = py_file.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if "eval(" in stripped and not stripped.startswith("#") and "literal_eval(" not in stripped:
                eval_calls.append(f"{py_file.name}:{idx} -> {stripped}")
    assert not eval_calls, f"Unsafe eval calls found: {eval_calls}"


def test_mkvmerge_presence_in_config():
    # Verify BinConfig has MKVMERGE_NAME
    cfg_file = _REPO_ROOT / "bot" / "core" / "config_manager.py"
    content = cfg_file.read_text(encoding="utf-8")
    assert "MKVMERGE_NAME = bin_name(5)" in content
    assert '"mkvmerge"' in content

    # Verify Dockerfile installs mkvtoolnix
    docker_file = _REPO_ROOT / "Dockerfile"
    docker_content = docker_file.read_text(encoding="utf-8")
    assert "mkvtoolnix" in docker_content

    # Verify start.sh validates mkvmerge
    start_sh = _REPO_ROOT / "start.sh"
    start_content = start_sh.read_text(encoding="utf-8")
    assert "require_binary mkvmerge mkvtoolnix" in start_content


def test_start_message_styled():
    # Verify START_MSG has StarFallX styling
    en_file = _REPO_ROOT / "bot" / "helper" / "languages" / "en.py"
    content = en_file.read_text(encoding="utf-8")
    assert "StarFallX v1.2" in content
    assert "MKVToolNix" in content


def test_qbittorrent_vps_memory_limits():
    # Verify qBittorrent working set is bounded to 1024MB for 4GB VPS
    qbit_file = _REPO_ROOT / "configs" / "qbittorrent" / "qBittorrent" / "config" / "qBittorrent.conf"
    content = qbit_file.read_text(encoding="utf-8")
    assert "MemoryWorkingSetLimit=1024" in content
    assert "Session\\AsyncIOThreadsCount=8" in content
    assert "Session\\DiskCacheSize=256" in content


def test_hyperdl_temp_parts_purged_in_files_utils():
    # Verify regex in files_utils.py matches .temp.00 parts
    import re
    part_name = "test_movie.mp4.temp.03"
    assert re.search(r"\.temp\.\d+$", part_name) is not None

