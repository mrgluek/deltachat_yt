import os
import sys
import unittest
import tempfile
import shutil
import json
import asyncio
from unittest.mock import MagicMock, patch

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


class TestChapterSlicing(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.test_db = "test_chapters_ytbot.db"
        database.DB_PATH = self.test_db
        database.init_db()

    def tearDown(self):
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

    def test_parse_timestamp_to_seconds(self):
        """Test timestamp string to seconds conversions."""
        self.assertEqual(bot._parse_timestamp_to_seconds("0:00"), 0)
        self.assertEqual(bot._parse_timestamp_to_seconds("00:00"), 0)
        self.assertEqual(bot._parse_timestamp_to_seconds("4:39"), 279)
        self.assertEqual(bot._parse_timestamp_to_seconds("08:03"), 483)
        self.assertEqual(bot._parse_timestamp_to_seconds("1:02:04"), 3724)
        self.assertEqual(bot._parse_timestamp_to_seconds("01:19:52"), 4792)

    def test_extract_chapters_from_description_multiline(self):
        """Test parsing chapters from multiline description text."""
        desc = """
        ──────────────────────── 🎵 Tracklist ────────────────────────
        0:00 煙霞 - Smoky Haze
        4:39 珈琲 - Black Coffee
        8:03 霧港 - Foggy Harbor
        1:02:04 霧雨 - Drizzle
        """
        chapters = bot._extract_chapters_from_description(desc, total_duration=4000)
        self.assertEqual(len(chapters), 4)
        self.assertEqual(chapters[0], {"start_time": 0, "end_time": 279, "title": "煙霞 - Smoky Haze"})
        self.assertEqual(chapters[1], {"start_time": 279, "end_time": 483, "title": "珈琲 - Black Coffee"})
        self.assertEqual(chapters[2], {"start_time": 483, "end_time": 3724, "title": "霧港 - Foggy Harbor"})
        self.assertEqual(chapters[3], {"start_time": 3724, "end_time": 4000, "title": "霧雨 - Drizzle"})

    def test_extract_chapters_from_description_inline_markdown(self):
        """Test parsing chapters from inline markdown timestamp links (as in YouTube descriptions)."""
        desc = "[0:00](https://www.youtube.com/watch?v=KtflOe5C7RM) 煙霞 - Smoky Haze [4:39](https://www.youtube.com/watch?v=KtflOe5C7RM&t=279s) 珈琲 - Black Coffee [8:03](https://www.youtube.com/watch?v=KtflOe5C7RM&t=483s) 霧港 - Foggy Harbor"
        chapters = bot._extract_chapters_from_description(desc, total_duration=800)
        self.assertEqual(len(chapters), 3)
        self.assertEqual(chapters[0], {"start_time": 0, "end_time": 279, "title": "煙霞 - Smoky Haze"})
        self.assertEqual(chapters[1], {"start_time": 279, "end_time": 483, "title": "珈琲 - Black Coffee"})
        self.assertEqual(chapters[2], {"start_time": 483, "end_time": 800, "title": "霧港 - Foggy Harbor"})

    def test_get_video_chapters_from_info_chapters(self):
        """Test extracting chapters from yt-dlp native chapters dict."""
        info = {
            "duration": 500,
            "chapters": [
                {"start_time": 0.0, "end_time": 200.0, "title": "Chapter One"},
                {"start_time": 200.0, "end_time": 500.0, "title": "Chapter Two"}
            ]
        }
        chapters = bot._get_video_chapters(info)
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0], {"start_time": 0, "end_time": 200, "title": "Chapter One"})
        self.assertEqual(chapters[1], {"start_time": 200, "end_time": 500, "title": "Chapter Two"})

    def test_find_chapter_matching(self):
        """Test matching start_time and end_time to chapters."""
        chapters = [
            {"start_time": 0, "end_time": 279, "title": "Track 1"},
            {"start_time": 279, "end_time": 483, "title": "Track 2"},
            {"start_time": 483, "end_time": 752, "title": "Track 3"}
        ]
        # Exact match
        idx, ch = bot._find_chapter(chapters, 279, 483)
        self.assertEqual(idx, 1)
        self.assertEqual(ch["title"], "Track 2")

        # Fuzzy match (within 2s)
        idx, ch = bot._find_chapter(chapters, 280, None)
        self.assertEqual(idx, 1)
        self.assertEqual(ch["title"], "Track 2")

        # Time inside range
        idx, ch = bot._find_chapter(chapters, 500, None)
        self.assertEqual(idx, 2)
        self.assertEqual(ch["title"], "Track 3")

        # None start_time
        idx, ch = bot._find_chapter(chapters, None, None)
        self.assertIsNone(idx)
        self.assertIsNone(ch)

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=True)
    def test_display_link_info_with_chapters_initial_preview(self, mock_is_admin, mock_send):
        """Test that link preview without time params offers Track 1 when chapters exist."""
        info = {
            "title": "Japanese Jazz Mix",
            "duration": 4900,
            "chapters": [
                {"start_time": 0.0, "end_time": 279.0, "title": "煙霞 - Smoky Haze"},
                {"start_time": 279.0, "end_time": 483.0, "title": "珈琲 - Black Coffee"}
            ]
        }
        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.chat_id = 101
        mock_msg.from_id = 202

        bot._display_link_info(mock_bot, 1, mock_msg, "KtflOe5C7RM", info, None)

        mock_send.assert_called_once()
        text = mock_send.call_args[0][3]
        self.assertIn("Japanese Jazz Mix", text)
        self.assertIn("2 tracks", text)
        self.assertIn("Track 1: 煙霞 - Smoky Haze (00:00-04:39)", text)
        self.assertIn("/yt_KtflOe5C7RM_0_279", text)
        self.assertIn("/ytm_KtflOe5C7RM_0_279", text)
        self.assertIn("/ytms_KtflOe5C7RM_0_279", text)

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=True)
    def test_display_link_info_with_specific_chapter_timestamp(self, mock_is_admin, mock_send):
        """Test that link preview with t=279s offers Track 2 directly."""
        info = {
            "title": "Japanese Jazz Mix",
            "duration": 4900,
            "chapters": [
                {"start_time": 0.0, "end_time": 279.0, "title": "煙霞 - Smoky Haze"},
                {"start_time": 279.0, "end_time": 483.0, "title": "珈琲 - Black Coffee"}
            ]
        }
        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.chat_id = 101
        mock_msg.from_id = 202

        bot._display_link_info(mock_bot, 1, mock_msg, "https://www.youtube.com/watch?v=KtflOe5C7RM&t=279s", info, None)

        mock_send.assert_called_once()
        text = mock_send.call_args[0][3]
        self.assertIn("Track 2. 珈琲 - Black Coffee", text)
        self.assertIn("/yt_KtflOe5C7RM_279_483", text)
        self.assertIn("/ytm_KtflOe5C7RM_279_483", text)
        self.assertIn("/ytms_KtflOe5C7RM_279_483", text)

    @patch('bot._send')
    @patch('bot._react')
    @patch('bot._is_dc_admin', return_value=True)
    async def test_send_from_cache_next_track_suggestion(self, mock_is_admin, mock_react, mock_send):
        """Test sending a track slice from cache offers the next track command."""
        tmpdir = tempfile.mkdtemp(prefix="cache_test_")
        try:
            fake_audio = os.path.join(tmpdir, "test.opus")
            with open(fake_audio, "wb") as f:
                f.write(b"AUDIO_BYTES")

            info = {
                "title": "Japanese Jazz Mix",
                "duration": 4900,
                "chapters": [
                    {"start_time": 0.0, "end_time": 279.0, "title": "煙霞 - Smoky Haze"},
                    {"start_time": 279.0, "end_time": 483.0, "title": "珈琲 - Black Coffee"}
                ]
            }
            mock_bot = MagicMock()
            mock_msg = MagicMock()
            mock_msg.chat_id = 101
            mock_msg.from_id = 202
            mock_msg.id = 555

            await bot._send_from_cache(
                mock_bot, 1, mock_msg,
                "https://youtu.be/KtflOe5C7RM?start=0&end=279",
                "audio", fake_audio, info=info
            )

            mock_send.assert_called_once()
            caption = mock_send.call_args[0][3]
            self.assertIn("煙霞 - Smoky Haze [00:00-04:39]", caption)
            self.assertIn("Next track: 珈琲 - Black Coffee (04:39-08:03): /ytm_KtflOe5C7RM_279_483", caption)
            self.assertIn("Save to Navidrome: /ytms_KtflOe5C7RM_279_483", caption)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_to_navidrome_with_chapter(self):
        """Test saving a track to Navidrome uses chapter title for filename and tags."""
        tmpdir = tempfile.mkdtemp(prefix="nav_chapter_test_")
        music_dir = os.path.join(tmpdir, "music_library")
        os.makedirs(music_dir, exist_ok=True)
        try:
            fake_audio = os.path.join(tmpdir, "track.opus")
            with open(fake_audio, "wb") as f:
                f.write(b"AUDIO_DATA")

            info = {
                "title": "Japanese Jazz Compilation",
                "uploader": "Jazz Channel",
                "duration": 4900,
                "chapters": [
                    {"start_time": 0.0, "end_time": 279.0, "title": "煙霞 - Smoky Haze"},
                    {"start_time": 279.0, "end_time": 483.0, "title": "珈琲 - Black Coffee"}
                ]
            }

            dest_path, dest_lrc, err = bot._save_to_navidrome(
                fake_audio, info, music_dir,
                video_id="https://youtu.be/KtflOe5C7RM?start=0&end=279"
            )

            self.assertIsNone(err)
            self.assertIsNotNone(dest_path)
            self.assertTrue(os.path.exists(dest_path))
            self.assertIn("煙霞 - Smoky Haze.opus", dest_path)
            self.assertIn("Jazz Channel", dest_path)
            self.assertIn("Japanese Jazz Compilation", dest_path)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
