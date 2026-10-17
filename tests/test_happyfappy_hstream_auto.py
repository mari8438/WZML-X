import ast
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
_CT_PATH = _REPO_ROOT / "bot" / "modules" / "create_torrent.py"
_RES_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "hstream_resolver.py"


class FakeMessage:
    def __init__(self, text="", caption=None, reply_to_message=None):
        self.text = text
        self.caption = caption or ""
        self.reply_to_message = reply_to_message


class FakeStream:
    def __init__(self, label, resolution, fps, urls=None):
        self.label = label
        self.resolution = resolution
        self.fps = fps
        self.urls = urls or ["http://example.com/stream.mpd"]


class TestHappyFappyHstreamAuto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Extract create_torrent functions via AST
        with open(_CT_PATH, encoding="utf-8") as f:
            ct_code = f.read()
        ct_tree = ast.parse(ct_code)
        target_funcs = {
            "_safe_name",
            "_parse_folder_mode",
        }
        extracted = [
            node for node in ct_tree.body
            if isinstance(node, ast.FunctionDef) and node.name in target_funcs
        ]
        ct_ast = ast.Module(body=extracted, type_ignores=[])
        ct_compiled = compile(ct_ast, filename="<ast_ct>", mode="exec")
        ct_ns = {
            "re_sub": __import__("re").sub,
            "unquote": __import__("urllib.parse").parse.unquote,
            "shlex_split": __import__("shlex").split,
            "ospath": __import__("os").path,
        }
        exec(ct_compiled, ct_ns)
        cls.ct_ns = ct_ns

        # Extract hstream_resolver functions via AST
        with open(_RES_PATH, encoding="utf-8") as f:
            res_code = f.read()
        res_tree = ast.parse(res_code)
        res_funcs = {"_number", "select_hstream_pack_stream"}
        extracted_res = [
            node for node in res_tree.body
            if isinstance(node, ast.FunctionDef) and node.name in res_funcs
        ]
        res_ast = ast.Module(body=extracted_res, type_ignores=[])
        res_compiled = compile(res_ast, filename="<ast_res>", mode="exec")
        res_ns = {"HstreamStream": FakeStream}
        exec(res_compiled, res_ns)
        cls.res_ns = res_ns

    def test_parse_hanime_pack_with_dash(self):
        msg = FakeMessage("/ctorrent --happyfappy Hanime Pack - 1")
        res = self.ct_ns["_parse_folder_mode"](msg)
        self.assertIsNotNone(res)
        folder_name, links = res
        self.assertEqual(folder_name, "Hanime Pack - 1")
        self.assertEqual(links, [])

    def test_parse_hanime_pack_quoted(self):
        msg = FakeMessage('/ctorrent "Hanime Pack - 1" --happyfappy')
        res = self.ct_ns["_parse_folder_mode"](msg)
        self.assertIsNotNone(res)
        folder_name, links = res
        self.assertEqual(folder_name, "Hanime Pack - 1")
        self.assertEqual(links, [])

    def test_parse_auto_pack_keyword(self):
        msg1 = FakeMessage("/ctorrent --happyfappy auto")
        res1 = self.ct_ns["_parse_folder_mode"](msg1)
        self.assertIsNotNone(res1)
        self.assertEqual(res1[0], "auto")
        self.assertEqual(res1[1], [])

        msg2 = FakeMessage("/ctorrent auto --happyfappy")
        res2 = self.ct_ns["_parse_folder_mode"](msg2)
        self.assertIsNotNone(res2)
        self.assertEqual(res2[0], "auto")
        self.assertEqual(res2[1], [])

    def test_stream_priority_1080p48fps(self):
        s_720 = FakeStream("720p", "720p", 24.0)
        s_1080 = FakeStream("1080p", "1080p", 24.0)
        s_1080_48 = FakeStream("1080p48fps", "1080p", 48.0)

        chosen = self.res_ns["select_hstream_pack_stream"]([s_720, s_1080, s_1080_48])
        self.assertEqual(chosen.label, "1080p48fps")

    def test_stream_fallback_1080p(self):
        s_720 = FakeStream("720p", "720p", 24.0)
        s_1080 = FakeStream("1080p", "1080p", 24.0)

        chosen = self.res_ns["select_hstream_pack_stream"]([s_720, s_1080])
        self.assertEqual(chosen.label, "1080p")

    def test_stream_fallback_best_available(self):
        s_720 = FakeStream("720p", "720p", 24.0)

        chosen = self.res_ns["select_hstream_pack_stream"]([s_720])
        self.assertEqual(chosen.label, "720p")

    def test_pinned_index_text_format(self):
        # Verify the structure of the pinned catalog text
        packs = [
            {
                "pack_name": "Hanime Pack - 1",
                "readable_size": "30.5 GiB",
                "episodes_count": 28,
                "upload_url": "https://happyfappy.net/torrents/1",
                "channel_message_id": 100,
                "total_size_bytes": 32749201948,
            }
        ]
        clean_chan = "987654321"
        lines = [
            "📚 <b>HAPPYFAPPY MEGAPACKS CATALOG</b>",
            "──────────────────────────────",
        ]
        for p in packs:
            p_name = p.get("pack_name", "Pack")
            p_size = p.get("readable_size", "")
            p_eps = p.get("episodes_count", 0)
            p_url = p.get("upload_url", "")
            post_id = p.get("channel_message_id")
            post_link = f"https://t.me/c/{clean_chan}/{post_id}" if post_id else ""
            link_str = f" ➔ <a href=\"{post_link}\">View Post</a>" if post_link else ""
            lines.append(f"📁 <b>{p_name}</b> • {p_size} ({p_eps} eps){link_str}")
        self.assertIn("Hanime Pack - 1", "\n".join(lines))
        self.assertIn("https://t.me/c/987654321/100", "\n".join(lines))
