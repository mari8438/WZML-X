import base64
import codecs
import json
import unittest
from unittest.mock import MagicMock, patch

from bot.helper.ext_utils.exceptions import DirectDownloadLinkException
from bot.helper.mirror_leech_utils.download_utils import direct_link_generator as dlg


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

    @patch.object(dlg, "hubcloud")
    def test_toonworld_props_bypass(self, mock_hubcloud):
        mock_hubcloud.side_effect = lambda u: f"DIRECT:{u}"
        sample_url = "https://archive.toonworld4all.me/redirect/testtoken123"
        html = '''
        <html>
        <script>
        window.__PAGE__ = "redirect";
        window.__PROPS__ = {"userSystem":"24hour","destination":"https://shrinkme.click/test","link":{"domain":"https://hubcloud.ist/drive/","hidden":"hiddenfile456"},"total":4};
        </script>
        </html>
        '''

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html

        with patch("bot.helper.mirror_leech_utils.download_utils.direct_link_generator.Session") as mock_session_cls:
            mock_session = mock_session_cls.return_value
            mock_session.get.return_value = mock_resp

            result = dlg.bypass_hubhold_aggregators(sample_url)
            self.assertEqual(result, "DIRECT:https://hubcloud.ist/drive/hiddenfile456")
            mock_hubcloud.assert_called_once_with("https://hubcloud.ist/drive/hiddenfile456")

    @patch.object(dlg, "hubcloud")
    def test_xdmovies_redirect_bypass(self, mock_hubcloud):
        mock_hubcloud.side_effect = lambda u: f"DIRECT:{u}"
        sample_url = "https://link.xdmovies.wtf/download/sampleid123"
        
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://hubcloud.ist/drive/directmovie789"}

        with patch("bot.helper.mirror_leech_utils.download_utils.direct_link_generator.Session") as mock_session_cls:
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

    def test_exception_on_unresolvable_url(self):
        sample_url = "https://link.xdmovies.wtf/download/invalid"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>Empty page with no links</body></html>"

        with patch("bot.helper.mirror_leech_utils.download_utils.direct_link_generator.Session") as mock_session_cls:
            mock_session = mock_session_cls.return_value
            mock_session.get.return_value = mock_resp

            with self.assertRaises(DirectDownloadLinkException):
                dlg.bypass_hubhold_aggregators(sample_url)


if __name__ == "__main__":
    unittest.main()
