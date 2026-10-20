import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).parents[1]
_RESOLVER_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "hindianime_resolver.py"
_MODULE_PATH = _REPO_ROOT / "bot" / "modules" / "hindianime.py"

# Mock missing dependencies if running on host python
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
mock_rss = MagicMock()
mock_rss._parse_chat_value = lambda chat: (int(chat) if str(chat).lstrip("-").isdigit() else str(chat), None)
sys.modules["bot.modules.rss"] = mock_rss

# Load resolver module
spec_res = importlib.util.spec_from_file_location("bot.helper.ext_utils.hindianime_resolver", _RESOLVER_PATH)
resolver_mod = importlib.util.module_from_spec(spec_res)
resolver_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.hindianime_resolver"] = resolver_mod
spec_res.loader.exec_module(resolver_mod)

HindiAnimeResolver = resolver_mod.HindiAnimeResolver
HindiAnimeCandidate = resolver_mod.HindiAnimeCandidate

# Load module
spec_mod = importlib.util.spec_from_file_location("bot.modules.hindianime", _MODULE_PATH)
haz_mod = importlib.util.module_from_spec(spec_mod)
haz_mod.__package__ = "bot.modules"
sys.modules["bot.modules.hindianime"] = haz_mod
spec_mod.loader.exec_module(haz_mod)


class TestHindiAnimeResolver(unittest.TestCase):
    def test_tamil_audio_detection(self):
        self.assertTrue(HindiAnimeResolver.is_tamil_audio("Solo Leveling Season 2 Multi Audio [Tamil + Telugu + Hindi] 1080p"))
        self.assertTrue(HindiAnimeResolver.is_tamil_audio("Jujutsu Kaisen Season 1 Tamil Dubbed Dual Audio"))
        self.assertTrue(HindiAnimeResolver.is_tamil_audio("Bleach TYBW Part 3 [Tamil] 720p"))
        self.assertFalse(HindiAnimeResolver.is_tamil_audio("Demon Slayer Season 4 Hindi Dubbed Multi Audio [Hindi + English]"))
        self.assertFalse(HindiAnimeResolver.is_tamil_audio("Attack on Titan Japanese Audio English Sub"))

    def test_extract_season_episode(self):
        s, e = HindiAnimeResolver.extract_season_episode("Solo Leveling S02E03 1080p")
        self.assertEqual(s, 2)
        self.assertEqual(e, 3)

        s2, e2 = HindiAnimeResolver.extract_season_episode("DanDaDan Season 1 Episode 12")
        self.assertEqual(s2, 1)
        self.assertEqual(e2, 12)

    def test_candidate_dedupe_key(self):
        cand1 = HindiAnimeCandidate(
            post_title="Blue Box S01",
            post_url="https://hindianimeszone.com/blue-box/",
            episode_title="Episode 01",
            quality="1080p x265",
            gateway_url="https://002.hindianimeszone.com/download1.php?code=123",
            gdflix_url="https://new5.gdflix.io/file/abc",
        )
        self.assertEqual(cand1.dedupe_key, "gdflix:https://new5.gdflix.io/file/abc")

        cand2 = HindiAnimeCandidate(
            post_title="Blue Box S01",
            post_url="https://hindianimeszone.com/blue-box/",
            episode_title="Episode 01",
            quality="720p",
            gateway_url="https://002.hindianimeszone.com/download1.php?code=xyz",
            gdflix_url="",
        )
        self.assertEqual(cand2.dedupe_key, "gateway:https://002.hindianimeszone.com/download1.php?code=xyz")

    def test_parse_post_episodes(self):
        sample_html = """
        <div>
            <b>Language:</span></b> <b>Tamil + Hindi + Eng</b>
            <div class="episode">
                <span class="episode-title">Episode 01</span>
                <a href="https://002.hindianimeszone.com/download1.php?code=aaa&q=1080p">Download 1080p HQ</a>
                <a href="https://002.hindianimeszone.com/download1.php?code=bbb&q=720p">Download 720p x265</a>
                <a href="https://002.hindianimeszone.com/download1.php?code=ccc&q=480p">Download 480p</a>
            </div>
            <div class="episode">
                <span class="episode-title">Episode 02</span>
                <a href="https://002.hindianimeszone.com/download1.php?code=ddd&q=1080p">Download 1080p</a>
            </div>
        </div>
        """
        episodes = HindiAnimeResolver.parse_post_episodes(
            sample_html,
            "Blue Box Season 1 Multi Audio [Tamil + Hindi]",
            "https://hindianimeszone.com/blue-box/",
        )
        self.assertEqual(len(episodes), 4)
        qualities = [c["quality"] for c in episodes]
        self.assertIn("1080p", qualities[0].lower())
        self.assertIn("720p", qualities[1].lower())
        self.assertIn("480p", qualities[2].lower())
        self.assertEqual(episodes[0]["season"], 1)
        self.assertEqual(episodes[0]["episode"], 1)
        self.assertEqual(episodes[3]["episode"], 2)


class TestHindiAnimeModule(unittest.TestCase):
    def setUp(self):
        haz_mod.user_data = {}
        haz_mod.Config.HINDIANIME_CHANNEL_ID = -1004330385098
        haz_mod.Config.HINDIANIME_USER_ID = 8814651149

    def test_prepare_user_settings(self):
        user_id = 8814651149
        haz_mod._prepare_user_settings(user_id)
        settings = haz_mod.user_data[user_id]
        
        self.assertTrue(settings["AUTO_POSTER_ENABLED"])
        self.assertEqual(settings["POST_TEMPLATE_ID"], 11)
        self.assertTrue(settings["AUTO_POSTER_USE_AS_THUMBNAIL"])
        self.assertFalse(settings["AUTO_POSTER_SEND_MESSAGE"])
        self.assertTrue(settings["AUTO_REMOVE_STREAMS"])
        self.assertEqual(settings["AUTO_KEEP_AUDIO_LANGS"], "Tamil")
        self.assertTrue(settings["AUTO_UNZIP"])
        self.assertTrue(settings["AUTO_MERGE"])
        self.assertIn("[S{season}E{episode}]", settings["lremname_auto"])
        self.assertIn("🄰🅂-", settings["lremname_auto"])

    def test_get_target_channel(self):
        haz_mod.Config.HINDIANIME_CHANNEL_ID = -1004330385098
        self.assertEqual(haz_mod._get_target_channel(), "-1004330385098")

    @patch.object(haz_mod, "_save_seen", new_callable=AsyncMock)
    @patch.object(haz_mod, "_start_rss_download", new_callable=AsyncMock)
    def test_queue_hindianime_candidates(self, mock_download, mock_save):
        haz_mod.Config.LEECH_DUMP_CHAT = "-1003887117402"
        haz_mod.Config.HINDIANIME_CHANNEL_ID = -1004330385098
        cand1 = HindiAnimeCandidate(
            post_title="Solo Leveling S02",
            post_url="https://hindianimeszone.com/solo-leveling/",
            episode_title="Episode 01",
            quality="1080p",
            gateway_url="https://002.hindianimeszone.com/download1.php?code=111",
            gdflix_url="https://new5.gdflix.io/file/testfile123",
            season=2,
            episode=1,
            file_title="Solo Leveling S02E01 1080p.mkv",
        )

        import asyncio

        async def run_test():
            queued = await haz_mod.queue_hindianime_candidates([cand1], seen=set())
            self.assertEqual(queued, 1)
            mock_download.assert_called_once()
            call_kwargs = mock_download.call_args[1]
            self.assertEqual(call_kwargs["url"], "https://new5.gdflix.io/file/testfile123")
            self.assertEqual(call_kwargs["rss_chat_id"], -1003887117402)
            self.assertEqual(call_kwargs["rss_upload_chat"], "-1004330385098")
            self.assertEqual(call_kwargs["user_id"], 8814651149)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
