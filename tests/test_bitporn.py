import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).parents[1]
_BITPORN_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "bitporn.py"

if importlib.util.find_spec("httpx") is None:
    mock_httpx = MagicMock()
    mock_httpx.__spec__ = importlib.machinery.ModuleSpec("httpx", None)
    sys.modules["httpx"] = mock_httpx

mock_config = MagicMock()
mock_config.BITPORN_URL = "https://bitporn.eu"
mock_config.BITPORN_ANNOUNCE_URL = "https://bitporn.eu/announce/21f0b107f9c57d8b4e080e8ffcb47199"

mock_cm = MagicMock()
mock_cm.Config = mock_config
sys.modules["bot"] = MagicMock()
sys.modules["bot.core"] = MagicMock()
sys.modules["bot.core.config_manager"] = mock_cm

_BP_SPEC = importlib.util.spec_from_file_location("bitporn_mod", _BITPORN_PATH)
bp_mod = importlib.util.module_from_spec(_BP_SPEC)
_BP_SPEC.loader.exec_module(bp_mod)

parse_netscape_cookies = bp_mod.parse_netscape_cookies
BitpornClient = bp_mod.BitpornClient


class TestBitporn(unittest.TestCase):
    def test_parse_netscape_cookies(self):
        sample = (
            "# Netscape HTTP Cookie File\n"
            ".bitporn.eu\tTRUE\t/\tTRUE\t1826863328\tlaravel_cookie_consent\t1\n"
            ".bitporn.eu\tTRUE\t/\tTRUE\t1792315039\tXSRF-TOKEN\ttoken123\n"
            ".bitporn.eu\tTRUE\t/\tTRUE\t1792315039\tbitporn_session\tsession456\n"
        )
        cookies = parse_netscape_cookies(sample)
        self.assertEqual(cookies.get("laravel_cookie_consent"), "1")
        self.assertEqual(cookies.get("XSRF-TOKEN"), "token123")
        self.assertEqual(cookies.get("bitporn_session"), "session456")

    def test_client_init(self):
        client = BitpornClient(
            base_url="https://bitporn.eu",
            announce_url="https://bitporn.eu/announce/testpasskey",
            cookie_file="test_cookie",
        )
        self.assertEqual(client.base_url, "https://bitporn.eu")
        self.assertEqual(client.announce_url, "https://bitporn.eu/announce/testpasskey")


if __name__ == "__main__":
    unittest.main()
