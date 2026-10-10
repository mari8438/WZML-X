import importlib.util
import unittest

if importlib.util.find_spec("apscheduler") is None:
    raise unittest.SkipTest("Auto-process tests require the bot runtime dependencies")

from bot.helper.ext_utils.files_utils import _is_protected_download_path


class AutoProcessSafetyTest(unittest.TestCase):
    def test_seeding_path_is_protected_from_generic_cleanup(self):
        self.assertTrue(_is_protected_download_path("/usr/src/app/torrents/seeding"))
        self.assertTrue(
            _is_protected_download_path(
                "/usr/src/app/torrents/seeding/episode/source.mkv"
            )
        )

    def test_temporary_download_path_is_not_protected(self):
        self.assertFalse(_is_protected_download_path("/usr/src/app/downloads/17874"))


if __name__ == "__main__":
    unittest.main()
