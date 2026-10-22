import importlib.machinery
import importlib.util
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).parents[1]
_RESOLVER_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "missav_resolver.py"
_SUBTITLE_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "missav_subtitles.py"
_MODULE_PATH = _REPO_ROOT / "bot" / "modules" / "missav_leech.py"

# Mock missing dependencies if running on bare host python
for mod in ["httpx", "lxml", "lxml.html", "PIL", "PIL.Image", "PIL.ImageFilter", "PIL.ImageOps", "pyrogram", "pyrogram.errors", "pysubs2"]:
    mock = MagicMock()
    mock.__path__ = []
    mock.__spec__ = importlib.machinery.ModuleSpec(mod, None)
    sys.modules[mod] = mock

sys.modules["bot"] = MagicMock()
sys.modules["bot"].DOWNLOAD_DIR = str(_REPO_ROOT / "tests" / "temp_downloads")
sys.modules["bot.core"] = MagicMock()
sys.modules["bot.core.config_manager"] = MagicMock()
sys.modules["bot.core.tg_client"] = MagicMock()
sys.modules["bot.helper"] = MagicMock()
sys.modules["bot.helper.ext_utils"] = MagicMock()
sys.modules["bot.helper.ext_utils.bot_utils"] = MagicMock()
sys.modules["bot.helper.ext_utils.files_utils"] = MagicMock()
sys.modules["bot.helper.ext_utils.hstream_translation"] = MagicMock()
sys.modules["bot.helper.mirror_leech_utils"] = MagicMock()
sys.modules["bot.helper.mirror_leech_utils.download_utils"] = MagicMock()
sys.modules["bot.helper.mirror_leech_utils.download_utils.yt_dlp_download"] = MagicMock()
sys.modules["bot.helper.poster_engine"] = MagicMock()
sys.modules["bot.helper.poster_engine.engine"] = MagicMock()
sys.modules["bot.helper.telegram_helper"] = MagicMock()
sys.modules["bot.helper.telegram_helper.bot_commands"] = MagicMock()
sys.modules["bot.helper.telegram_helper.message_utils"] = MagicMock()
sys.modules["bot.modules.batch_task_registry"] = MagicMock()

# Load resolver module
spec_res = importlib.util.spec_from_file_location("bot.helper.ext_utils.missav_resolver", _RESOLVER_PATH)
resolver_mod = importlib.util.module_from_spec(spec_res)
resolver_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.missav_resolver"] = resolver_mod
spec_res.loader.exec_module(resolver_mod)

# Load subtitles module
spec_sub = importlib.util.spec_from_file_location("bot.helper.ext_utils.missav_subtitles", _SUBTITLE_PATH)
subtitles_mod = importlib.util.module_from_spec(spec_sub)
subtitles_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.missav_subtitles"] = subtitles_mod
spec_sub.loader.exec_module(subtitles_mod)

_ARCHIVE_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "missav_archive.py"
_TOPIC_ARCH_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "missav_topic_archive.py"

# Load archive module
spec_arc = importlib.util.spec_from_file_location("bot.helper.ext_utils.missav_archive", _ARCHIVE_PATH)
archive_mod = importlib.util.module_from_spec(spec_arc)
archive_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.missav_archive"] = archive_mod
spec_arc.loader.exec_module(archive_mod)

# Load topic archive module
spec_topic_arc = importlib.util.spec_from_file_location("bot.helper.ext_utils.missav_topic_archive", _TOPIC_ARCH_PATH)
topic_arc_mod = importlib.util.module_from_spec(spec_topic_arc)
topic_arc_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.missav_topic_archive"] = topic_arc_mod
spec_topic_arc.loader.exec_module(topic_arc_mod)

# Load leech module
spec_mod = importlib.util.spec_from_file_location("bot.modules.missav_leech", _MODULE_PATH)
leech_mod = importlib.util.module_from_spec(spec_mod)
leech_mod.__package__ = "bot.modules"
sys.modules["bot.modules.missav_leech"] = leech_mod
spec_mod.loader.exec_module(leech_mod)


class TestMissAV(unittest.TestCase):
    def test_fc2_filter_disabled(self):
        is_fc2 = resolver_mod.is_fc2_title
        # All titles should be accepted (not filtered out)
        self.assertFalse(is_fc2("FC2-PPV-4993751"))
        self.assertFalse(is_fc2("FC2PPV-123456"))
        self.assertFalse(is_fc2("fc2-ppv-4993188"))
        self.assertFalse(is_fc2("https://missav.live/en/fc2-ppv-4993751"))
        self.assertFalse(is_fc2("EBWH-373 Uncensored Leaked"))
        self.assertFalse(is_fc2("FNS-270"))

    def test_unpack_player_script(self):
        sample_packed = (
            "eval(function(p,a,c,k,e,d){e=function(c){return "
            "c.toString(36)};if(!''.replace(/^/,String)){while(c--){d[c.toString(a)]=k[c]||c.toString(a)}k=[function(e){return "
            "d[e]}];e=function(){return'\\\\w+'};c=1};while(c--){if(k[c]){p=p.replace(new RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c])}}return p"
            "}('e=\\'8://7.6/5-4-3-2-1/d.0\\';c=\\'8://7.6/5-4-3-2-1/a/9.0\\';',"
            "15,15,'m3u8|cf00d835f9bc|936e|44d2|5ace|2c09e992|com|surrit|https|video|720p|source1280|source842|playlist|source'.split('|'),0,{}))"
        )
        unpacked = resolver_mod._unpack_player_scripts(sample_packed)
        self.assertTrue(len(unpacked) > 0)
        self.assertIn("https://surrit.com/2c09e992-5ace-44d2-936e-cf00d835f9bc/playlist.m3u8", unpacked[0])

    def test_extract_sample_urls(self):
        sample_source = 'urls: ["https:\\/\\/surrit.com\\/abc-123\\/seek\\/_0.jpg","https:\\/\\/surrit.com\\/abc-123\\/seek\\/_1.jpg"]'
        urls = resolver_mod._extract_sample_urls(sample_source)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://surrit.com/abc-123/seek/_0.jpg")
        self.assertEqual(urls[1], "https://surrit.com/abc-123/seek/_1.jpg")

    def test_brand_subtitle(self):
        tmp_brand = _REPO_ROOT / "tests" / "test_brand.ass"
        try:
            subtitles_mod.write_starfall_brand_subtitle(str(tmp_brand), 3600.0)
            content = tmp_brand.read_text(encoding="utf-8")
            self.assertIn("Join our Telegram channel [@Anime_Starfall]", content)
            self.assertIn("Style: Starfall", content)
            self.assertIn("Dialogue: 0,0:00:00.00,1:00:01.00", content)
        finally:
            if tmp_brand.exists():
                tmp_brand.unlink()

    def test_video_filename_format(self):
        item = resolver_mod.MissAVTitle(
            url="https://missav.live/en/ebwh-373",
            title="EBWH-373 Beautiful Woman In Love",
        )
        quality = resolver_mod.MissAVQuality(label="1080p", url="https://surrit.com/test.m3u8", height=1080)
        filename_tamil = leech_mod._video_filename(item, quality, tamil_available=True)
        self.assertIn("🄰🅂- EBWH-373 Beautiful Woman In Love [1080p] [Jap] Tamil + ESub ~ [@Anime_Starfall🥰].mkv", filename_tamil)

        filename_esub = leech_mod._video_filename(item, quality, tamil_available=False)
        filename_eng = leech_mod._video_filename(item, quality, tamil_available=False, has_eng_sub=True)
        self.assertIn("🄰🅂- EBWH-373 Beautiful Woman In Love [1080p] [Jap] [English Subtitles] ~ [@Anime_Starfall🥰].mkv", filename_eng)

    def test_poster_caption_format(self):
        item = resolver_mod.MissAVTitle(
            url="https://missav.live/en/ebwh-373",
            title="EBWH-373 Beautiful Woman",
            date="2026-05-10",
            views=45230,
            actors=["Eimi Fukada"],
            director="Madonna",
            genres=["Uncensored", "Creampie"],
            description="A very exciting synopsis.",
        )
        caption, replies = leech_mod._poster_caption(item, tamil_available=True)
        self.assertIn("EBWH-373 Beautiful Woman", caption)
        self.assertIn("45,230 Views | 📅 2026-05-10", caption)
        self.assertIn("Japanese ~ Tamil + ESub", caption)
        self.assertIn("Eimi Fukada", caption)
        self.assertIn("Madonna", caption)
        self.assertIn("#uncensored #creampie", caption)
        self.assertIn("A very exciting synopsis.", caption)
        self.assertIn("Dropped By ➤ [@Anime_Starfall🥰]", caption)

        caption_eng, _ = leech_mod._poster_caption(item, tamil_available=False, has_eng_sub=True)
        self.assertIn("Japanese ~ English Subtitles", caption_eng)

    def test_english_subtitle_detection(self):
        is_sub = resolver_mod.is_english_subtitled
        # Title matches
        t1 = resolver_mod.MissAVTitle(url="https://missav.live/en/juq-556", title="JUQ-556 [English Subtitles] Stepmother")
        self.assertTrue(is_sub(t1))
        # Genre matches
        t2 = resolver_mod.MissAVTitle(url="https://missav.live/en/abp-123", title="ABP-123 Regular Title", genres=["Drama", "English Subtitles"])
        self.assertTrue(is_sub(t2))
        t3 = resolver_mod.MissAVTitle(url="https://missav.live/en/abp-124", title="ABP-124 Regular Title", genres=["Drama", "english-subtitle"])
        self.assertTrue(is_sub(t3))
        # String matches
        self.assertTrue(is_sub("eng_sub"))
        # Non-matching
        t4 = resolver_mod.MissAVTitle(url="https://missav.live/en/abp-125", title="ABP-125 Normal", genres=["Drama", "Creampie"])
        self.assertFalse(is_sub(t4))

    def test_select_quality_fallback(self):
        select_q = leech_mod._select_quality
        q1080 = resolver_mod.MissAVQuality(label="1080p", url="http://1080.m3u8", height=1080)
        q720 = resolver_mod.MissAVQuality(label="720p", url="http://720.m3u8", height=720)
        q480 = resolver_mod.MissAVQuality(label="480p", url="http://480.m3u8", height=480)
        q360 = resolver_mod.MissAVQuality(label="360p", url="http://360.m3u8", height=360)

        all_q = [q1080, q720, q480, q360]
        # Exact match
        self.assertEqual(select_q(all_q, "720p"), [q720])
        # Downward fallback when 720p missing
        missing_720 = [q1080, q480, q360]
        self.assertEqual(select_q(missing_720, "720p"), [q480])
        # Downward fallback to 360p
        low_only = [q360]
        self.assertEqual(select_q(low_only, "720p"), [q360])
        # Fallback when only higher quality is available (request 360p -> closest higher 480p)
        higher_only = [q1080, q480]
        self.assertEqual(select_q(higher_only, "360p"), [q480])
        # Fallback when only higher quality is available (request 720p -> 1080p)
        high_only = [q1080]
        self.assertEqual(select_q(high_only, "720p"), [q1080])
        # Fallback to source when height is 0
        q_source = resolver_mod.MissAVQuality(label="source", url="http://s.m3u8", height=0)
        self.assertEqual(select_q([q_source], "720p"), [q_source])
        # None preferred returns all
        self.assertEqual(select_q(all_q, None), all_q)

    def test_parse_query_and_quality(self):
        parse = leech_mod._parse_query_and_quality
        q, pref = parse(["/mll", "Mother", "English", "Subtitles", "720p"])
        self.assertEqual(q, "Mother English Subtitles")
        self.assertEqual(pref, "720p")

        q2, pref2 = parse(["/mll", "Mother", "English", "Subtitles", "720p", "-up", "-100123|5"])
        self.assertEqual(q2, "Mother English Subtitles")
        self.assertEqual(pref2, "720p")

        q3, pref3 = parse(["/mll", "A-C", "480p"])
        self.assertEqual(q3, "A-C")
        self.assertEqual(pref3, "480p")

        q4, pref4 = parse(["/mll", "https://missav.live/en/abp-123"])
        self.assertEqual(q4, "https://missav.live/en/abp-123")
        self.assertIsNone(pref4)

    def test_non_english_detector(self):
        is_non_eng = leech_mod._is_non_english
        self.assertTrue(is_non_eng("義母と息子の禁断の愛"))
        self.assertTrue(is_non_eng("こんにちは"))
        self.assertFalse(is_non_eng("Mother and Son forbidden love"))
        self.assertFalse(is_non_eng(""))

    def test_forum_topic_name_formatting(self):
        format_name = leech_mod.format_forum_topic_name

        # User's exact prompt example:
        # /mll Tall English Subtitle 360p -> Tall Esub 360p
        self.assertEqual(format_name("Tall English Subtitle", "360p"), "Tall Esub 360p")

        # Mother with plural Subtitles and 720p
        self.assertEqual(format_name("Mother English Subtitles", "720p"), "Mother Esub 720p")

        # Uncensored Leak abbreviation -> UC Leak
        self.assertEqual(format_name("Mother Uncensored Leak English Subtitle", "360p"), "Mother UC Leak Esub 360p")
        self.assertEqual(format_name("Uncensored Leaked Stars", "720p"), "UC Leak Stars 720p")

        # Uncensored abbreviation -> UC
        self.assertEqual(format_name("Tokyo Hot Uncensored", "1080p"), "Tokyo Hot UC 1080p")
        self.assertEqual(format_name("FC2 Uncensored", "720p"), "FC2 UC 720p")

        # Censored abbreviation -> C
        self.assertEqual(format_name("S-Cute Censored", "480p"), "S-Cute C 480p")

        # Without preferred quality
        self.assertEqual(format_name("Mother English Subtitles", None), "Mother Esub")

    def test_missav_archive_extract_code(self):
        extract = archive_mod._extract_video_code
        self.assertEqual(extract("https://missav.live/en/ebwh-373"), "ebwh-373")
        self.assertEqual(extract("https://missav.live/en/dm12/ebwh-373"), "ebwh-373")
        self.assertEqual(extract("https://missav.live/en/fc2-ppv-4993751"), "fc2-ppv-4993751")
        self.assertEqual(extract("EBWH-373 Beautiful Woman"), "ebwh-373")

        item = resolver_mod.MissAVTitle(url="https://missav.live/en/stars-999", title="STARS-999 Star")
        self.assertEqual(extract(item), "stars-999")

    def test_missav_archive_save_and_retrieve(self):
        import asyncio

        item = resolver_mod.MissAVTitle(
            url="https://missav.live/en/test-101",
            title="TEST-101 Title",
        )

        async def run_archive_test():
            # Save record
            await archive_mod.MissAVArchive.save(
                item=item,
                chat_id=-1003887117402,
                message_ids=[1001, 1002, 1003],
                quality="720p",
            )
            # Retrieve by item object
            rec1 = await archive_mod.MissAVArchive.get(item)
            self.assertIsNotNone(rec1)
            self.assertEqual(rec1["code"], "test-101")
            self.assertEqual(rec1["chat_id"], -1003887117402)
            self.assertEqual(rec1["message_ids"], [1001, 1002, 1003])

            # Retrieve by URL string
            rec2 = await archive_mod.MissAVArchive.get("https://missav.live/en/test-101")
            self.assertEqual(rec2["message_ids"], [1001, 1002, 1003])

            # Retrieve by code string
            rec3 = await archive_mod.MissAVArchive.get("test-101")
            self.assertEqual(rec3["message_ids"], [1001, 1002, 1003])
            self.assertFalse(rec3.get("backup_copied", False))

            # Test updating backup status
            await archive_mod.MissAVArchive.update_backup_status("test-101", True)
            rec_updated = await archive_mod.MissAVArchive.get("test-101")
            self.assertTrue(rec_updated.get("backup_copied"))

            # Clean up
            await archive_mod.MissAVArchive.remove("test-101")
            rec_deleted = await archive_mod.MissAVArchive.get("test-101")
            self.assertIsNone(rec_deleted)

        asyncio.run(run_archive_test())

    def test_backup_channel_resolution(self):
        get_backup = leech_mod._get_backup_channel
        # Default backup channel is -1003969712219 (Jav - 1)
        self.assertEqual(get_backup(), -1003969712219)

    def test_search_pagination_discovery(self):
        import asyncio
        from unittest.mock import patch

        class DummyResolver(resolver_mod.MissAVResolver):
            def __init__(self):
                super().__init__()
                self.calls = []

            async def _get(self, url, **kwargs):
                self.calls.append(url)
                return "<html>dummy</html>"

            async def resolve_title(self, link, source=None):
                return resolver_mod.MissAVTitle(url=link, title=link.split('/')[-1].upper(), views=100)

        def mock_links(source, page_url):
            if "page=2" in page_url:
                return [f"https://missav.live/en/card-{i}" for i in range(13, 18)]  # 5 links (last page)
            else:
                return [f"https://missav.live/en/card-{i}" for i in range(1, 13)]  # 12 links (full page)

        async def run_test():
            r = DummyResolver()
            with patch.object(resolver_mod, "_catalog_links", side_effect=mock_links):
                items = await r.discover_query("Mother English Subtitles", max_pages=5)
            # Page 1 (12) + Page 2 (5) = 17 items total
            self.assertEqual(len(items), 17)
            self.assertEqual(len(r.calls), 2)
            self.assertIn("card-1", items[0].url)
            self.assertIn("card-17", items[-1].url)

        asyncio.run(run_test())

    def test_upload_title_deletes_from_main_dump(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        item = resolver_mod.MissAVTitle(url="https://missav.live/en/test-del-1", title="TEST-DEL-1")
        prepared = {
            "item": item,
            "preview_video": None,
            "poster": None,
            "thumb": None,
            "caption": "test caption",
            "caption_replies": [],
            "videos": [],
            "sample_collage": None,
            "quality_label": "720p",
        }

        async def run_test():
            cancel_event = asyncio.Event()
            stage_msg = MagicMock(id=999, chat=MagicMock(id=-1003887117402))
            backup_msg = MagicMock(id=888)
            dest_msg = MagicMock(id=777)

            leech_mod.TgClient.bot.send_message = AsyncMock(return_value=stage_msg)
            leech_mod.TgClient.bot.copy_message = AsyncMock(side_effect=[backup_msg, dest_msg])
            leech_mod.TgClient.bot.delete_messages = AsyncMock(return_value=True)
            leech_mod.Config.LEECH_DUMP_CHAT = "-1003887117402"
            leech_mod.Config.MISSAV_BACKUP_CHANNEL = "-1003969712219"

            await leech_mod._upload_title(prepared, destination=-1004326682090, thread_id=12, cancel_event=cancel_event)

            # Check that delete_messages was called for stage_chat (-1003887117402) with [999]
            leech_mod.TgClient.bot.delete_messages.assert_called_with(
                chat_id=-1003887117402,
                message_ids=[999],
            )

            # Check that archive was saved with backup_channel (-1003969712219) and [888]
            rec = await archive_mod.MissAVArchive.get(item)
            self.assertIsNotNone(rec)
            self.assertEqual(rec["chat_id"], -1003969712219)
            self.assertEqual(rec["message_ids"], [888])

            await archive_mod.MissAVArchive.remove(item)

        asyncio.run(run_test())

    def test_is_valid_tag(self):
        # Ignored tags should return False
        for tag in ["#4k", "4k", "#english_subtitles", "English Subtitles", "english_subtitle", "#hd", "VR", "create_playlist", "exclusive"]:
            self.assertFalse(leech_mod._is_valid_tag(tag), f"Expected {tag} to be invalid")
        
        # Regular tags should return True
        for tag in ["Creampie", "Milf", "Schoolgirl", "Cosplay", "Drama", "Office Lady"]:
            self.assertTrue(leech_mod._is_valid_tag(tag), f"Expected {tag} to be valid")

    def test_extract_code_prefix(self):
        self.assertEqual(leech_mod.extract_code_prefix("SSIS-123"), "SSIS")
        self.assertEqual(leech_mod.extract_code_prefix("stars-999"), "STARS")
        self.assertEqual(leech_mod.extract_code_prefix("ADN-456"), "ADN")
        self.assertEqual(leech_mod.extract_code_prefix("FC2-PPV-1234567"), "FC2-PPV")
        self.assertEqual(leech_mod.extract_code_prefix("1pondo-123456_001"), "1PONDO")
        self.assertEqual(leech_mod.extract_code_prefix("caribbeancom-010123-001"), "CARIBBEANCOM")

    def test_resolve_all_target_topics(self):
        import asyncio
        from unittest.mock import AsyncMock

        item = resolver_mod.MissAVTitle(
            url="https://missav.live/en/ssis-555",
            title="SSIS-555 Beautiful Morning",
            genres=["4k", "English Subtitles", "Creampie", "Exclusive", "Milf"],
            actors=["Yui Hatano", "Meguri"],
        )

        async def run_test():
            # Mock _create_topic to return predictable IDs
            topic_counter = 100
            async def fake_create_topic(chat_id, name):
                nonlocal topic_counter
                topic_counter += 1
                return topic_counter

            orig_create_topic = leech_mod._create_topic
            leech_mod._create_topic = AsyncMock(side_effect=fake_create_topic)
            try:
                targets = await leech_mod._resolve_all_target_topics(item)
                # Targets should contain:
                # 2 for valid genres (Creampie, Milf) in tags_group (-1004486729792)
                # 2 for actresses (Yui Hatano, Meguri) in actress_group (-1003967556547)
                # 1 for code prefix (SSIS) in codes_group (-1004345896581)
                tags_targets = [t for t in targets if t[0] == leech_mod.MISSAV_DEFAULT_TAGS_GROUP]
                act_targets = [t for t in targets if t[0] == leech_mod.MISSAV_DEFAULT_ACTRESS_GROUP]
                code_targets = [t for t in targets if t[0] == leech_mod.MISSAV_DEFAULT_CODES_GROUP]

                self.assertEqual(len(tags_targets), 2)
                self.assertEqual(len(act_targets), 2)
                self.assertEqual(len(code_targets), 1)
                self.assertEqual(len(targets), 5)
            finally:
                leech_mod._create_topic = orig_create_topic

        asyncio.run(run_test())

    def test_discover_pages_streaming(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        def mock_links(source, page_url):
            if "page=2" in page_url:
                return ["https://missav.live/en/card-13"]
            else:
                return [f"https://missav.live/en/card-{i}" for i in range(1, 13)]

        async def run_test():
            r = resolver_mod.MissAVResolver()
            r._get = AsyncMock(return_value="<html>dummy</html>")
            r.resolve_title = AsyncMock(side_effect=lambda u, s=None: resolver_mod.MissAVTitle(url=u, title=u.split('/')[-1]))

            with patch.object(resolver_mod, "_catalog_links", side_effect=mock_links):
                pages_collected = []
                async for p, total, items in r.discover_pages("https://missav.live/en/english-subtitle", max_pages=2):
                    pages_collected.append((p, total, [it.title for it in items]))

            self.assertEqual(len(pages_collected), 2)
            self.assertEqual(pages_collected[0][0], 1)
            self.assertEqual(len(pages_collected[0][2]), 12)
            self.assertEqual(pages_collected[1][0], 2)
            self.assertEqual(pages_collected[1][2], ["card-13"])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
