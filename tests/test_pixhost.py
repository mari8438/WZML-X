import importlib.util
from pathlib import Path
import unittest
from unittest import IsolatedAsyncioTestCase

if importlib.util.find_spec("httpx") is None:
    raise unittest.SkipTest("Pixhost tests require the bot dependency httpx")


MODULE_PATH = Path(__file__).parents[1] / "bot" / "helper" / "ext_utils" / "pixhost.py"
SPEC = importlib.util.spec_from_file_location("pixhost_under_test", MODULE_PATH)
pixhost = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pixhost)


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class PixhostValidationTest(IsolatedAsyncioTestCase):
    def test_direct_url_rules(self):
        self.assertEqual(
            pixhost.validate_pixhost_url("https://t1.pixhost.cc/thumbs/1/a.jpg"),
            "https://t1.pixhost.cc/thumbs/1/a.jpg",
        )
        for value in (
            "http://t1.pixhost.cc/thumbs/1/a.jpg",
            "https://pixhost.to/show/1/a.jpg",
            "https://t1.pixhost.cc/thumbs/1/a.jpg?manage=secret",
            "https://example.com/a.jpg",
            "https://t1.pixhost.cc/thumbs/1/a.txt",
        ):
            with self.subTest(value=value), self.assertRaises(pixhost.PixhostError):
                pixhost.validate_pixhost_url(value)

    async def test_upload_sends_required_multipart_options(self):
        image_path = Path(self.id().replace(".", "_") + ".jpg")
        image_path.write_bytes(b"test image")
        try:
            client = FakeClient(FakeResponse({"th_url": "https://t1.pixhost.cc/thumbs/1/a.jpg"}))
            result = await pixhost.upload_image(str(image_path), client=client)
            self.assertTrue(result.endswith("a.jpg"))
            _, kwargs = client.calls[0]
            self.assertEqual(kwargs["data"]["content_type"], "1")
            self.assertEqual(kwargs["data"]["max_th_size"], "500")
            self.assertEqual(kwargs["data"]["optimize_for_web"], "1")
            self.assertEqual(kwargs["headers"]["Accept"], "application/json")
        finally:
            image_path.unlink(missing_ok=True)

    async def test_invalid_response_is_rejected(self):
        image_path = Path(self.id().replace(".", "_") + ".jpg")
        image_path.write_bytes(b"test image")
        try:
            client = FakeClient(FakeResponse({"th_url": "https://pixhost.to/show/1/a.jpg"}))
            with self.assertRaises(pixhost.PixhostError):
                await pixhost.upload_image(str(image_path), client=client)
        finally:
            image_path.unlink(missing_ok=True)
