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
    def test_fc2_filter(self):
        is_fc2 = resolver_mod.is_fc2_title
        # Should be filtered out
        self.assertTrue(is_fc2("FC2-PPV-4993751"))
        self.assertTrue(is_fc2("FC2PPV-123456"))
        self.assertTrue(is_fc2("fc2-ppv-4993188"))
        self.assertTrue(is_fc2("https://missav.live/en/fc2-ppv-4993751"))
        self.assertTrue(is_fc2("Something FC2 Amateur video"))

        # Should NOT be filtered out (keep uncensored studio releases)
        self.assertFalse(is_fc2("EBWH-373 Uncensored Leaked"))
        self.assertFalse(is_fc2("FNS-270"))
        self.assertFalse(is_fc2("MAAN-1200"))
        self.assertFalse(is_fc2("JERA-049"))
        self.assertFalse(is_fc2("https://missav.live/en/ebwh-373"))

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
        self.assertIn("🄰🅂- EBWH-373 Beautiful Woman In Love [1080p] [Jap] ESub ~ [@Anime_Starfall🥰].mkv", filename_esub)

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


if __name__ == "__main__":
    unittest.main()
