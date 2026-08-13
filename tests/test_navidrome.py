import io
import json
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


class TestNavidromeIntegration(unittest.TestCase):

    def setUp(self):
        self.test_db = "test_ytbot_navidrome.db"
        database.DB_PATH = self.test_db
        database.init_db()
        self.temp_dir = tempfile.mkdtemp(prefix="nav_test_")

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
        """Test bot.VERSION is 1.6.21."""
        self.assertEqual(bot.VERSION, "1.6.21")

    def test_sanitize_filename(self):
        """Test filename sanitization for various edge cases and illegal characters."""
        self.assertEqual(bot._sanitize_filename("Valid Name"), "Valid Name")
        self.assertEqual(bot._sanitize_filename("AC/DC: Back In Black?"), "AC_DC_ Back In Black_")
        self.assertEqual(bot._sanitize_filename("...Leading and trailing dots..."), "Leading and trailing dots")
        self.assertEqual(bot._sanitize_filename("   "), "Unknown")
        self.assertEqual(bot._sanitize_filename(None), "Unknown")
        self.assertEqual(bot._sanitize_filename("A" * 150, max_length=50), "A" * 50)

    def test_get_navidrome_config_defaults(self):
        """Test reading Navidrome configuration from environment and database."""
        with patch.dict(os.environ, {
            "NAVIDROME_URL": "https://music.example.com",
            "NAVIDROME_USER": "admin",
            "NAVIDROME_PASSWORD": "secretpassword",
            "NAVIDROME_MUSIC_DIR": self.temp_dir
        }, clear=True):
            url, user, pwd, music_dir = bot._get_navidrome_config()
            self.assertEqual(url, "https://music.example.com")
            self.assertEqual(user, "admin")
            self.assertEqual(pwd, "secretpassword")
            self.assertEqual(music_dir, self.temp_dir)

    def test_save_to_navidrome(self):
        """Test copying audio file into organized Artist/Album/Title.opus structure."""
        # Create a dummy audio file
        src_file = os.path.join(self.temp_dir, "temp_source.opus")
        with open(src_file, "wb") as f:
            f.write(b"OPUS_DUMMY_DATA")

        info = {
            "artist": "Rick Astley",
            "album": "Whenever You Need Somebody",
            "title": "Never Gonna Give You Up"
        }

        music_dir = os.path.join(self.temp_dir, "music_library")
        dest_path, err = bot._save_to_navidrome(src_file, info, music_dir)

        self.assertIsNone(err)
        self.assertIsNotNone(dest_path)
        self.assertTrue(os.path.exists(dest_path))

        expected_rel = os.path.join("Rick Astley", "Whenever You Need Somebody", "Never Gonna Give You Up.opus")
        self.assertTrue(dest_path.endswith(expected_rel))

        with open(dest_path, "rb") as f:
            self.assertEqual(f.read(), b"OPUS_DUMMY_DATA")

    @patch("urllib.request.urlopen")
    def test_trigger_subsonic_scan_success(self, mock_urlopen):
        """Test successful Subsonic startScan.view trigger."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "subsonic-response": {
                "status": "ok",
                "version": "1.16.1",
                "scanStatus": {
                    "scanning": True,
                    "count": 128
                }
            }
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        ok, msg = bot._trigger_subsonic_scan("https://music.example.com", "admin", "secret123")
        self.assertTrue(ok)
        self.assertIn("128 files indexed", msg)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertTrue(req.full_url.startswith("https://music.example.com/rest/startScan.view?"))
        self.assertIn("u=admin", req.full_url)
        self.assertIn("v=1.16.1", req.full_url)
        self.assertIn("f=json", req.full_url)
        self.assertIn("c=DeltaChatYTBot", req.full_url)

    @patch("urllib.request.urlopen")
    def test_trigger_subsonic_scan_error(self, mock_urlopen):
        """Test handling Subsonic API error response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "subsonic-response": {
                "status": "failed",
                "error": {
                    "code": 40,
                    "message": "Wrong username or password"
                }
            }
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        ok, msg = bot._trigger_subsonic_scan("https://music.example.com", "admin", "wrongpassword")
        self.assertFalse(ok)
        self.assertIn("Subsonic error: Wrong username or password", msg)

    @patch("urllib.request.urlopen", side_effect=Exception("Connection refused"))
    def test_trigger_subsonic_scan_connection_failure(self, mock_urlopen):
        """Test handling connection failure when contacting Navidrome server."""
        ok, msg = bot._trigger_subsonic_scan("http://localhost:4533", "admin", "secret")
        self.assertFalse(ok)
        self.assertIn("Connection failed", msg)

    @patch("bot._is_dc_admin", return_value=False)
    @patch("bot._send")
    def test_ytms_command_non_admin_rejected(self, mock_send, mock_is_admin):
        """Test that non-admin users cannot execute /ytms."""
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.id = 101
        mock_event.msg.chat_id = 202
        mock_event.msg.from_id = 303
        mock_event.msg.text = "/ytms dQw4w9WgXcQ"
        mock_event.msg.is_bot = False
        bot.dc_accid = 1

        bot.ytms_command(mock_bot, 1, mock_event)

        mock_send.assert_called_once_with(mock_bot, 1, 202, "❌ Only the bot administrator can use /ytms.")

    @patch("bot._is_dc_admin", return_value=True)
    @patch("threading.Thread")
    def test_ytms_command_admin_success(self, mock_thread, mock_is_admin):
        """Test that admin can trigger /ytms and it dispatches background thread."""
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.id = 102
        mock_event.msg.chat_id = 202
        mock_event.msg.from_id = 303
        mock_event.msg.text = "/ytms dQw4w9WgXcQ"
        mock_event.msg.is_bot = False
        bot.dc_accid = 1

        bot.ytms_command(mock_bot, 1, mock_event)

        mock_thread.assert_called_once()
        args = mock_thread.call_args[1]["args"]
        self.assertEqual(args[0], mock_bot)
        self.assertEqual(args[1], 1)
        self.assertEqual(args[3], "dQw4w9WgXcQ")

    @patch("bot._is_dc_admin", return_value=True)
    def test_help_text_includes_ytms_for_admin(self, mock_is_admin):
        """Test that /help text displays /ytms command when viewed by admin."""
        database.set_config("admin_dc_email", "admin@example.com")
        mock_bot = MagicMock()
        mock_bot.rpc.get_contact.return_value.address = "admin@example.com"

        help_text = bot._get_help_text(mock_bot, 1, 123)
        self.assertIn("/ytms <url>", help_text)
        self.assertIn("/ytms_<video_id>", help_text)


if __name__ == "__main__":
    unittest.main()
