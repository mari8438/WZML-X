import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).parents[1]
_RESOLVER_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "xdmovies_resolver.py"
_HAZ_RESOLVER_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "hindianime_resolver.py"
_MODULE_PATH = _REPO_ROOT / "bot" / "modules" / "xdmovies.py"
_HAZ_MODULE_PATH = _REPO_ROOT / "bot" / "modules" / "hindianime.py"

# Mock external dependencies if needed
for mod in [
    "curl_cffi",
    "curl_cffi.requests",
    "httpx",
    "lxml",
    "lxml.html",
    "PIL",
    "pyrogram",
    "pyrogram.errors",
    "pyrogram.filters",
    "pyrogram.handlers",
    "pyrogram.types",
    "apscheduler",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers.interval",
    "pymongo",
    "sabnzbdapi",
]:
    if mod not in sys.modules:
        mock = MagicMock()
        mock.__path__ = []
        mock.__spec__ = importlib.machinery.ModuleSpec(mod, None)
        sys.modules[mod] = mock

class MockButton:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data

class MockButtonMaker:
    def __init__(self):
        self._button = []
        self._current_row = []

    def data_button(self, text, callback_data, **kwargs):
        self._current_row.append(MockButton(text, callback_data))

    def build_menu(self, b_cols=1):
        rows = []
        for i in range(0, len(self._current_row), b_cols):
            rows.append(self._current_row[i:i+b_cols])
        self._button = rows
        return rows

sys.modules["bot"] = MagicMock()
sys.modules["bot"].user_data = {}
sys.modules["bot.core"] = MagicMock()
sys.modules["bot.core.config_manager"] = MagicMock()
sys.modules["bot.core.tg_client"] = MagicMock()
sys.modules["bot.helper"] = MagicMock()
sys.modules["bot.helper"].__path__ = []
sys.modules["bot.helper.ext_utils"] = MagicMock()
sys.modules["bot.helper.ext_utils"].__path__ = []
sys.modules["bot.helper.ext_utils.bot_utils"] = MagicMock()
sys.modules["bot.helper.ext_utils.db_handler"] = MagicMock()
mock_th = MagicMock()
mock_th.__path__ = []
mock_th.button_build = MagicMock()
mock_th.button_build.ButtonMaker = MockButtonMaker
mock_th.filters = MagicMock()
mock_th.message_utils = MagicMock()
sys.modules["bot.helper.telegram_helper"] = mock_th
sys.modules["bot.helper.telegram_helper.button_build"] = mock_th.button_build
sys.modules["bot.helper.telegram_helper.filters"] = mock_th.filters
sys.modules["bot.helper.telegram_helper.message_utils"] = mock_th.message_utils
mock_rss = MagicMock()
mock_rss._parse_chat_value = lambda chat: (int(chat) if str(chat).lstrip("-").isdigit() else str(chat), None)
sys.modules["bot.modules.rss"] = mock_rss

# Load haz resolver
spec_haz_res = importlib.util.spec_from_file_location("bot.helper.ext_utils.hindianime_resolver", _HAZ_RESOLVER_PATH)
haz_resolver_mod = importlib.util.module_from_spec(spec_haz_res)
haz_resolver_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.hindianime_resolver"] = haz_resolver_mod
spec_haz_res.loader.exec_module(haz_resolver_mod)

# Load xdmovies resolver
spec_res = importlib.util.spec_from_file_location("bot.helper.ext_utils.xdmovies_resolver", _RESOLVER_PATH)
resolver_mod = importlib.util.module_from_spec(spec_res)
resolver_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.xdmovies_resolver"] = resolver_mod
spec_res.loader.exec_module(resolver_mod)

XDMoviesResolver = resolver_mod.XDMoviesResolver
XDMoviesCandidate = resolver_mod.XDMoviesCandidate

# Load xdmovies module
spec_mod = importlib.util.spec_from_file_location("bot.modules.xdmovies", _MODULE_PATH)
xdm_mod = importlib.util.module_from_spec(spec_mod)
xdm_mod.__package__ = "bot.modules"
sys.modules["bot.modules.xdmovies"] = xdm_mod
spec_mod.loader.exec_module(xdm_mod)

# Load hindianime module
spec_haz = importlib.util.spec_from_file_location("bot.modules.hindianime", _HAZ_MODULE_PATH)
haz_mod = importlib.util.module_from_spec(spec_haz)
haz_mod.__package__ = "bot.modules"
sys.modules["bot.modules.hindianime"] = haz_mod
spec_haz.loader.exec_module(haz_mod)


class TestXDMoviesResolver(unittest.TestCase):
    def test_tamil_audio_detection(self):
        self.assertTrue(XDMoviesResolver.is_tamil_audio("The Greatest of All Time (2024) [Tamil + Telugu + Hindi] 1080p"))
        self.assertTrue(XDMoviesResolver.is_tamil_audio("Leo (2023) [Tamil (Cleaned)] 1080p HQ"))
        self.assertTrue(XDMoviesResolver.is_tamil_audio("Vikram (2022) [Tam + Tel + Hin] HEVC"))
        self.assertTrue(XDMoviesResolver.is_tamil_audio("Loki S02 [Multi-Audio Tam/Tel/Hin/Eng]"))
        self.assertFalse(XDMoviesResolver.is_tamil_audio("Kalki 2898 AD (2024) [Hindi + Telugu] 1080p"))
        self.assertFalse(XDMoviesResolver.is_tamil_audio("Deadpool & Wolverine (2024) [Eng + Hin]"))
        self.assertFalse(XDMoviesResolver.is_tamil_audio("Stranger Things S04 [English + Japanese]"))

    def test_target_quality_filter(self):
        # Must be strictly 1080p AND AV1 or HEVC/H.265
        self.assertTrue(XDMoviesResolver.is_target_quality("1080p 10bit AV1")[0])
        self.assertTrue(XDMoviesResolver.is_target_quality("1080p HEVC x265 10bit")[0])
        self.assertTrue(XDMoviesResolver.is_target_quality("1080p H.265 DDP5.1")[0])
        self.assertTrue(XDMoviesResolver.is_target_quality("1080p HQ WebRip x265")[0])

        # Rejections:
        # Non-1080p
        self.assertFalse(XDMoviesResolver.is_target_quality("720p HEVC x265")[0])
        self.assertFalse(XDMoviesResolver.is_target_quality("2160p 4K UHD HEVC")[0])
        self.assertFalse(XDMoviesResolver.is_target_quality("480p AV1")[0])

        # Non-AV1/HEVC (e.g. standard H.264 / AVC / x264)
        self.assertFalse(XDMoviesResolver.is_target_quality("1080p AVC x264 AAC")[0])
        self.assertFalse(XDMoviesResolver.is_target_quality("1080p H264 BluRay")[0])
        self.assertFalse(XDMoviesResolver.is_target_quality("1080p WebRip DD5.1")[0])

    def test_is_target_candidate(self):
        cand_good = XDMoviesCandidate(
            post_title="The Goat Life (2024)",
            post_url="https://huh.xdmovies.wtf/movies/the-goat-life-2024",
            is_series=False,
            file_title="The Goat Life (2024) 1080p HEVC Tamil",
            quality="1080p hevc",
            has_tamil=True,
            download_url="https://link.xdmovies.wtf/download/xyz123",
            category="movie",
        )
        self.assertTrue(XDMoviesResolver.is_target_candidate(cand_good))

        # Rejected because non-Tamil
        cand_no_tamil = XDMoviesCandidate(
            post_title="Stree 2 (2024)",
            post_url="https://huh.xdmovies.wtf/movies/stree-2-2024",
            is_series=False,
            file_title="Stree 2 (2024) 1080p HEVC Hindi",
            quality="1080p hevc",
            has_tamil=False,
            download_url="https://link.xdmovies.wtf/download/hindi123",
            category="movie",
        )
        self.assertFalse(XDMoviesResolver.is_target_candidate(cand_no_tamil))

        # Rejected because 720p
        cand_720p = XDMoviesCandidate(
            post_title="GOAT (2024)",
            post_url="https://huh.xdmovies.wtf/movies/goat-2024",
            is_series=False,
            file_title="GOAT (2024) 720p HEVC Tamil",
            quality="720p hevc",
            has_tamil=True,
            download_url="https://link.xdmovies.wtf/download/goat720",
            category="movie",
        )
        self.assertFalse(XDMoviesResolver.is_target_candidate(cand_720p))

        # Rejected because H264
        cand_h264 = XDMoviesCandidate(
            post_title="GOAT (2024)",
            post_url="https://huh.xdmovies.wtf/movies/goat-2024",
            is_series=False,
            file_title="GOAT (2024) 1080p x264 Tamil",
            quality="1080p x264",
            has_tamil=True,
            download_url="https://link.xdmovies.wtf/download/goath264",
            category="movie",
        )
        self.assertFalse(XDMoviesResolver.is_target_candidate(cand_h264))

    def test_extract_season_episode(self):
        s1, e1 = XDMoviesResolver.extract_season_episode("Loki S02E05 1080p AV1")
        self.assertEqual(s1, 2)
        self.assertEqual(e1, 5)

        s2, e2 = XDMoviesResolver.extract_season_episode("The Boys Season 4 Episode 08 1080p HEVC")
        self.assertEqual(s2, 4)
        self.assertEqual(e2, 8)

        # Movie (no season/episode)
        s3, e3 = XDMoviesResolver.extract_season_episode("GOAT (2024) 1080p HEVC")
        self.assertIsNone(s3)
        self.assertIsNone(e3)

    def test_dedupe_key(self):
        cand = XDMoviesCandidate(
            post_title="Loki S02",
            post_url="https://huh.xdmovies.wtf/series/loki",
            is_series=True,
            file_title="Loki S02E01 1080p AV1",
            quality="1080p av1",
            has_tamil=True,
            download_url="https://link.xdmovies.wtf/download/lokis02e01",
        )
        self.assertEqual(cand.dedupe_key, "xdm:file:Loki S02E01 1080p AV1")


class TestXDMoviesSettingsAndWorkflow(unittest.TestCase):
    def test_task_settings_movie_vs_series(self):
        user_id = 999999

        # Movie settings: poster card and series description MUST BE DISABLED
        mov_settings = xdm_mod._get_xdmovies_task_settings(user_id, is_series=False)
        self.assertFalse(mov_settings["AUTO_POSTER_ENABLED"])
        self.assertFalse(mov_settings["AUTO_POSTER_SEND_MESSAGE"])
        self.assertTrue(mov_settings["AUTO_REMOVE_STREAMS"])
        self.assertEqual(mov_settings["AUTO_KEEP_AUDIO_LANGS"], "Tamil, tam")
        self.assertTrue(mov_settings["AUTO_MERGE"])
        self.assertTrue(mov_settings["AUTO_UNZIP"])
        self.assertEqual(mov_settings["POST_TEMPLATE_ID"], 11)

        # Series settings: poster card and series description MUST BE ENABLED
        ser_settings = xdm_mod._get_xdmovies_task_settings(user_id, is_series=True)
        self.assertTrue(ser_settings["AUTO_POSTER_ENABLED"])
        self.assertTrue(ser_settings["AUTO_POSTER_SEND_MESSAGE"])
        self.assertTrue(ser_settings["AUTO_REMOVE_STREAMS"])
        self.assertEqual(ser_settings["AUTO_KEEP_AUDIO_LANGS"], "Tamil, tam")
        self.assertTrue(ser_settings["AUTO_MERGE"])
        self.assertTrue(ser_settings["AUTO_UNZIP"])
        self.assertIn("S{season}E{episode}", ser_settings["lremname_auto"])

    def test_dashboard_ui_builders(self):
        text = xdm_mod._build_dashboard_text()
        self.assertIn("XDMOVIES SETTINGS DASHBOARD", text)
        self.assertIn("1080p Only", text)
        self.assertIn("AV1 + H.265 / HEVC", text)
        self.assertIn("Tamil Audio", text)

        buttons = xdm_mod._build_dashboard_buttons(12345)
        # Check ButtonMaker has data buttons for key functions
        btn_dict = {btn.text: btn.callback_data for btn in buttons._current_row}
        self.assertIn("🎯 Movies Target", btn_dict)
        self.assertIn("🎯 Series Target", btn_dict)
        self.assertIn("🗓️ Leech Last Week", btn_dict)
        self.assertIn("🔍 Search & Leech", btn_dict)
        self.assertIn("🔄 Check Latest", btn_dict)


class TestHindiAnimeZoneSettings(unittest.TestCase):
    def test_anime_dashboard_ui_builders(self):
        text = haz_mod._build_anime_dashboard_text()
        self.assertIn("HINDIANIMEZONE SETTINGS DASHBOARD", text)
        self.assertIn("1080p Only", text)
        self.assertIn("Tamil Audio", text)

        buttons = haz_mod._build_anime_dashboard_buttons(12345)
        btn_dict = {btn.text: btn.callback_data for btn in buttons._current_row}
        self.assertIn("🎯 Anime Target", btn_dict)
        self.assertIn("🗓️ Leech Last Week", btn_dict)
        self.assertIn("🔍 Search & Leech", btn_dict)
        self.assertIn("🔄 Check Latest", btn_dict)


if __name__ == "__main__":
    unittest.main()
