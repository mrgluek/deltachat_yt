"""
Unit tests for cache cleaner and cache limits in deltachat_yt.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
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


class TestCacheCleaner(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_yt_cache_")
        self.orig_cache_dir = bot.CACHE_DIR
        self.orig_thumb_dir = bot.THUMB_CACHE_DIR
        self.orig_max_size = bot.CACHE_MAX_SIZE

        bot.CACHE_DIR = os.path.join(self.test_dir, "cache")
        bot.THUMB_CACHE_DIR = os.path.join(self.test_dir, "thumbnails")
        os.makedirs(bot.CACHE_DIR, exist_ok=True)
        os.makedirs(bot.THUMB_CACHE_DIR, exist_ok=True)

    def tearDown(self):
        bot.CACHE_DIR = self.orig_cache_dir
        bot.THUMB_CACHE_DIR = self.orig_thumb_dir
        bot.CACHE_MAX_SIZE = self.orig_max_size
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cache_constants_defined(self):
        """Verify CACHE_MAX_SIZE and MAX_CACHE_SIZE are defined and equal."""
        self.assertTrue(hasattr(bot, "CACHE_MAX_SIZE"))
        self.assertTrue(hasattr(bot, "MAX_CACHE_SIZE"))
        self.assertEqual(bot.CACHE_MAX_SIZE, 2 * 1024 * 1024 * 1024)
        self.assertEqual(bot.MAX_CACHE_SIZE, bot.CACHE_MAX_SIZE)
        self.assertEqual(bot.CACHE_MAX_AGE, 86400)

    def test_clean_cache_no_dir(self):
        """Verify _clean_cache_once executes cleanly when cache directory does not exist."""
        bot.CACHE_DIR = os.path.join(self.test_dir, "nonexistent")
        bot._clean_cache_once()

    def test_clean_cache_removes_expired_files(self):
        """Verify files older than CACHE_MAX_AGE are removed while newer files remain."""
        now = time.time()

        old_file = os.path.join(bot.CACHE_DIR, "old_video.mp4")
        with open(old_file, "wb") as f:
            f.write(b"old data")
        os.utime(old_file, (now - 100000, now - 100000))

        new_file = os.path.join(bot.CACHE_DIR, "new_video.mp4")
        with open(new_file, "wb") as f:
            f.write(b"new data")
        os.utime(new_file, (now - 100, now - 100))

        bot._clean_cache_once(now=now)

        self.assertFalse(os.path.exists(old_file))
        self.assertTrue(os.path.exists(new_file))

    def test_clean_cache_enforces_max_size(self):
        """Verify oldest files are pruned first when total cache size exceeds CACHE_MAX_SIZE."""
        now = time.time()
        bot.CACHE_MAX_SIZE = 500  # set small 500-byte limit

        file1 = os.path.join(bot.CACHE_DIR, "video1.mp4")
        with open(file1, "wb") as f:
            f.write(b"a" * 300)
        os.utime(file1, (now - 300, now - 300))

        file2 = os.path.join(bot.CACHE_DIR, "video2.mp4")
        with open(file2, "wb") as f:
            f.write(b"b" * 300)
        os.utime(file2, (now - 200, now - 200))

        file3 = os.path.join(bot.CACHE_DIR, "video3.mp4")
        with open(file3, "wb") as f:
            f.write(b"c" * 100)
        os.utime(file3, (now - 100, now - 100))

        # Total size = 700 bytes > 500 bytes. Oldest (file1) should be removed first.
        bot._clean_cache_once(now=now)

        self.assertFalse(os.path.exists(file1))
        self.assertTrue(os.path.exists(file2))
        self.assertTrue(os.path.exists(file3))

    def test_clean_thumbnails_removes_expired(self):
        """Verify thumbnails older than CACHE_MAX_AGE are removed."""
        now = time.time()

        old_thumb = os.path.join(bot.THUMB_CACHE_DIR, "old_thumb.jpg")
        with open(old_thumb, "wb") as f:
            f.write(b"thumbnail")
        os.utime(old_thumb, (now - 100000, now - 100000))

        new_thumb = os.path.join(bot.THUMB_CACHE_DIR, "new_thumb.jpg")
        with open(new_thumb, "wb") as f:
            f.write(b"thumbnail")
        os.utime(new_thumb, (now - 100, now - 100))

        bot._clean_cache_once(now=now)

        self.assertFalse(os.path.exists(old_thumb))
        self.assertTrue(os.path.exists(new_thumb))

    @patch.object(database, "cleanup_old_records")
    def test_clean_cache_calls_db_cleanup_and_handles_error(self, mock_db_cleanup):
        """Verify database retention cleanup is invoked and DB errors do not crash cleaner."""
        mock_db_cleanup.side_effect = Exception("DB lock error")
        bot._clean_cache_once()
        mock_db_cleanup.assert_called_once()
