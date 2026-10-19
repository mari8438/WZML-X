import base64
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).parents[1]
_RESOLVER_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "fourkhdhub_resolver.py"
_MODULE_PATH = _REPO_ROOT / "bot" / "modules" / "fourkhdhub.py"

# Mock missing dependencies if running on bare host python
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
    "pymongo.server_api",
    "sabnzbdapi",
]:
    mock = MagicMock()
    mock.__path__ = []
    mock.__spec__ = importlib.machinery.ModuleSpec(mod, None)
    sys.modules[mod] = mock

sys.modules["bot"] = MagicMock()
sys.modules["bot"].DOWNLOAD_DIR = str(_REPO_ROOT / "tests" / "temp_downloads")
sys.modules["bot"].user_data = {}
sys.modules["bot.core"] = MagicMock()
sys.modules["bot.core.config_manager"] = MagicMock()
sys.modules["bot.core.tg_client"] = MagicMock()
sys.modules["bot.helper"] = MagicMock()
sys.modules["bot.helper.ext_utils"] = MagicMock()
sys.modules["bot.helper.ext_utils.bot_utils"] = MagicMock()
sys.modules["bot.helper.ext_utils.db_handler"] = MagicMock()
sys.modules["bot.helper.telegram_helper"] = MagicMock()
sys.modules["bot.helper.telegram_helper.message_utils"] = MagicMock()
sys.modules["bot.modules.rss"] = MagicMock()

# Load resolver module
spec_res = importlib.util.spec_from_file_location("bot.helper.ext_utils.fourkhdhub_resolver", _RESOLVER_PATH)
resolver_mod = importlib.util.module_from_spec(spec_res)
resolver_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.fourkhdhub_resolver"] = resolver_mod
spec_res.loader.exec_module(resolver_mod)

FourKHDHubResolver = resolver_mod.FourKHDHubResolver
FourKHDHubCandidate = resolver_mod.FourKHDHubCandidate

# Load fourkhdhub module
spec_mod = importlib.util.spec_from_file_location("bot.modules.fourkhdhub", _MODULE_PATH)
fourkhdhub_mod = importlib.util.module_from_spec(spec_mod)
fourkhdhub_mod.__package__ = "bot.modules"
sys.modules["bot.modules.fourkhdhub"] = fourkhdhub_mod
spec_mod.loader.exec_module(fourkhdhub_mod)


class TestFourKHDHubResolver(unittest.TestCase):
    def test_mediator_decoding(self):
        """Verifies the reverse-engineered 3-step base64/rot13 unpacking."""
        test_payload = (
            "Y214WE0xWjNZbXRhVUdwMmIxQldObFo2ZFRCeFZVOXRRbmxxYVV0UU9XRndla2w1YjNveGFYRlVPV3h3YkRWM1"
            "RERnFhVzVVT1dkTlNtdDFiM3BGZVhCNWFtbFdkbXAyYjJ4V05sWjZVMVpJZDA5M1JsSXdNa2RWZURWdk1rVkx"
            "SbnBqZGtWdGVHdEtlRm94Y0ZSYWJVaExUVzVHVW1OcVRWUXhTWEY2UkcxdlVVMTJTbFJuTTBwS01WaHZNRFJ0"
            "VERONWVFWlZUbXBXWVRBOQ=="
        )
        resolved_url = FourKHDHubResolver.decode_mediator_payload(test_payload)
        self.assertEqual(resolved_url, "https://hubcloud.ist/drive/wyzmy0bbh7w2tzt")

    def test_target_quality_filter(self):
        """Tests that only 1080p HEVC and 1080p AV1 are accepted."""
        # 1080p HEVC - Should match
        hevc_title = (
            "Colony (2026) DS4K 1080p 10bit BluRay HEVC x265 "
            "[Hindi-Tamil-Telugu JHS HE-AAC 2.0 128kbps] ESub-(St0neR-4kHdHub).mkv"
        )
        is_match, q = FourKHDHubResolver.is_target_quality(hevc_title)
        self.assertTrue(is_match)
        self.assertEqual(q, "1080p hevc")

        # 1080p AV1 - Should match
        av1_title = "Lanterns.S01.1080p.AMZN.WEB-DL.Multi.DDP5.1.AV1-4KHDHub.Com.zip"
        is_match, q = FourKHDHubResolver.is_target_quality(av1_title)
        self.assertTrue(is_match)
        self.assertEqual(q, "1080p av1")

        # 2160p (4K) - Should be rejected
        k4_title = "Colony (2026) 2160p 10bit SDR BluRay HEVC x265 [Hindi-Tamil...]"
        is_match, _ = FourKHDHubResolver.is_target_quality(k4_title)
        self.assertFalse(is_match)

        # 1080p x264/AVC (Not HEVC or AV1) - Should be rejected
        x264_title = "Colony (2026) 1080p BluRay REMUX AVC x264 [Hindi-Tamil...]"
        is_match, _ = FourKHDHubResolver.is_target_quality(x264_title)
        self.assertFalse(is_match)

        # 720p - Should be rejected
        p720_title = "Colony (2026) 720p HEVC [Hindi-Tamil...]"
        is_match, _ = FourKHDHubResolver.is_target_quality(p720_title)
        self.assertFalse(is_match)

    def test_tamil_audio_check(self):
        """Tests Tamil audio detection."""
        self.assertTrue(FourKHDHubResolver.is_tamil_audio("Hindi, Tamil, Telugu, English"))
        self.assertTrue(FourKHDHubResolver.is_tamil_audio("[Hindi-Tamil-Telugu JHS HE-AAC]"))
        self.assertFalse(FourKHDHubResolver.is_tamil_audio("Hindi, Telugu, English"))
        self.assertFalse(FourKHDHubResolver.is_tamil_audio("Korean + English DDP 5.1"))

    def test_season_episode_extraction(self):
        """Tests extraction of season and episode numbers."""
        s, e = FourKHDHubResolver.extract_season_episode("Lanterns.S01E08.Dirt.and.Stars.1080p...")
        self.assertEqual((s, e), (1, 8))

        s, e = FourKHDHubResolver.extract_season_episode("Lanterns.S01.1080p.AV1.zip")
        self.assertEqual(s, 1)
        self.assertIsNone(e)

        s, e = FourKHDHubResolver.extract_season_episode("Colony (2026) 1080p HEVC.mkv")
        self.assertIsNone(s)
        self.assertIsNone(e)

    def test_parse_movie_candidates(self):
        """Tests parsing a movie post (Colony) with mocked download items."""
        sample_movie_html = """
        <div class="metadata-list">
          <div class="metadata-item"><span class="metadata-label">Audios:</span>
          <span class="metadata-value">Hindi | Tamil | Telugu | English</span></div>
        </div>
        <div class="download-item">
          <div class="file-title">Colony.2026.2160p.HEVC.mkv</div>
          <span class="badge">BluRay 2160p</span>
          <a href="https://greenmotors.club/?id=AAAA" class="btn"><span>Download HubCloud</span></a>
        </div>
        <div class="download-item">
          <div class="file-title">Colony.2026.1080p.x264.mkv</div>
          <span class="badge">1080p</span><span class="badge">x264</span>
          <a href="https://greenmotors.club/?id=BBBB" class="btn"><span>Download HubCloud</span></a>
        </div>
        <div class="download-item">
          <div class="file-title">Colony.2026.1080p.HEVC.x265.Tamil.mkv</div>
          <span class="badge">1080p</span><span class="badge">HEVC</span>
          <a href="https://greenmotors.club/?id=CCCC" class="btn"><span>Download HubCloud</span></a>
        </div>
        """
        candidates = FourKHDHubResolver.parse_post_candidates(
            sample_movie_html,
            "https://4khdhub.one/colony-movie-8357/",
            "Colony",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["file_title"], "Colony.2026.1080p.HEVC.x265.Tamil.mkv")
        self.assertEqual(candidates[0]["quality"], "1080p hevc")
        self.assertFalse(candidates[0]["is_series"])
        self.assertEqual(candidates[0]["category"], "movie")
        self.assertEqual(candidates[0]["mediator_url"], "https://greenmotors.club/?id=CCCC")

    def test_parse_series_candidates(self):
        """Tests parsing a series post (Lanterns) with season pack and episode items."""
        sample_series_html = """
        <div class="metadata-list">
          <div class="metadata-item"><span class="metadata-label">Audios:</span>
          <span class="metadata-value">Hindi | Tamil | English</span></div>
        </div>
        <div class="download-item">
          <div class="file-title">Lanterns.S01.1080p.AV1.Tamil.zip</div>
          <span class="badge">1080p</span><span class="badge">AV1</span>
          <a href="https://greenmotors.club/?id=ZZZZ" class="btn"><span>Download HubCloud</span></a>
        </div>
        """
        candidates = FourKHDHubResolver.parse_post_candidates(
            sample_series_html,
            "https://4khdhub.one/lanterns-series-7739/",
            "Lanterns",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["quality"], "1080p av1")
        self.assertTrue(candidates[0]["is_series"])
        self.assertTrue(candidates[0]["is_season_pack"])
        self.assertEqual(candidates[0]["season"], 1)


class TestFourKHDHubModule(unittest.TestCase):
    def test_prepare_user_settings_series_vs_movie(self):
        """Verifies requirement 3: for season auto poster ON and autorename [S{season}E{episode}]."""
        user_id = 8814651149
        fourkhdhub_mod.user_data[user_id] = {
            "AUTO_POSTER_ENABLED": False,
            "lremname_auto": "{name} {year} {resolution} {bit} ~",
        }

        # 1. Prepare for series:
        fourkhdhub_mod._prepare_user_settings(user_id, is_series=True)
        self.assertTrue(fourkhdhub_mod.user_data[user_id]["AUTO_POSTER_ENABLED"])
        self.assertTrue(fourkhdhub_mod.user_data[user_id]["lremname_auto"].startswith("[S{season}E{episode}] "))

        # 2. Prepare for movie:
        fourkhdhub_mod._prepare_user_settings(user_id, is_series=False)
        self.assertFalse(fourkhdhub_mod.user_data[user_id]["lremname_auto"].startswith("[S{season}E{episode}]"))
        self.assertEqual(fourkhdhub_mod.user_data[user_id]["lremname_auto"], "{name} {year} {resolution} {bit} ~")

    def test_target_channel_routing(self):
        """Verifies requirement 5: Hollywood vs Webseries channel ID routing."""
        fourkhdhub_mod.Config.FOURKHDHUB_HOLLYWOOD_CHANNEL_ID = "-1001111111"
        fourkhdhub_mod.Config.FOURKHDHUB_WEBSERIES_CHANNEL_ID = "-1002222222"

        hw_channel = fourkhdhub_mod._get_target_channel(is_series=False)
        ws_channel = fourkhdhub_mod._get_target_channel(is_series=True)

        self.assertEqual(hw_channel, "-1001111111")
        self.assertEqual(ws_channel, "-1002222222")


if __name__ == "__main__":
    unittest.main()
