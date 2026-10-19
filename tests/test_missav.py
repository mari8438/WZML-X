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


if __name__ == "__main__":
    unittest.main()
