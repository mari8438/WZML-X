import ast
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
_CT_PATH = _REPO_ROOT / "bot" / "modules" / "create_torrent.py"


class FakeMessage:
    def __init__(self, text="", reply_to_message=None):
        self.text = text
        self.caption = ""
        self.reply_to_message = reply_to_message


class HappyFappyBatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_CT_PATH, encoding="utf-8") as f:
            code = f.read()
        tree = ast.parse(code)
        target_funcs = {
            "_safe_name",
            "_parse_folder_mode",
            "_extract_links_from_message",
            "_pack_transaction_file",
            "load_pack_transaction",
            "update_pack_transaction",
        }
        extracted = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in target_funcs
        ]
        module_ast = ast.Module(body=extracted, type_ignores=[])
        compiled = compile(module_ast, filename="<ast>", mode="exec")
        ns = {
            "re_sub": __import__("re").sub,
            "unquote": __import__("urllib.parse").parse.unquote,
            "shlex_split": __import__("shlex").split,
            "ospath": __import__("os").path,
            "dump": __import__("json").dump,
            "load": __import__("json").load,
            "time": __import__("time").time,
            "LOGGER": type("FakeLogger", (), {"warning": lambda *a, **k: None, "info": lambda *a, **k: None})(),
            "Config": type("FakeConfig", (), {"CTORRENT_OUTPUT_DIR": "/tmp/output"}),
        }
        exec(compiled, ns)
        cls.ns = ns
        cls.code = code

    def test_parse_folder_mode_option_b_with_links(self):
        msg = FakeMessage('/ctorrent "Hentai Mega Pack Vol.1" - https://hstream.moe/hentai/1 https://hstream.moe/hentai/2 --happyfappy')
        folder_name, links = self.ns["_parse_folder_mode"](msg)
        self.assertEqual(folder_name, "Hentai Mega Pack Vol.1")
        self.assertEqual(links, ["https://hstream.moe/hentai/1", "https://hstream.moe/hentai/2"])

    def test_parse_folder_mode_option_b_empty_links_for_interactive(self):
        msg = FakeMessage('/ctorrent "Hentai Mega Pack Vol.1" - --happyfappy')
        folder_name, links = self.ns["_parse_folder_mode"](msg)
        self.assertEqual(folder_name, "Hentai Mega Pack Vol.1")
        self.assertEqual(links, [])

    def test_parse_folder_mode_with_flag_m(self):
        msg = FakeMessage('/ctorrent -m "Hentai Mega Pack Vol.2" https://site.com/v1.mp4 https://site.com/v2.mp4 --happyfappy')
        folder_name, links = self.ns["_parse_folder_mode"](msg)
        self.assertEqual(folder_name, "Hentai Mega Pack Vol.2")
        self.assertEqual(links, ["https://site.com/v1.mp4", "https://site.com/v2.mp4"])

    def test_parse_folder_mode_with_dash_i(self):
        msg = FakeMessage('/ctorrent "Hentai Mega Pack Vol.3" - -i 5 https://site.com/v1.mp4 https://site.com/v2.mp4 --happyfappy')
        folder_name, links = self.ns["_parse_folder_mode"](msg)
        self.assertEqual(folder_name, "Hentai Mega Pack Vol.3")
        self.assertEqual(links, ["https://site.com/v1.mp4", "https://site.com/v2.mp4"])

    def test_extract_links_from_reply_message(self):
        reply_msg = FakeMessage("Here are the pack links:\nhttps://hstream.moe/hentai/a\n(https://hstream.moe/hentai/b);")
        links = self.ns["_extract_links_from_message"](reply_msg)
        self.assertEqual(links, ["https://hstream.moe/hentai/a", "https://hstream.moe/hentai/b"])

    def test_pack_transaction_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            pack_name = "Studio Alpha Megapack"
            self.ns["update_pack_transaction"](pack_name, "COLLECTING", output_dir=td, file_count=3)
            txn = self.ns["load_pack_transaction"](pack_name, output_dir=td)
            self.assertEqual(txn.get("stage"), "COLLECTING")
            self.assertEqual(txn.get("file_count"), 3)

            self.ns["update_pack_transaction"](pack_name, "SEEDING", output_dir=td, torrent_hash="abcd1234ef")
            txn2 = self.ns["load_pack_transaction"](pack_name, output_dir=td)
            self.assertEqual(txn2.get("stage"), "SEEDING")
            self.assertEqual(txn2.get("torrent_hash"), "abcd1234ef")

    def test_remove_failed_source_preserves_directory(self):
        import asyncio
        try:
            from aiofiles.os import path as aiopath
        except ImportError:
            class MockAiopath:
                @staticmethod
                async def exists(p):
                    return Path(p).exists()
                @staticmethod
                async def isdir(p):
                    return Path(p).is_dir()
            aiopath = MockAiopath()

        async def _test():
            with tempfile.TemporaryDirectory() as td:
                test_dir = Path(td) / "batch_folder"
                test_dir.mkdir()
                test_file = test_dir / "video1.mp4"
                test_file.write_bytes(b"dummy video data")

                # Parse _remove_failed_happyfappy_source
                tree = ast.parse(self.code)
                func_node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_remove_failed_happyfappy_source")
                mod = ast.Module(body=[func_node], type_ignores=[])
                compiled = compile(mod, filename="<ast>", mode="exec")
                ns = {
                    "aiopath": aiopath,
                    "aioremove": lambda p: None,
                    "LOGGER": type("FakeLogger", (), {"warning": lambda *a: None, "info": lambda *a: None})(),
                }
                exec(compiled, ns)
                _remove_failed = ns["_remove_failed_happyfappy_source"]

                # For directory: should return False and keep folder intact!
                result = await _remove_failed(str(test_dir))
                self.assertFalse(result)
                self.assertTrue(test_dir.exists())
                self.assertTrue(test_file.exists())

        asyncio.run(_test())

    def test_happyfappy_pack_bbcode_template_structure(self):
        self.assertIn('"happyfappy_pack":', self.code)
        self.assertIn("{total_size}", self.code)
        self.assertIn("{files_count}", self.code)
        self.assertIn("{spoilers}", self.code)
        self.assertIn("Screenshots", self.code)


if __name__ == "__main__":
    unittest.main()
