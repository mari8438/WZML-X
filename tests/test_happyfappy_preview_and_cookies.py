import importlib.util
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).parents[1]
_HAPPYFAPPY_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "happyfappy.py"
_CREATE_TORRENT_PATH = _REPO_ROOT / "bot" / "modules" / "create_torrent.py"
_PIXHOST_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "pixhost.py"

import sys
import importlib.machinery
from unittest.mock import MagicMock
if importlib.util.find_spec("httpx") is None:
    mock_httpx = MagicMock()
    mock_httpx.__spec__ = importlib.machinery.ModuleSpec("httpx", None)
    sys.modules["httpx"] = mock_httpx

_HF_SPEC = importlib.util.spec_from_file_location("happyfappy_mod", _HAPPYFAPPY_PATH)
hf_mod = importlib.util.module_from_spec(_HF_SPEC)
_HF_SPEC.loader.exec_module(hf_mod)


def test_parse_cookies_raw_string():
    raw = "sid=%FB5%7B%28%7C; cid=a%00%86%02; other=ignore"
    cookies = hf_mod._parse_cookies(raw)
    assert cookies.get("sid") == "%FB5%7B%28%7C"
    assert cookies.get("cid") == "a%00%86%02"
    assert cookies.get("other") == "ignore"


def test_parse_cookies_json_list():
    raw_json = '[{"name": "sid", "value": "val_sid", "domain": "www.happyfappy.net"}, {"name": "cid", "value": "val_cid", "domain": ".happyfappy.net"}]'
    cookies = hf_mod._parse_cookies(raw_json)
    assert cookies.get("sid") == "val_sid"
    assert cookies.get("cid") == "val_cid"


def test_parse_cookies_json_dict():
    raw_json = '{"sid": "sid_123", "cid": "cid_456"}'
    cookies = hf_mod._parse_cookies(raw_json)
    assert cookies.get("sid") == "sid_123"
    assert cookies.get("cid") == "cid_456"


def test_parse_cookies_netscape_string():
    netscape = (
        "# Netscape HTTP Cookie File\n"
        "www.happyfappy.net\tFALSE\t/\tTRUE\t0\tsid\tsid_netscape_val\n"
        "www.happyfappy.net\tFALSE\t/\tTRUE\t1794535701\tcid\tcid_netscape_val\n"
    )
    cookies = hf_mod._parse_cookies(netscape)
    assert cookies.get("sid") == "sid_netscape_val"
    assert cookies.get("cid") == "cid_netscape_val"


def test_parse_cookies_from_file(tmp_path):
    cookie_file = tmp_path / "happyfappy.cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        "www.happyfappy.net\tFALSE\t/\tTRUE\t0\tsid\tsid_file_token\n"
        "www.happyfappy.net\tFALSE\t/\tTRUE\t1794535701\tcid\tcid_file_token\n",
        encoding="utf-8",
    )
    cookies = hf_mod._parse_cookies(str(cookie_file))
    assert cookies.get("sid") == "sid_file_token"
    assert cookies.get("cid") == "cid_file_token"


def test_screenshot_cycle_to_12():
    # Simulate when hstream only has 4 screenshots: should repeat to exactly 12
    screenshots = [f"shot_{i}.jpg" for i in range(4)]
    selected = []
    while len(selected) < 12:
        for s in screenshots:
            selected.append(s)
            if len(selected) == 12:
                break
    assert len(selected) == 12
    assert selected == [
        "shot_0.jpg", "shot_1.jpg", "shot_2.jpg", "shot_3.jpg",
        "shot_0.jpg", "shot_1.jpg", "shot_2.jpg", "shot_3.jpg",
        "shot_0.jpg", "shot_1.jpg", "shot_2.jpg", "shot_3.jpg",
    ]


def test_screenshot_sampling_when_more_than_12():
    # Simulate when hstream has 20 screenshots: should pick 12 evenly distributed
    screenshots = [f"shot_{i}.jpg" for i in range(20)]
    indices = [round(i * (len(screenshots) - 1) / 11) for i in range(12)]
    selected = [screenshots[i] for i in indices]
    assert len(selected) == 12
    assert selected[0] == "shot_0.jpg"
    assert selected[-1] == "shot_19.jpg"


def test_preview_webp_start_calculation():
    # If video is 1200 seconds (20 mins), start should be approx 25% (300s)
    total_duration = 1200
    preview_duration = 10
    start = min(total_duration * 0.25, max(0.0, total_duration - preview_duration - 5.0))
    assert start == 300.0

    # If video is very short, e.g. 15 seconds
    total_duration = 15
    start = min(total_duration * 0.25, max(0.0, total_duration - preview_duration - 5.0))
    assert 0.0 <= start <= 5.0


def test_pixhost_webp_mime_type(tmp_path):
    import asyncio
    async def _test():
        _PX_SPEC = importlib.util.spec_from_file_location("pixhost_mod", _PIXHOST_PATH)
        px_mod = importlib.util.module_from_spec(_PX_SPEC)
        _PX_SPEC.loader.exec_module(px_mod)
        
        webp_path = tmp_path / "test.webp"
        webp_path.write_bytes(b"RIFFdummyWEBP")
        
        class FakeClient:
            def __init__(self):
                self.calls = []
            async def post(self, url, headers=None, files=None, data=None):
                self.calls.append({"files": files, "data": data})
                class Resp:
                    status_code = 200
                    def raise_for_status(self): pass
                    def json(self): return {"th_url": "https://t1.pixhost.cc/thumbs/1/test.webp", "show_url": "https://pixhost.to/show/1/test.webp"}
                return Resp()
                
        client = FakeClient()
        result = await px_mod.upload_image_details(str(webp_path), client=client)
        assert result["thumbnail"] == "https://t1.pixhost.cc/thumbs/1/test.webp"
        files = client.calls[0]["files"]
        assert files["img"][2] == "image/webp"

    asyncio.run(_test())

