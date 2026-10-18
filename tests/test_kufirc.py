import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).parents[1]
_KUFIRC_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "kufirc.py"
_LUMINAPIX_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "luminapix.py"

if importlib.util.find_spec("httpx") is None:
    mock_httpx = MagicMock()
    mock_httpx.__spec__ = importlib.machinery.ModuleSpec("httpx", None)
    sys.modules["httpx"] = mock_httpx

# Mock bot.core.config_manager and bot
mock_config = MagicMock()
mock_config.KUFIRC_URL = "https://kufirc.com"
mock_config.KUFIRC_ANNOUNCE_URL = "http://kufirc.com:7456/pcscvrlpzrlko1pgjnunto103sg0abn6/announce"
mock_config.LUMINAPIX_API_KEY = "test_key"

mock_cm = MagicMock()
mock_cm.Config = mock_config
sys.modules["bot"] = MagicMock()
sys.modules["bot.core"] = MagicMock()
sys.modules["bot.core.config_manager"] = mock_cm

_LUMI_SPEC = importlib.util.spec_from_file_location("luminapix_mod", _LUMINAPIX_PATH)
lumi_mod = importlib.util.module_from_spec(_LUMI_SPEC)
_LUMI_SPEC.loader.exec_module(lumi_mod)

_KUFIRC_SPEC = importlib.util.spec_from_file_location("kufirc_mod", _KUFIRC_PATH)
kufirc_mod = importlib.util.module_from_spec(_KUFIRC_SPEC)
_KUFIRC_SPEC.loader.exec_module(kufirc_mod)


class TestLuminapix(unittest.TestCase):
    def test_validate_luminapix_url(self):
        valid = "https://luminapix.eu/images/2026/10/18/test.webp"
        self.assertEqual(lumi_mod.validate_luminapix_url(valid), valid)

    def test_validate_luminapix_url_invalid(self):
        with self.assertRaises(lumi_mod.LuminapixError):
            lumi_mod.validate_luminapix_url("https://malicious.com/test.webp")


class TestKufirc(unittest.TestCase):
    def test_parse_kufirc_cookies_netscape(self):
        netscape = (
            "# Netscape HTTP Cookie File\n"
            "kufirc.com\tFALSE\t/\tTRUE\t0\tsid\tsession123\n"
            "kufirc.com\tFALSE\t/\tTRUE\t1794723328\tcid\tcid123\n"
        )
        parsed = kufirc_mod._parse_kufirc_cookies(netscape)
        self.assertEqual(parsed.get("sid"), "session123")
        self.assertEqual(parsed.get("cid"), "cid123")

    def test_parse_kufirc_cookies_dict(self):
        parsed = kufirc_mod._parse_kufirc_cookies({"sid": "session456"})
        self.assertEqual(parsed.get("sid"), "session456")

    def test_hidden_fields_parser(self):
        html = (
            '<form id="upload_table" method="post">\n'
            '  <input type="hidden" name="auth" value="auth_token_xyz">\n'
            '  <input type="hidden" name="submit" value="true">\n'
            '</form>'
        )
        parser = kufirc_mod._HiddenFields(target_form_id="upload_table")
        parser.feed(html)
        self.assertEqual(parser.fields.get("auth"), "auth_token_xyz")
        self.assertEqual(parser.fields.get("submit"), "true")


if __name__ == "__main__":
    unittest.main()
