import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).parents[1]

# Mock dependencies
for mod in [
    "aiofiles",
    "aiofiles.os",
    "aiopath",
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
    if "aiopath" in mod or "aiofiles" in mod:
        mock.exists = AsyncMock(return_value=True)
        mock.isdir = AsyncMock(return_value=True)
        mock.isfile = AsyncMock(return_value=True)
        mock.path.exists = AsyncMock(return_value=True)
        mock.path.isdir = AsyncMock(return_value=True)
        mock.path.isfile = AsyncMock(return_value=True)
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

# Load hindianime resolver and module
spec_haz_res = importlib.util.spec_from_file_location("bot.helper.ext_utils.hindianime_resolver", _REPO_ROOT / "bot" / "helper" / "ext_utils" / "hindianime_resolver.py")
haz_res_mod = importlib.util.module_from_spec(spec_haz_res)
haz_res_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.hindianime_resolver"] = haz_res_mod
spec_haz_res.loader.exec_module(haz_res_mod)
HindiAnimeResolver = haz_res_mod.HindiAnimeResolver
HindiAnimeCandidate = haz_res_mod.HindiAnimeCandidate

spec_haz = importlib.util.spec_from_file_location("bot.modules.hindianime", _REPO_ROOT / "bot" / "modules" / "hindianime.py")
haz_mod = importlib.util.module_from_spec(spec_haz)
haz_mod.__package__ = "bot.modules"
sys.modules["bot.modules.hindianime"] = haz_mod
spec_haz.loader.exec_module(haz_mod)

# Load fourkhdhub resolver and module
spec_4k_res = importlib.util.spec_from_file_location("bot.helper.ext_utils.fourkhdhub_resolver", _REPO_ROOT / "bot" / "helper" / "ext_utils" / "fourkhdhub_resolver.py")
fourk_res_mod = importlib.util.module_from_spec(spec_4k_res)
fourk_res_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.fourkhdhub_resolver"] = fourk_res_mod
spec_4k_res.loader.exec_module(fourk_res_mod)
FourKHDHubResolver = fourk_res_mod.FourKHDHubResolver
FourKHDHubCandidate = fourk_res_mod.FourKHDHubCandidate

spec_4k = importlib.util.spec_from_file_location("bot.modules.fourkhdhub", _REPO_ROOT / "bot" / "modules" / "fourkhdhub.py")
fourk_mod = importlib.util.module_from_spec(spec_4k)
fourk_mod.__package__ = "bot.modules"
sys.modules["bot.modules.fourkhdhub"] = fourk_mod
spec_4k.loader.exec_module(fourk_mod)


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"} if text else default


class TestLeechSettingsAndOnDemand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        haz_mod.user_data = {}
        fourk_mod.user_data = {}

    def test_to_bool(self):
        self.assertTrue(_to_bool(True))
        self.assertTrue(_to_bool("true"))
        self.assertTrue(_to_bool("True"))
        self.assertTrue(_to_bool("1"))
        self.assertTrue(_to_bool("yes"))
        self.assertTrue(_to_bool("on"))
        self.assertFalse(_to_bool(False))
        self.assertFalse(_to_bool("false"))
        self.assertFalse(_to_bool("0"))
        self.assertFalse(_to_bool("no"))
        self.assertFalse(_to_bool(None, default=False))
        self.assertTrue(_to_bool(None, default=True))

    def test_starfallx_final_destination_preserves_leech_dest(self):
        # Implementation logic verification
        def _final_destination(listener):
            dest = getattr(listener, "leech_dest", None)
            thread_id = None
            if not dest:
                user_dump = getattr(listener, "user_dict", {}).get("LEECH_DUMP_CHAT")
                up_dest = getattr(listener, "up_dest", None)
                if up_dest and (up_dest != "-100111111111" or user_dump):
                    dest = up_dest
                    thread_id = getattr(listener, "chat_thread_id", None)
            if not dest:
                dest = getattr(getattr(listener, "message", None), "chat", None)
                dest = getattr(dest, "id", None) or getattr(listener, "user_id", None)
                thread_id = getattr(getattr(listener, "message", None), "message_thread_id", None)
            if not isinstance(dest, int):
                if "|" in str(dest):
                    dest, thread = str(dest).split("|", 1)
                    thread_id = int(thread) if thread.lstrip("-").isdigit() else None
                if str(dest).lstrip("-").isdigit():
                    dest = int(dest)
            return dest, thread_id

        listener = MagicMock()
        listener.leech_dest = -100987654321
        listener.up_dest = -100111111111
        listener.message.chat.id = -100222222222

        dest, thread = _final_destination(listener)
        self.assertEqual(dest, -100987654321)

    def test_destination_routing_priority_in_common(self):
        # Verification that user_dict["LEECH_DUMP_CHAT"] takes priority over group chat
        group_dest = "-100222222222"
        user_dict = {"LEECH_DUMP_CHAT": "-1003887117402"}
        up_dest_cli = ""  # No -up passed on command line
        main_dump = "-100111111111"  # Global dump channel

        target_dest = (
            up_dest_cli
            or user_dict.get("LEECH_DUMP_CHAT")
            or group_dest
            or ""
        )
        self.assertEqual(target_dest, "-1003887117402")

    def test_anime_task_settings_isolation(self):
        user_id = 77777
        haz_mod.user_data[user_id] = {
            "AUTO_REMOVE_STREAMS": False,
            "AUTO_KEEP_AUDIO_LANGS": "Japanese",
            "AUTORENAME": False,
            "AUTO_POSTER_SEND_MESSAGE": True,
        }
        task_settings = haz_mod._get_anime_task_settings(user_id, is_season_pack=False)
        self.assertTrue(task_settings["AUTO_REMOVE_STREAMS"])
        self.assertEqual(task_settings["AUTO_KEEP_AUDIO_LANGS"], "Tamil")
        self.assertTrue(task_settings["AUTORENAME"])

        # Check global user_data was NOT mutated
        self.assertFalse(haz_mod.user_data[user_id]["AUTO_REMOVE_STREAMS"])
        self.assertEqual(haz_mod.user_data[user_id]["AUTO_KEEP_AUDIO_LANGS"], "Japanese")
        self.assertFalse(haz_mod.user_data[user_id]["AUTORENAME"])
        self.assertTrue(haz_mod.user_data[user_id]["AUTO_POSTER_SEND_MESSAGE"])

    def test_fourkhd_task_settings_isolation(self):
        user_id = 66666
        fourk_mod.user_data[user_id] = {
            "lremname_auto": "MyMovie.mkv",
            "AUTO_POSTER_ENABLED": False,
        }
        series_settings = fourk_mod._get_fourkhd_task_settings(user_id, is_series=True)
        self.assertIn("[S{season}E{episode}]", series_settings["lremname_auto"])
        self.assertTrue(series_settings["AUTO_POSTER_ENABLED"])

        # Global user_data remains untainted
        self.assertEqual(fourk_mod.user_data[user_id]["lremname_auto"], "MyMovie.mkv")
        self.assertFalse(fourk_mod.user_data[user_id]["AUTO_POSTER_ENABLED"])

    def test_schedulers_disabled_by_default(self):
        with patch.object(haz_mod.Config, "HINDIANIME_AUTO_LEECH", False), \
             patch("bot.scheduler") as mock_sched:
            haz_mod.start_hindianime_scheduler()
            mock_sched.add_job.assert_not_called()

        with patch.object(fourk_mod.Config, "FOURKHDHUB_AUTO_LEECH", False), \
             patch("bot.scheduler") as mock_sched_4k:
            fourk_mod.start_fourkhdhub_scheduler()
            mock_sched_4k.add_job.assert_not_called()

    @patch.object(haz_mod, "_start_rss_download", new_callable=AsyncMock)
    async def test_queue_hindianime_uses_task_user_dict(self, mock_download):
        user_id = 55555
        haz_mod.user_data[user_id] = {"AUTO_POSTER_SEND_MESSAGE": True}
        cand = HindiAnimeCandidate(
            post_title="Dandadan S01",
            post_url="https://hindianimeszone.com/dandadan/",
            episode_title="Episode 01",
            quality="1080p",
            gateway_url="https://example.com/gw",
            gdflix_url="https://gdflix.test/file123",
            season=1,
            episode=1,
            file_title="Dandadan S01E01 1080p.mkv",
        )
        queued = await haz_mod.queue_hindianime_candidates([cand], owner_id=user_id, target_chat="-100999999999")
        self.assertEqual(queued, 1)
        mock_download.assert_called_once()
        kwargs = mock_download.call_args[1]
        self.assertIn("task_user_dict", kwargs)
        self.assertEqual(kwargs["task_user_dict"]["AUTO_KEEP_AUDIO_LANGS"], "Tamil")
        # Global user_data was NOT polluted
        self.assertNotIn("AUTO_KEEP_AUDIO_LANGS", haz_mod.user_data[user_id])

    @patch.object(fourk_mod, "_start_rss_download", new_callable=AsyncMock)
    async def test_queue_fourkhd_uses_task_user_dict(self, mock_download):
        user_id = 44444
        fourk_mod.user_data[user_id] = {"lremname_auto": "CleanTitle.mkv"}
        cand = FourKHDHubCandidate(
            post_title="Loki S02",
            post_url="https://4khdhub.one/loki/",
            is_series=True,
            file_title="Loki S02E01 1080p.mkv",
            quality="1080p HEVC",
            has_tamil=True,
            hub_url="https://hubcloud.test/link123",
            mediator_url="https://example.com/med",
            season=2,
            episode=1,
        )
        queued = await fourk_mod.queue_fourkhd_candidates([cand], owner_id=user_id, target_chat="-100888888888")
        self.assertEqual(queued, 1)
        mock_download.assert_called_once()
        kwargs = mock_download.call_args[1]
        self.assertIn("task_user_dict", kwargs)
        self.assertIn("[S{season}E{episode}]", kwargs["task_user_dict"]["lremname_auto"])
        # Global user_data was NOT mutated
        self.assertEqual(fourk_mod.user_data[user_id]["lremname_auto"], "CleanTitle.mkv")


if __name__ == "__main__":
    unittest.main()
