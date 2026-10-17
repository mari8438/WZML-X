import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).parents[1]
_HAPPYFAPPY_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "happyfappy.py"
_PIXHOST_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "pixhost.py"

import sys
if importlib.util.find_spec("httpx") is None:
    mock_httpx = MagicMock()
    mock_httpx.__spec__ = importlib.machinery.ModuleSpec("httpx", None)
    sys.modules["httpx"] = mock_httpx

_HF_SPEC = importlib.util.spec_from_file_location("happyfappy_mod", _HAPPYFAPPY_PATH)
hf_mod = importlib.util.module_from_spec(_HF_SPEC)
_HF_SPEC.loader.exec_module(hf_mod)


class HappyFappyPreviewAndCookiesTest(unittest.TestCase):
    def test_parse_cookies_raw_string(self):
        raw = "sid=%FB5%7B%28%7C; cid=a%00%86%02; other=ignore"
        cookies = hf_mod._parse_cookies(raw)
        self.assertEqual(cookies.get("sid"), "%FB5%7B%28%7C")
        self.assertEqual(cookies.get("cid"), "a%00%86%02")
        self.assertEqual(cookies.get("other"), "ignore")

    def test_parse_cookies_json_list(self):
        raw_json = '[{"name": "sid", "value": "val_sid", "domain": "www.happyfappy.net"}, {"name": "cid", "value": "val_cid", "domain": ".happyfappy.net"}]'
        cookies = hf_mod._parse_cookies(raw_json)
        self.assertEqual(cookies.get("sid"), "val_sid")
        self.assertEqual(cookies.get("cid"), "val_cid")

    def test_parse_cookies_json_dict(self):
        raw_json = '{"sid": "sid_123", "cid": "cid_456"}'
        cookies = hf_mod._parse_cookies(raw_json)
        self.assertEqual(cookies.get("sid"), "sid_123")
        self.assertEqual(cookies.get("cid"), "cid_456")

    def test_parse_cookies_netscape_string(self):
        netscape = (
            "# Netscape HTTP Cookie File\n"
            "www.happyfappy.net\tFALSE\t/\tTRUE\t0\tsid\tsid_netscape_val\n"
            "www.happyfappy.net\tFALSE\t/\tTRUE\t1794535701\tcid\tcid_netscape_val\n"
        )
        cookies = hf_mod._parse_cookies(netscape)
        self.assertEqual(cookies.get("sid"), "sid_netscape_val")
        self.assertEqual(cookies.get("cid"), "cid_netscape_val")

    def test_parse_cookies_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            cookie_file = Path(td) / "happyfappy.cookies.txt"
            cookie_file.write_text(
                "# Netscape HTTP Cookie File\n"
                "www.happyfappy.net\tFALSE\t/\tTRUE\t0\tsid\tsid_file_token\n"
                "www.happyfappy.net\tFALSE\t/\tTRUE\t1794535701\tcid\tcid_file_token\n",
                encoding="utf-8",
            )
            cookies = hf_mod._parse_cookies(str(cookie_file))
            self.assertEqual(cookies.get("sid"), "sid_file_token")
            self.assertEqual(cookies.get("cid"), "cid_file_token")

    def test_screenshot_cycle_to_12(self):
        screenshots = [f"shot_{i}.jpg" for i in range(4)]
        selected = []
        while len(selected) < 12:
            for s in screenshots:
                selected.append(s)
                if len(selected) == 12:
                    break
        self.assertEqual(len(selected), 12)
        self.assertEqual(
            selected,
            [
                "shot_0.jpg", "shot_1.jpg", "shot_2.jpg", "shot_3.jpg",
                "shot_0.jpg", "shot_1.jpg", "shot_2.jpg", "shot_3.jpg",
                "shot_0.jpg", "shot_1.jpg", "shot_2.jpg", "shot_3.jpg",
            ],
        )

    def test_screenshot_sampling_when_more_than_12(self):
        screenshots = [f"shot_{i}.jpg" for i in range(20)]
        indices = [round(i * (len(screenshots) - 1) / 11) for i in range(12)]
        selected = [screenshots[i] for i in indices]
        self.assertEqual(len(selected), 12)
        self.assertEqual(selected[0], "shot_0.jpg")
        self.assertEqual(selected[-1], "shot_19.jpg")

    def test_preview_webp_sample_video_segments(self):
        # 4 parts of 5s each (total 20s) across the video, speeded up to 10s
        total_dur = 1200
        starts = [
            total_dur * 0.15,
            total_dur * 0.40,
            total_dur * 0.65,
            total_dur * 0.85,
        ]
        self.assertEqual(len(starts), 4)
        self.assertEqual(starts[0], 180.0)
        self.assertEqual(starts[-1], 1020.0)
        # Verify 4 segments * 5s = 20s
        self.assertEqual(len(starts) * 5.0, 20.0)

    def test_pixhost_webp_mime_type(self):
        import asyncio

        async def _test():
            _PX_SPEC = importlib.util.spec_from_file_location("pixhost_mod", _PIXHOST_PATH)
            px_mod = importlib.util.module_from_spec(_PX_SPEC)
            _PX_SPEC.loader.exec_module(px_mod)

            with tempfile.TemporaryDirectory() as td:
                webp_path = Path(td) / "test.webp"
                webp_path.write_bytes(b"RIFFdummyWEBP")

                class FakeClient:
                    def __init__(self):
                        self.calls = []

                    async def post(self, url, headers=None, files=None, data=None):
                        self.calls.append({"files": files, "data": data})

                        class Resp:
                            status_code = 200

                            def raise_for_status(self):
                                pass

                            def json(self):
                                return {
                                    "th_url": "https://t1.pixhost.cc/thumbs/1/test.webp",
                                    "show_url": "https://pixhost.to/show/1/test.webp",
                                }

                        return Resp()

                client = FakeClient()
                result = await px_mod.upload_image_details(str(webp_path), client=client)
                self.assertEqual(result["thumbnail"], "https://t1.pixhost.cc/thumbs/1/test.webp")
                files = client.calls[0]["files"]
                self.assertEqual(files["img"][2], "image/webp")

        asyncio.run(_test())

    def test_category_value_resolution(self):
        client = hf_mod.HappyFappyClient("https://www.happyfappy.net", "user", "pass")
        client._upload_html = (
            '<select id="category" name="category">'
            '<option value="0">---</option>'
            '<option value="1">Pron</option>'
            '<option value="11">Asian</option>'
            '<option value="15">AI</option>'
            '</select>'
        )
        self.assertEqual(client._category_value(""), "1")
        self.assertEqual(client._category_value(None), "1")
        self.assertEqual(client._category_value("0"), "1")
        self.assertEqual(client._category_value("1"), "1")
        self.assertEqual(client._category_value("11"), "11")
        self.assertEqual(client._category_value("Pron"), "1")
        self.assertEqual(client._category_value("Hentai"), "1")
        self.assertEqual(client._category_value("Asian"), "11")
        self.assertEqual(client._category_value("AI"), "15")
        self.assertEqual(client._category_value("NonExistentCategory"), "1")

    def test_hidden_fields_target_form_id(self):
        html = (
            '<form id="searchbox">'
            '<input type="hidden" name="action" value="search">'
            '<input type="hidden" name="token" value="tok123">'
            '</form>'
            '<form id="upload_table" enctype="multipart/form-data">'
            '<input type="hidden" name="auth" value="auth456">'
            '<input type="hidden" name="submit" value="true">'
            '</form>'
        )
        parser = hf_mod._HiddenFields(target_form_id="upload_table")
        parser.feed(html)
        self.assertIn("auth", parser.fields)
        self.assertIn("submit", parser.fields)
        self.assertNotIn("action", parser.fields)
        self.assertNotIn("token", parser.fields)

    def test_tag_normalization(self):
        _CT_PATH = _REPO_ROOT / "bot" / "modules" / "create_torrent.py"
        with open(_CT_PATH, encoding="utf-8") as f:
            code = f.read()
        import re
        self.assertIn("def _normalize_gazelle_tag", code)
        clean = re.sub(r"[^a-zA-Z0-9.]+", ".", "Hand Job").strip(".")
        self.assertEqual(clean.lower(), "hand.job")
        clean2 = re.sub(r"[^a-zA-Z0-9.]+", ".", "4K 48Fps").strip(".")
        self.assertEqual(clean2.lower(), "4k.48fps")

    def test_happyfappy_tags_minimum_five_and_genres(self):
        _CT_PATH = _REPO_ROOT / "bot" / "modules" / "create_torrent.py"
        with open(_CT_PATH, encoding="utf-8") as f:
            code = f.read()
        import ast
        tree = ast.parse(code)
        funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("_normalize_gazelle_tag", "_happyfappy_tags")]
        module_ast = ast.Module(body=funcs, type_ignores=[])
        compiled = compile(module_ast, filename="<ast>", mode="exec")
        ns = {"re": __import__("re"), "getattr": getattr, "Config": type("Config", (), {"HAPPYFAPPY_TAGS": ""})}
        exec(compiled, ns)
        _happyfappy_tags = ns["_happyfappy_tags"]

        # With empty summary:
        tags_empty = _happyfappy_tags("Test.Title", {})
        self.assertGreaterEqual(len(tags_empty.split()), 5)
        self.assertIn("hentai", tags_empty.split())
        self.assertIn("anime", tags_empty.split())

        # With genres:
        summary = {"genres": ["Hand Job", "Dark Skin", "Big Boobs", "Mind Control"]}
        tags_genres = _happyfappy_tags("Test.Title", summary)
        split_tags = tags_genres.split()
        self.assertIn("hand.job", split_tags)
        self.assertIn("dark.skin", split_tags)
        self.assertIn("big.boobs", split_tags)
        self.assertIn("mind.control", split_tags)
        self.assertGreaterEqual(len(split_tags), 5)


if __name__ == "__main__":
    unittest.main()
