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
        """Test bot.VERSION is 1.6.23."""
        self.assertEqual(bot.VERSION, "1.6.23")

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
            url, user, pwd, token, salt, music_dir = bot._get_navidrome_config()
            self.assertEqual(url, "https://music.example.com")
            self.assertEqual(user, "admin")
            self.assertEqual(pwd, "secretpassword")
            self.assertIsNone(token)
            self.assertIsNone(salt)
            self.assertEqual(music_dir, self.temp_dir)

    def test_get_navidrome_config_token_salt(self):
        """Test reading Navidrome token and salt when password is not set."""
        with patch.dict(os.environ, {
            "NAVIDROME_URL": "https://music.example.com",
            "NAVIDROME_USER": "admin",
            "NAVIDROME_TOKEN": "mytoken123",
            "NAVIDROME_SALT": "mysalt456",
            "NAVIDROME_MUSIC_DIR": self.temp_dir
        }, clear=True):
            url, user, pwd, token, salt, music_dir = bot._get_navidrome_config()
            self.assertEqual(url, "https://music.example.com")
            self.assertEqual(user, "admin")
            self.assertIsNone(pwd)
            self.assertEqual(token, "mytoken123")
            self.assertEqual(salt, "mysalt456")
            self.assertEqual(music_dir, self.temp_dir)

    def test_save_to_navidrome(self):
        """Test copying audio file and companion .lrc into organized Artist/Album/Title structure."""
        # Create a dummy audio file and a companion .lrc file
        src_file = os.path.join(self.temp_dir, "temp_source.opus")
        with open(src_file, "wb") as f:
            f.write(b"OPUS_DUMMY_DATA")
        src_lrc = os.path.join(self.temp_dir, "temp_source.lrc")
        with open(src_lrc, "w", encoding="utf-8") as f:
            f.write("[00:10.00] Never gonna give you up")

        info = {
            "artist": "Rick Astley",
            "album": "Whenever You Need Somebody",
            "title": "Never Gonna Give You Up"
        }

        music_dir = os.path.join(self.temp_dir, "music_library")
        dest_path, dest_lrc, err = bot._save_to_navidrome(src_file, info, music_dir)

        self.assertIsNone(err)
        self.assertIsNotNone(dest_path)
        self.assertTrue(os.path.exists(dest_path))
        self.assertIsNotNone(dest_lrc)
        self.assertTrue(os.path.exists(dest_lrc))

        expected_rel = os.path.join("Rick Astley", "Whenever You Need Somebody", "Never Gonna Give You Up.opus")
        expected_lrc_rel = os.path.join("Rick Astley", "Whenever You Need Somebody", "Never Gonna Give You Up.lrc")
        self.assertTrue(dest_path.endswith(expected_rel))
        self.assertTrue(dest_lrc.endswith(expected_lrc_rel))

        with open(dest_path, "rb") as f:
            self.assertEqual(f.read(), b"OPUS_DUMMY_DATA")
        with open(dest_lrc, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "[00:10.00] Never gonna give you up")

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

        ok, msg = bot._trigger_subsonic_scan("https://music.example.com", "admin", password="secret123")
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
    def test_trigger_subsonic_scan_token_salt(self, mock_urlopen):
        """Test Subsonic scan trigger using precomputed token and salt without plaintext password."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "subsonic-response": {
                "status": "ok",
                "version": "1.16.1",
                "scanStatus": {
                    "count": 42
                }
            }
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        ok, msg = bot._trigger_subsonic_scan(
            "https://music.example.com", "admin",
            token="abcdef1234567890", salt="fixedsalt123"
        )
        self.assertTrue(ok)
        self.assertIn("42 files indexed", msg)

        req = mock_urlopen.call_args[0][0]
        self.assertIn("t=abcdef1234567890", req.full_url)
        self.assertIn("s=fixedsalt123", req.full_url)

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
        self.assertIn("Navidrome:", help_text)

    def test_check_navidrome_status_not_configured(self):
        """Test _check_navidrome_status when no Navidrome config is present."""
        with patch.dict(os.environ, {}, clear=True):
            ok, msg = bot._check_navidrome_status()
            self.assertFalse(ok)
            self.assertIn("Not configured", msg)

    @patch("urllib.request.urlopen")
    def test_check_navidrome_status_success(self, mock_urlopen):
        """Test _check_navidrome_status with successful Subsonic ping."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "subsonic-response": {
                "status": "ok",
                "version": "1.16.1",
                "type": "navidrome",
                "serverVersion": "0.54.5"
            }
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        with patch.dict(os.environ, {
            "NAVIDROME_URL": "https://music.example.com",
            "NAVIDROME_USER": "admin",
            "NAVIDROME_TOKEN": "abc",
            "NAVIDROME_SALT": "123",
            "NAVIDROME_MUSIC_DIR": self.temp_dir
        }, clear=True):
            ok, msg = bot._check_navidrome_status()
            self.assertTrue(ok)
            self.assertIn("Navidrome v0.54.5", msg)
            self.assertIn("folder OK", msg)

    @patch("bot._is_dc_admin", return_value=True)
    @patch("bot._check_navidrome_status", return_value=(True, "Navidrome v0.54.5 (folder OK: `/music`)"))
    @patch("bot._send")
    def test_stats_command_includes_navidrome_for_admin(self, mock_send, mock_check, mock_is_admin):
        """Test that /stats output includes Navidrome status for admin."""
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.id = 103
        mock_event.msg.chat_id = 202
        mock_event.msg.from_id = 303
        mock_event.msg.is_bot = False

        bot.stats_command(mock_bot, 1, mock_event)
        mock_send.assert_called_once()
        sent_reply = mock_send.call_args[0][3]
        self.assertIn("Navidrome:", sent_reply)
        self.assertIn("Navidrome v0.54.5", sent_reply)

    @patch("bot._get_navidrome_config")
    @patch("bot._trigger_subsonic_scan", return_value=(True, "Scan initiated"))
    @patch("bot._save_to_navidrome")
    @patch("bot._fetch_video_info_with_fallback")
    @patch("bot._send")
    @patch("bot._react")
    def test_do_ytms_sends_text_without_file_attachment(self, mock_react, mock_send, mock_fetch_info, mock_save, mock_scan, mock_nav_cfg):
        """Verify _do_ytms sends text confirmation only, without attaching audio file."""
        mock_nav_cfg.return_value = ("https://music.example.com", "admin", "pwd", None, None, self.temp_dir)
        mock_save.return_value = (os.path.join(self.temp_dir, "Track.opus"), None, None)

        async def fake_info(video_id):
            return {"duration": 120, "extractor": "youtube", "title": "Test Title"}, None, 0
        mock_fetch_info.side_effect = fake_info

        # Create a cached audio file
        cache_file = os.path.join(bot.CACHE_DIR, f"{bot._get_cache_id('dummy_vid_123')}.opus")
        os.makedirs(bot.CACHE_DIR, exist_ok=True)
        with open(cache_file, "wb") as f:
            f.write(b"OPUS_DATA")

        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.id = 111
        mock_msg.chat_id = 222
        mock_msg.from_id = 333

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(bot._do_ytms(mock_bot, 1, mock_msg, "dummy_vid_123"))
        finally:
            loop.close()

        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        kwargs = mock_send.call_args[1]
        self.assertEqual(args[0], mock_bot)
        self.assertEqual(args[1], 1)
        self.assertEqual(args[2], 222)
        self.assertIn("Saved to Navidrome library!", args[3])
        # Assert file is None / not passed as keyword argument
        self.assertNotIn("file", kwargs)


if __name__ == "__main__":
    unittest.main()
