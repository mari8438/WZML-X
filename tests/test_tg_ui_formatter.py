import ast
import asyncio
import importlib.util
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).parents[1]

_MODULE_PATH = _REPO_ROOT / "bot" / "helper" / "telegram_helper" / "tg_ui_formatter.py"
_SPEC = importlib.util.spec_from_file_location("tg_ui_formatter", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

format_card = _MODULE.format_card
is_premium_session = _MODULE.is_premium_session
publish_telegraph_article = _MODULE.publish_telegraph_article
safe_emoji = _MODULE.safe_emoji
PREMIUM_MOVIE = _MODULE.PREMIUM_MOVIE
PREMIUM_PACKAGE = _MODULE.PREMIUM_PACKAGE

_HELP_PATH = _REPO_ROOT / "bot" / "helper" / "ext_utils" / "help_messages.py"
_HELP_CONTENT = _HELP_PATH.read_text(encoding="utf-8")
_HELP_TREE = ast.parse(_HELP_CONTENT)


class TgUIFormatterTest(TestCase):
    def test_is_premium_session_with_non_premium_client(self):
        client = MagicMock()
        client.me.is_premium = False
        client.me.is_bot = False
        self.assertFalse(is_premium_session(client))

    def test_is_premium_session_with_bot_account(self):
        # Standard bot accounts cannot render custom tg-emoji without premium user session
        client = MagicMock()
        client.me.is_premium = True
        client.me.is_bot = True
        self.assertFalse(is_premium_session(client))

    def test_is_premium_session_with_premium_user_client(self):
        client = MagicMock()
        client.me.is_premium = True
        client.me.is_bot = False
        self.assertTrue(is_premium_session(client))

    def test_safe_emoji_strict_non_premium_fallback(self):
        client = MagicMock()
        client.me.is_premium = False
        client.me.is_bot = False
        emoji = safe_emoji(PREMIUM_MOVIE, "🎬", client=client)
        self.assertEqual(emoji, "🎬")
        self.assertNotIn("<tg-emoji", emoji)

    def test_safe_emoji_premium_output(self):
        client = MagicMock()
        client.me.is_premium = True
        client.me.is_bot = False
        emoji = safe_emoji(PREMIUM_PACKAGE, "📦", client=client)
        self.assertEqual(
            emoji, f'<tg-emoji id="{PREMIUM_PACKAGE}">📦</tg-emoji>'
        )

    def test_format_card_expandable(self):
        fields = {
            "Size": "1.5 GB",
            "Elapsed": "42s",
            "Upload": "Hybrid",
            "User": "@testuser",
        }
        card = format_card(
            header="✦ STARFALLX • TASK COMPLETE ✦",
            title="🎬 Movie.2026.1080p.mkv",
            fields_dict=fields,
            footer="StarfallX Engine",
            expandable=True,
        )
        self.assertIn("<b>✦ STARFALLX • TASK COMPLETE ✦</b>", card)
        self.assertIn("<blockquote expandable>", card)
        self.assertIn("</blockquote>", card)
        self.assertIn("┌ <b>Size</b> : 1.5 GB", card)
        self.assertIn("├ <b>Elapsed</b> : 42s", card)
        self.assertIn("├ <b>Upload</b> : Hybrid", card)
        self.assertIn("└ <b>User</b> : @testuser", card)
        self.assertIn("<i>StarfallX Engine</i>", card)

    def test_format_card_non_expandable(self):
        fields = {"Status": "Online"}
        card = format_card(
            header="System Status",
            title=None,
            fields_dict=fields,
            expandable=False,
        )
        self.assertIn("<blockquote>", card)
        self.assertNotIn("<blockquote expandable>", card)
        self.assertIn("• <b>Status</b> : Online", card)

    def test_publish_telegraph_article_success(self):
        async def _run():
            mock_telegraph = MagicMock()
            mock_telegraph._telegraph = MagicMock()
            mock_telegraph.create_page = AsyncMock(
                return_value={
                    "path": "test-path-123",
                    "url": "https://graph.org/test-path-123",
                }
            )
            mock_mod = MagicMock()
            mock_mod.telegraph = mock_telegraph
            with patch.dict(
                sys.modules, {"bot.helper.ext_utils.telegraph_helper": mock_mod}
            ):
                url = await publish_telegraph_article(
                    "Test Title", "<p>Hello</p>"
                )
                self.assertEqual(url, "https://graph.org/test-path-123")

        asyncio.run(_run())

    def test_publish_telegraph_article_handles_error(self):
        async def _run():
            mock_telegraph = MagicMock()
            mock_telegraph._telegraph = MagicMock()
            mock_telegraph.create_page = AsyncMock(
                side_effect=Exception("API Error")
            )
            mock_mod = MagicMock()
            mock_mod.telegraph = mock_telegraph
            with patch.dict(
                sys.modules, {"bot.helper.ext_utils.telegraph_helper": mock_mod}
            ):
                url = await publish_telegraph_article("Title", "<p>Err</p>")
                self.assertIsNone(url)

        asyncio.run(_run())

    def test_help_messages_overview_is_expandable(self):
        self.assertIn("<blockquote expandable>", _HELP_CONTENT)
        self.assertIn("✦ STARFALLX • COMMANDS & USER MANUAL ✦", _HELP_CONTENT)

    def test_category_help_messages(self):
        for cat in ("leech", "mirror", "videotools", "settings"):
            self.assertIn(f'"{cat}"', _HELP_CONTENT)
        self.assertIn("CATEGORY_HELP_DICT", _HELP_CONTENT)

    def test_telegraph_help_html_generator(self):
        self.assertIn("def get_full_telegraph_help_html():", _HELP_CONTENT)
        self.assertIn("StarfallX v1.2", _HELP_CONTENT)
        self.assertIn("Leech Commands", _HELP_CONTENT)
        self.assertIn("Video Tools", _HELP_CONTENT)
