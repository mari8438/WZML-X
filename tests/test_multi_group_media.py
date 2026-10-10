import asyncio

import pytest


media_utils = pytest.importorskip("bot.helper.ext_utils.media_utils")
auto_process = pytest.importorskip("bot.helper.video_utils.auto_process")


async def _no_title_lookup(title, year=None):
    return title


async def _no_stream_lookup(filepath):
    return {}


def _parse(monkeypatch, filename):
    monkeypatch.setattr(media_utils, "_resolve_media_title", _no_title_lookup)
    monkeypatch.setattr(media_utils, "_extract_stream_rename_info", _no_stream_lookup)
    return asyncio.run(media_utils.extract_metadata_from_filename(filename))


def test_parser_detects_season_from_episode_filename(monkeypatch):
    meta = _parse(monkeypatch, "High School DxD S02E03 1080p.mkv")

    assert meta["content_type"] == "normal"
    assert meta["content_group"] == "season-2"
    assert meta["episode"] == "03"
    assert meta["episode_detected"] is True


def test_parser_uses_folder_season_context(monkeypatch):
    meta = _parse(monkeypatch, "High School DxD/Season 3/01.mkv")

    assert meta["content_type"] == "normal"
    assert meta["content_group"] == "season-3"


def test_parser_detects_ova_and_special_groups(monkeypatch):
    ova = _parse(monkeypatch, "High School DxD/OVA/OVA 01.mkv")
    special = _parse(monkeypatch, "High School DxD/Specials/SP 02.mkv")
    unnumbered = _parse(monkeypatch, "High School DxD/Specials/NCOP.mkv")

    assert (ova["content_type"], ova["content_group"], ova["episode"]) == (
        "ova",
        "ova",
        "01",
    )
    assert (special["content_type"], special["content_group"], special["episode"]) == (
        "special",
        "special",
        "02",
    )
    assert unnumbered["content_group"] == "special"
    assert unnumbered["episode_detected"] is False


def test_merge_names_keep_content_groups_separate():
    listener = type("Listener", (), {"merge_source_name": ""})()

    def batch(content_type, season="1"):
        return [
            {
                "path": "/tmp/episode.mkv",
                "size": 1,
                "meta": {
                    "title": "High School DxD",
                    "content_type": content_type,
                    "season": season,
                    "episode": "01",
                    "resolution": "1080p",
                    "bit": "10bit",
                    "quality": "BluRay",
                    "lib": "Judas",
                },
            },
            {
                "path": "/tmp/episode-2.mkv",
                "size": 1,
                "meta": {
                    "title": "High School DxD",
                    "content_type": content_type,
                    "season": season,
                    "episode": "02",
                    "resolution": "1080p",
                    "bit": "10bit",
                    "quality": "BluRay",
                    "lib": "Judas",
                },
            },
        ]

    assert "[S02-EP(01-02)]" in auto_process._build_batch_name(
        listener, batch("normal", "2")
    )
    assert "[OVA-EP(01-02)]" in auto_process._build_batch_name(
        listener, batch("ova")
    )
    assert "[SPECIAL-EP(01-02)]" in auto_process._build_batch_name(
        listener, batch("special")
    )


def test_non_video_torrent_files_are_not_merge_candidates(tmp_path):
    (tmp_path / "01.mkv").write_bytes(b"video")
    (tmp_path / "cover.jpg").write_bytes(b"image")
    (tmp_path / "readme.txt").write_text("metadata")

    candidates = asyncio.run(auto_process._video_files(str(tmp_path)))

    assert candidates == [str(tmp_path / "01.mkv")]
