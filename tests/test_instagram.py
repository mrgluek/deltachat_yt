import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch, ANY

# Mock deltabot_cli and deltachat2 if not installed
try:
    import deltachat2
except ImportError:
    sys.modules['deltachat2'] = MagicMock()

try:
    import deltabot_cli
except ImportError:
    class MockBotCli:
        def __init__(self, *args, **kwargs):
            pass
        def on(self, *args, **kwargs):
            return lambda func: func
        def on_init(self, func):
            return func
        def on_start(self, func):
            return func
        def start(self):
            pass
    mock_deltabot_cli = MagicMock()
    mock_deltabot_cli.BotCli = MockBotCli
    sys.modules['deltabot_cli'] = mock_deltabot_cli

import database
import bot


class TestInstagramHandling(unittest.TestCase):

    def setUp(self):
        self.test_db = "test_ytbot_instagram.db"
        database.DB_PATH = self.test_db
        database.init_db()
        self.temp_dir = tempfile.mkdtemp(prefix="ig_test_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        if os.path.exists(self.test_db):
            try:
                os.remove(self.test_db)
            except OSError:
                pass
        for sidecar in [f"{self.test_db}-wal", f"{self.test_db}-shm"]:
            if os.path.exists(sidecar):
                try:
                    os.remove(sidecar)
                except OSError:
                    pass

    def test_version_bumped(self):
        """Test bot.VERSION is 1.6.51."""
        self.assertEqual(bot.VERSION, "1.6.51")

    def test_clean_error_no_video_in_post(self):
        """Test _clean_error formats 'There is no video in this post' nicely."""
        raw_err = "ERROR: [Instagram] Dcscta2oz5v: There is no video in this post"
        cleaned = bot._clean_error(raw_err)
        self.assertEqual(cleaned, "Instagram error: There is no video in this post.")

    def test_supported_url_re_matches_instagram(self):
        """Test SUPPORTED_URL_RE matches various Instagram and instagr.am formats."""
        urls = [
            "https://www.instagram.com/reel/C8mZ5p2Nl5h/",
            "https://instagram.com/p/Dcscta2oz5v/",
            "https://instagr.am/reel/12345/",
            "https://instagr.am/p/ABCxyz/",
        ]
        for u in urls:
            match = bot.SUPPORTED_URL_RE.search(u)
            self.assertIsNotNone(match, f"Failed to match {u}")
            self.assertEqual(match.group(0), u)

    @patch("bot._get_fallback_configs")
    @patch("bot._fetch_video_info")
    def test_fetch_video_info_fast_fails_on_no_video(self, mock_fetch, mock_configs):
        """Verify _fetch_video_info_with_fallback does not retry other configs on photo posts."""
        mock_configs.return_value = [
            {"use_cookies": True, "proxy": None, "desc": "default proxy with cookies"},
            {"use_cookies": False, "proxy": None, "desc": "default proxy without cookies"},
            {"use_cookies": False, "proxy": "http://backup", "desc": "backup proxy"},
        ]
        async def fake_fetch(vid, use_cookies=True, custom_proxy=None):
            return None, "ERROR: [Instagram] test: There is no video in this post"
        mock_fetch.side_effect = fake_fetch

        loop = asyncio.new_event_loop()
        try:
            info, error, idx = loop.run_until_complete(
                bot._fetch_video_info_with_fallback("https://www.instagram.com/p/Dcscta2oz5v/")
            )
        finally:
            loop.close()

        self.assertIsNone(info)
        self.assertIn("There is no video in this post", error)
        # Should only have been called ONCE due to fast-failure
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("bot._send")
    @patch("bot._react")
    @patch("bot._fetch_video_info_with_fallback")
    def test_handle_link_info_clears_reaction_on_photo_post(self, mock_fetch, mock_react, mock_send):
        """Verify _handle_link_info removes reaction and does not send error for photo posts."""
        async def fake_fetch(vid):
            return None, "Instagram error: There is no video in this post.", 0
        mock_fetch.side_effect = fake_fetch

        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.id = 123
        mock_msg.chat_id = 456

        bot._handle_link_info(mock_bot, 1, mock_msg, "https://www.instagram.com/p/Dcscta2oz5v/")

        # Reaction should be cleared (empty string)
        mock_react.assert_called_once_with(mock_bot, 1, 123, "")
        # No error message should be sent to the chat
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
