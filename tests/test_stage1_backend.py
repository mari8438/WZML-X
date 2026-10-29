import os
import time
import unittest

os.environ["BOT_TOKEN"] = "123456789:AAH_test_secret_bot_token_starfall"
os.environ["WEB_ACCESS_PASSWORD"] = "starfall_super_secret"
os.environ["BASE_URL"] = "https://studio.starfall.anime"

from bot.helper.ext_utils.token_manager import (
    generate_user_token,
    verify_user_token,
    inspect_user_token,
    build_studio_url,
)


class TestStarfallTokenManager(unittest.TestCase):
    def test_generate_and_verify_valid_token(self):
        uid = 987654321
        token = generate_user_token(uid, expiry_days=7)
        self.assertIn(".", token)
        verified_uid = verify_user_token(token)
        self.assertEqual(verified_uid, uid)

        meta = inspect_user_token(token)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["user_id"], uid)
        self.assertGreater(meta["expires_at"], int(time.time()))

    def test_tampered_token_returns_none(self):
        token = generate_user_token(11223344, expiry_days=7)
        payload, sig = token.split(".", 1)
        flipped_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
        self.assertIsNone(verify_user_token(f"{payload}.{flipped_sig}"))
        self.assertIsNone(verify_user_token(f"{payload}a.{sig}"))
        self.assertIsNone(verify_user_token("invalid_token"))
        self.assertIsNone(verify_user_token(""))

    def test_build_studio_url(self):
        url = build_studio_url(55667788, "/app/user")
        self.assertTrue(url.startswith("https://studio.starfall.anime/app/user?token="))
        token = url.split("token=", 1)[1]
        self.assertEqual(verify_user_token(token), 55667788)


if __name__ == "__main__":
    unittest.main()
