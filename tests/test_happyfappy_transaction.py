import json
import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[1] / "bot" / "helper" / "ext_utils" / "happyfappy_transaction.py"
_SPEC = importlib.util.spec_from_file_location("happyfappy_transaction", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

from_module = _MODULE
STAGES = from_module.STAGES
TorrentValidationError = from_module.TorrentValidationError
_encode = from_module._encode
find_duplicate_transaction = from_module.find_duplicate_transaction
source_fingerprint = from_module.source_fingerprint
update_transaction = from_module.update_transaction
validate_accepted_torrent = from_module.validate_accepted_torrent

ANNOUNCE = "https://tracker.happyfappy.net/announce/user-token"


def write_torrent(path, source_name, size, *, private=1, announce=ANNOUNCE):
    info = {
        b"length": size,
        b"name": source_name.encode(),
        b"piece length": 16384,
        b"pieces": b"x" * 20,
    }
    metainfo = {b"announce": announce.encode(), b"info": info}
    if private is not None:
        info[b"private"] = private
    Path(path).write_bytes(_encode(metainfo))


def test_valid_torrent_and_info_hash(tmp_path):
    source = tmp_path / "episode.mkv"
    source.write_bytes(b"video-data")
    torrent = tmp_path / "episode.torrent"
    write_torrent(torrent, source.name, source.stat().st_size)

    result = validate_accepted_torrent(torrent, source, ANNOUNCE)

    assert result["name"] == source.name
    assert result["size"] == source.stat().st_size
    assert len(result["info_hash"]) == 40
    assert source_fingerprint(source)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"private": 0}, "not private"),
        ({"announce": "https://wrong.example/announce"}, "wrong announce"),
        ({"size": 99}, "size mismatch"),
        ({"source_name": "other.mkv"}, "name mismatch"),
    ],
)
def test_invalid_accepted_torrent_is_rejected(tmp_path, kwargs, message):
    source = tmp_path / "episode.mkv"
    source.write_bytes(b"video-data")
    torrent = tmp_path / "episode.torrent"
    write_torrent(
        torrent,
        kwargs.get("source_name", source.name),
        kwargs.get("size", source.stat().st_size),
        private=kwargs.get("private", 1),
        announce=kwargs.get("announce", ANNOUNCE),
    )

    with pytest.raises(TorrentValidationError, match=message):
        validate_accepted_torrent(torrent, source, ANNOUNCE)


def test_invalid_bencode_is_rejected(tmp_path):
    source = tmp_path / "episode.mkv"
    source.write_bytes(b"video-data")
    torrent = tmp_path / "episode.torrent"
    torrent.write_bytes(b"not a torrent")

    with pytest.raises(TorrentValidationError):
        validate_accepted_torrent(torrent, source, ANNOUNCE)


def test_transaction_resume_and_duplicate_detection(tmp_path):
    torrent = tmp_path / "episode.torrent"
    update_transaction(
        torrent,
        "DOWNLOADED",
        source_url="https://hstream.moe/hentai/episode-1",
        source_size=10,
        source_hash="abc",
        title="episode.mkv",
    )
    update_transaction(torrent, "HAPPYFAPPY_ACCEPTED", upload_url="https://happyfappy.test/torrents.php?id=1")

    state = json.loads(Path(f"{torrent}.transaction.json").read_text())
    assert state["stage"] == "HAPPYFAPPY_ACCEPTED"
    assert state["stage_index"] == STAGES.index("HAPPYFAPPY_ACCEPTED")
    duplicate = find_duplicate_transaction(
        tmp_path, "https://hstream.moe/hentai/episode-1", 10, "abc", "episode.mkv"
    )
    assert duplicate["stage"] == "HAPPYFAPPY_ACCEPTED"

    update_transaction(torrent, "SEEDING")
    assert find_duplicate_transaction(
        tmp_path, "https://hstream.moe/hentai/episode-1", 10, "abc", "episode.mkv"
    ) is None
