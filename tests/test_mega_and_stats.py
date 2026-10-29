from pathlib import Path
import re
import pytest

_REPO_ROOT = Path(__file__).parents[1]

# Link validation matching bot/helper/ext_utils/links_utils.py
def is_mega_link(url: str) -> bool:
    return bool(
        re.match(
            r"https?:\/\/(?:[a-zA-Z0-9-]+\.)?mega\.(?:co\.)?nz\/(?:folder\/|file\/|#F?!|#!)",
            url,
        )
        or "mega.nz" in url
        or "mega.co.nz" in url
    )


def is_mega_folder(url: str) -> bool:
    return bool(
        re.match(
            r"https?:\/\/(?:[a-zA-Z0-9-]+\.)?mega\.(?:co\.)?nz\/(?:#F!|folder\/)",
            url,
        )
    )


PROGRESS_PATTERN = re.compile(
    r"^(?P<name>.+?):\s+(?P<pct>[\d.]+)%\s+-\s+(?P<p_str>[^\(]+)\s+\((?P<p_bytes>\d+)\s+bytes\)\s+of\s+(?P<t_str>[^\(\n]+?)(?:\s+\((?P<speed>[^\)]+)\))?$"
)


def test_is_mega_link():
    assert is_mega_link("https://mega.nz/file/GWpmFAQS#K22Xkw2rAVDxLPu3oN7cL_j6QwmolarRipfR-3s4daw")
    assert is_mega_link("https://mega.nz/#!GWpmFAQS!K22Xkw2rAVDxLPu3oN7cL_j6QwmolarRipfR-3s4daw")
    assert is_mega_link("https://mega.nz/folder/abc12345#def67890")
    assert is_mega_link("https://mega.nz/#F!abc12345!def67890")
    assert is_mega_link("https://mega.co.nz/#!abc12345!def67890")
    assert not is_mega_link("https://drive.google.com/file/d/12345/view")
    assert not is_mega_link("https://t.me/c/123456/78")


def test_is_mega_folder():
    assert is_mega_folder("https://mega.nz/folder/abc12345#def67890")
    assert is_mega_folder("https://mega.nz/#F!abc12345!def67890")
    assert not is_mega_folder("https://mega.nz/file/GWpmFAQS#K22Xkw2rAVDxLPu3oN7cL_j6QwmolarRipfR-3s4daw")
    assert not is_mega_folder("https://mega.nz/#!GWpmFAQS!K22Xkw2rAVDxLPu3oN7cL_j6QwmolarRipfR-3s4daw")


def test_megatools_progress_regex():
    sample_line = "[Toonworld4all] The Apothecary Diaries S03E01 480p x264 WEB-DL Multi Audio ESub.mkv: 17.20% - 15.3 MiB (16007600 bytes) of 88.7 MiB (5.1 MiB/s)"
    match = PROGRESS_PATTERN.search(sample_line)
    assert match is not None
    data = match.groupdict()
    assert data["name"] == "[Toonworld4all] The Apothecary Diaries S03E01 480p x264 WEB-DL Multi Audio ESub.mkv"
    assert data["pct"] == "17.20"
    assert data["p_bytes"] == "16007600"
    assert data["t_str"] == "88.7 MiB"
    assert data["speed"] == "5.1 MiB/s"


def test_stats_7z_regex():
    sample_7z = "7-Zip [64] 26.02 : Copyright (c) 1999-2026 Igor Pavlov : 2026-06-25"
    m = re.search(r"7-Zip\s*(?:\[\d+\])?\s*([\d.]+)", sample_7z)
    assert m is not None
    assert m.group(1) == "26.02"

    sample_old_7z = "7-Zip 19.00 (x64) : Copyright"
    m2 = re.search(r"7-Zip\s*(?:\[\d+\])?\s*([\d.]+)", sample_old_7z)
    assert m2 is not None
    assert m2.group(1) == "19.00"


def test_megatools_code_integrity():
    download_file = _REPO_ROOT / "bot" / "helper" / "mirror_leech_utils" / "download_utils" / "megatools_download.py"
    assert download_file.exists()
    content = download_file.read_text(encoding="utf-8")
    assert "megatools" in content
    assert "add_mega_download" in content
    assert "MegaToolsDownloader" in content
    assert "PROGRESS_PATTERN" in content

    status_file = _REPO_ROOT / "bot" / "helper" / "mirror_leech_utils" / "status_utils" / "megatools_status.py"
    assert status_file.exists()
    s_content = status_file.read_text(encoding="utf-8")
    assert "MegaToolsStatus" in s_content

    ml_file = _REPO_ROOT / "bot" / "modules" / "mirror_leech.py"
    ml_content = ml_file.read_text(encoding="utf-8")
    assert "is_mega_link" in ml_content
    assert "add_mega_download" in ml_content
