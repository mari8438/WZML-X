"""Durable state and validation helpers for the HappyFappy upload flow."""

import hashlib
import json
from pathlib import Path


STAGES = (
    "DOWNLOADED",
    "TORRENT_CREATED",
    "ARTWORK_READY",
    "HAPPYFAPPY_ACCEPTED",
    "ACCEPTED_TORRENT_READY",
    "HASH_CHECKING",
    "SEEDING",
)


class TorrentValidationError(RuntimeError):
    pass


def transaction_path(torrent_path):
    return f"{torrent_path}.transaction.json"


def _jsonable(value):
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def update_transaction(path, stage, **values):
    if stage not in STAGES:
        raise ValueError(f"Unknown HappyFappy transaction stage: {stage}")
    target = Path(path)
    state_path = Path(transaction_path(path))
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
    state.update({"stage": stage, **{key: _jsonable(value) for key, value in values.items()}})
    state["stage_index"] = STAGES.index(stage)
    state["torrent_path"] = str(target)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    return str(state_path)


def source_fingerprint(source_path):
    digest = hashlib.sha256()
    with open(source_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicate_transaction(directory, source_url, source_size, source_hash, title):
    """Return an unfinished matching transaction, if one exists."""
    directory = Path(directory)
    for state_path in directory.glob("*.transaction.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if state.get("stage") == "SEEDING":
            continue
        if (
            state.get("source_url") == source_url
            and int(state.get("source_size", -1)) == int(source_size)
            and state.get("source_hash") == source_hash
            and state.get("title") == title
        ):
            return state
    return None


def _decode(data, offset=0):
    if offset >= len(data):
        raise TorrentValidationError("invalid bencode: unexpected end")
    token = data[offset:offset + 1]
    if token == b"i":
        end = data.find(b"e", offset + 1)
        if end < 0:
            raise TorrentValidationError("invalid bencode integer")
        try:
            return int(data[offset + 1:end]), end + 1
        except ValueError as error:
            raise TorrentValidationError("invalid bencode integer") from error
    if token == b"l":
        value, offset = [], offset + 1
        while data[offset:offset + 1] != b"e":
            item, offset = _decode(data, offset)
            value.append(item)
            if offset >= len(data):
                raise TorrentValidationError("invalid bencode list")
        return value, offset + 1
    if token == b"d":
        value, offset = {}, offset + 1
        while data[offset:offset + 1] != b"e":
            key, offset = _decode(data, offset)
            if not isinstance(key, bytes):
                raise TorrentValidationError("invalid bencode dictionary key")
            item, offset = _decode(data, offset)
            value[key] = item
            if offset >= len(data):
                raise TorrentValidationError("invalid bencode dictionary")
        return value, offset + 1
    colon = data.find(b":", offset)
    if colon < 0:
        raise TorrentValidationError("invalid bencode string")
    try:
        length = int(data[offset:colon])
    except ValueError as error:
        raise TorrentValidationError("invalid bencode string length") from error
    end = colon + 1 + length
    if end > len(data):
        raise TorrentValidationError("invalid bencode string contents")
    return data[colon + 1:end], end


def _encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(_encode(key) + _encode(item) for key, item in sorted(value.items())) + b"e"
    raise TypeError(type(value).__name__)


def validate_accepted_torrent(torrent_path, source_path, announce_url):
    try:
        raw = Path(torrent_path).read_bytes()
        metainfo, end = _decode(raw)
        if end != len(raw) or not isinstance(metainfo, dict):
            raise TorrentValidationError("invalid bencode")
    except OSError as error:
        raise TorrentValidationError(f"accepted torrent is unreadable: {error}") from error
    info = metainfo.get(b"info")
    if not isinstance(info, dict):
        raise TorrentValidationError("accepted torrent has no info dictionary")
    if info.get(b"private") != 1:
        raise TorrentValidationError("accepted torrent is not private")
    announces = [metainfo.get(b"announce", b"")]
    for tier in metainfo.get(b"announce-list", []):
        announces.extend(tier if isinstance(tier, list) else [])
    expected = str(announce_url).encode()
    if expected not in announces:
        raise TorrentValidationError("accepted torrent has the wrong announce URL")
    source = Path(source_path)
    expected_name = source.name.encode()
    if info.get(b"name") != expected_name:
        raise TorrentValidationError(
            f"accepted torrent name mismatch: expected {source.name!r}"
        )
    if b"length" in info:
        total = info[b"length"]
    else:
        files = info.get(b"files")
        total = sum(item.get(b"length", 0) for item in files or [] if isinstance(item, dict))
    actual_size = source.stat().st_size
    if total != actual_size:
        raise TorrentValidationError(
            f"accepted torrent size mismatch: torrent={total} bytes, source={actual_size} bytes"
        )
    info_hash = hashlib.sha1(_encode(info)).hexdigest()
    return {"info_hash": info_hash, "size": total, "name": source.name}
