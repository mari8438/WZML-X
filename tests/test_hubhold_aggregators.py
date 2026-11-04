import base64
import codecs
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, mock_open, patch

_REPO_ROOT = Path(__file__).parents[1]
_EXCEPTIONS_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "exceptions.py"
_DLG_PATH = _REPO_ROOT / "bot" / "helper" / "mirror_leech_utils" / "download_utils" / "direct_link_generator.py"

# Restore any real packages if previously mocked by other tests
for mod_name in ["lxml", "lxml.html", "lxml.etree", "curl_cffi", "curl_cffi.requests"]:
    if mod_name in sys.modules and isinstance(sys.modules[mod_name], MagicMock):
        del sys.modules[mod_name]

# Ensure mock/stub modules for bot hierarchy to avoid running bot/__init__.py side-effects
for mod_name in [
    "bot",
    "bot.core",
    "bot.core.config_manager",
    "bot.helper",
    "bot.helper.ext_utils",
    "bot.helper.mirror_leech_utils",
    "bot.helper.mirror_leech_utils.download_utils",
]:
    if mod_name not in sys.modules or not hasattr(sys.modules[mod_name], "__path__"):
        m = MagicMock()
        m.__path__ = []
        m.__spec__ = importlib.machinery.ModuleSpec(mod_name, None, is_package=True)
        sys.modules[mod_name] = m

# Load exceptions
spec_exc = importlib.util.spec_from_file_location("bot.helper.ext_utils.exceptions", _EXCEPTIONS_PATH)
exc_mod = importlib.util.module_from_spec(spec_exc)
exc_mod.__package__ = "bot.helper.ext_utils"
sys.modules["bot.helper.ext_utils.exceptions"] = exc_mod
spec_exc.loader.exec_module(exc_mod)

DirectDownloadLinkException = exc_mod.DirectDownloadLinkException

# Mock remaining ext_utils submodules needed by direct_link_generator
sys.modules["bot.helper.ext_utils.help_messages"] = MagicMock()
mock_links = MagicMock()
mock_links.is_share_link = lambda x: False
sys.modules["bot.helper.ext_utils.links_utils"] = mock_links
sys.modules["bot.helper.ext_utils.status_utils"] = MagicMock()
mock_bypass = MagicMock()
mock_bypass.is_url_shortener = lambda x: False
sys.modules["bot.helper.mirror_leech_utils.download_utils.url_shortener_bypass"] = mock_bypass

# Load direct_link_generator
spec_dlg = importlib.util.spec_from_file_location("bot.helper.mirror_leech_utils.download_utils.direct_link_generator", _DLG_PATH)
dlg = importlib.util.module_from_spec(spec_dlg)
dlg.__package__ = "bot.helper.mirror_leech_utils.download_utils"
sys.modules["bot.helper.mirror_leech_utils.download_utils.direct_link_generator"] = dlg
spec_dlg.loader.exec_module(dlg)


class TestHubHoldAggregators(unittest.TestCase):
    def test_decode_greenmotors_payload(self):
        target_hub_url = "https://hubcloud.ist/drive/sample123"
        o_encoded = base64.b64encode(target_hub_url.encode()).decode()
        payload_data = {"w": 10, "l": "https://greenmotors.cc/homelander/", "o": o_encoded}

        # Encoding sequence: json -> b64encode -> rot13 -> b64encode -> b64encode
        s4 = json.dumps(payload_data)
        s3 = base64.b64encode(s4.encode()).decode()
        s2 = codecs.encode(s3, "rot_13")
        s1 = base64.b64encode(s2.encode()).decode()
        full_payload = base64.b64encode(s1.encode()).decode()

        decoded = dlg._decode_greenmotors_payload(full_payload)
        self.assertEqual(decoded, target_hub_url)

    def test_toonworld_shortener_gate_detected(self):
        sample_url = "https://archive.toonworld4all.me/redirect/testtoken123"
        html = '''
        <html>
        <script>
        window.__PAGE__ = "redirect";
        window.__PROPS__ = {"userSystem":"24hour","destination":"https://shrinkme.click/test","link":{"domain":"https://hubcloud.ist/drive/","hidden":"tb0zd2am3c76l5s"},"total":4};
        </script>
        </html>
        '''

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html

        with patch.object(dlg, "Session") as mock_session_cls:
            mock_session = mock_session_cls.return_value
            mock_session.get.return_value = mock_resp

            with self.assertRaises(DirectDownloadLinkException) as ctx:
                dlg.bypass_hubhold_aggregators(sample_url)
            self.assertIn("protected by shortener gate", str(ctx.exception))
            self.assertIn("https://shrinkme.click/test", str(ctx.exception))

    @patch.object(dlg, "hubcloud")
    def test_toonworld_direct_destination_bypass(self, mock_hubcloud):
        mock_hubcloud.side_effect = lambda u: f"DIRECT:{u}"
        sample_url = "https://archive.toonworld4all.me/redirect/testtoken123"
        html = '''
        <html>
        <script>
        window.__PAGE__ = "redirect";
        window.__PROPS__ = {"userSystem":"24hour","destination":"https://hubcloud.ist/drive/realfile456","link":{"domain":"https://hubcloud.ist/drive/","hidden":"decoy789"},"total":4};
        </script>
        </html>
        '''

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html

        with patch.object(dlg, "Session") as mock_session_cls:
            mock_session = mock_session_cls.return_value
            mock_session.get.return_value = mock_resp

            result = dlg.bypass_hubhold_aggregators(sample_url)
            self.assertEqual(result, "DIRECT:https://hubcloud.ist/drive/realfile456")
            mock_hubcloud.assert_called_once_with("https://hubcloud.ist/drive/realfile456")

    @patch.object(dlg, "hubcloud")
    def test_toonworld_redirect_with_cookie_bypass(self, mock_hubcloud):
        mock_hubcloud.side_effect = lambda u: f"DIRECT:{u}"
        sample_url = "https://archive.toonworld4all.me/redirect/testtoken123"

        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://hubcloud.ist/drive/unlockedmovie999"}
        mock_resp.url = sample_url

        with patch.object(dlg, "Session") as mock_session_cls:
            mock_session = mock_session_cls.return_value
            mock_session.get.return_value = mock_resp

            result = dlg.bypass_hubhold_aggregators(sample_url)
            self.assertEqual(result, "DIRECT:https://hubcloud.ist/drive/unlockedmovie999")
            mock_hubcloud.assert_called_once_with("https://hubcloud.ist/drive/unlockedmovie999")

    @patch.object(dlg, "filepress")
    def test_toonworld_filepress_dispatch(self, mock_filepress):
        mock_filepress.side_effect = lambda u: f"FILEPRESS_DIRECT:{u}"
        sample_url = "https://archive.toonworld4all.me/redirect/testtoken123"

        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://new2.filepress.lat/file/sample123"}
        mock_resp.url = sample_url

        with patch.object(dlg, "Session") as mock_session_cls:
            mock_session = mock_session_cls.return_value
            mock_session.get.return_value = mock_resp

            result = dlg.bypass_hubhold_aggregators(sample_url)
            self.assertEqual(result, "FILEPRESS_DIRECT:https://new2.filepress.lat/file/sample123")
            mock_filepress.assert_called_once_with("https://new2.filepress.lat/file/sample123")

    @patch.object(dlg, "hubcloud")
    def test_xdmovies_redirect_bypass(self, mock_hubcloud):
        mock_hubcloud.side_effect = lambda u: f"DIRECT:{u}"
        sample_url = "https://link.xdmovies.wtf/download/sampleid123"

        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://hubcloud.ist/drive/directmovie789"}
        mock_resp.url = sample_url

        with patch.object(dlg, "Session") as mock_session_cls:
            mock_session = mock_session_cls.return_value
            mock_session.get.return_value = mock_resp

            result = dlg.bypass_hubhold_aggregators(sample_url)
            self.assertEqual(result, "DIRECT:https://hubcloud.ist/drive/directmovie789")
            mock_hubcloud.assert_called_once_with("https://hubcloud.ist/drive/directmovie789")

    @patch.object(dlg, "bypass_hubhold_aggregators")
    def test_direct_link_generator_routing(self, mock_bypass):
        mock_bypass.return_value = "https://direct.download.link/file.mkv"

        urls = [
            "https://link.xdmovies.wtf/download/abc",
            "https://greenmotors.club/?id=xyz",
            "https://archive.toonworld4all.me/redirect/123",
        ]

        for url in urls:
            res = dlg.direct_link_generator(url)
            self.assertEqual(res, "https://direct.download.link/file.mkv")

        self.assertEqual(mock_bypass.call_count, 3)

    def test_load_domain_cookies(self):
        cookies_txt_content = (
            "# Netscape HTTP Cookie File\n"
            ".toonworld4all.me\tTRUE\t/\tTRUE\t1793858087\tuser\tsampleusertoken123\n"
            ".otherdomain.com\tTRUE\t/\tFALSE\t1793858087\tsession\tothersession456\n"
        )
        with patch("os.path.isfile", side_effect=lambda p: p == "cookies.txt"):
            with patch("builtins.open", mock_open(read_data=cookies_txt_content)):
                cookies = dlg._load_domain_cookies(["toonworld4all"])
                self.assertEqual(cookies, {"user": "sampleusertoken123"})

    def test_exception_on_unresolvable_url(self):
        sample_url = "https://link.xdmovies.wtf/download/invalid"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>Empty page with no links</body></html>"

        with patch.object(dlg, "Session") as mock_session_cls:
            mock_session = mock_session_cls.return_value
            mock_session.get.return_value = mock_resp

            with self.assertRaises(DirectDownloadLinkException):
                dlg.bypass_hubhold_aggregators(sample_url)


if __name__ == "__main__":
    unittest.main()
