import os
import sys
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

# Import database and bot
import database
import bot


class TestYTBotAudioTags(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.test_db = "test_ytbot.db"
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

    def test_version_constant(self):
        """Test that bot.VERSION constant is set."""
        self.assertTrue(hasattr(bot, "VERSION"))
        self.assertEqual(bot.VERSION, "1.6.45")

    def test_database_config_roundtrip(self):
        """Test set_config and get_config in database."""
        database.set_config("resilient", "1")
        self.assertEqual(database.get_config("resilient"), "1")
        
        database.set_config("resilient", "0")
        self.assertEqual(database.get_config("resilient"), "0")

    def test_database_downloads(self):
        """Test recording a download in the database."""
        database.add_download(123, 456, "dQw4w9WgXcQ", "Never Gonna Give You Up", 212, "audio", 1048576)
        # Verify database insertion
        conn = database.sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("SELECT video_id, title, download_type FROM downloads WHERE chat_id = ?", (123,))
        row = cursor.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "dQw4w9WgXcQ")
        self.assertEqual(row[1], "Never Gonna Give You Up")
        self.assertEqual(row[2], "audio")

    def test_tag_audio_file_mp3(self):
        """Test tagging MP3 file with ID3 tags using mutagen."""
        dummy_mp3 = "dummy_test.mp3"
        with open(dummy_mp3, "wb") as f:
            f.write(b"MP3_DATA")
        try:
            mock_mutagen = MagicMock()
            mock_id3 = MagicMock()
            mock_mutagen.id3.ID3.return_value = mock_id3

            info = {
                "title": "Tropical Trip",
                "artist": "Suduaya",
                "album": "Singles",
                "year": "2024",
                "lyrics": "Sample lyrics"
            }

            with patch.dict(sys.modules, {"mutagen": mock_mutagen, "mutagen.id3": mock_mutagen.id3}):
                bot._tag_audio_file(dummy_mp3, info, webpage_url="https://music.yandex.ru/track/153461847")
                mock_id3.save.assert_called_once()
                self.assertIn("TIT2", mock_id3.__setitem__.call_args_list[0][0][0])
                self.assertIn("TPE1", mock_id3.__setitem__.call_args_list[1][0][0])
                self.assertIn("TPE2", mock_id3.__setitem__.call_args_list[2][0][0])
                self.assertIn("TALB", mock_id3.__setitem__.call_args_list[3][0][0])
        finally:
            if os.path.exists(dummy_mp3):
                os.remove(dummy_mp3)

    @patch("asyncio.create_subprocess_exec")
    async def test_download_audio_tag_options(self, mock_subprocess):
        """Verify yt-dlp audio download command includes metadata, thumbnail, subtitles, and parse-metadata flags."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        
        async def fake_communicate():
            return (b'{"_filename": "/tmp/test.opus", "title": "Test Track"}', b'')
            
        mock_proc.communicate = fake_communicate
        mock_subprocess.return_value = mock_proc

        filepath, info, err = await bot._download_audio(
            video_id="dQw4w9WgXcQ",
            output_dir="/tmp",
            duration=180,
            use_cookies=False,
            custom_proxy=None
        )

        mock_subprocess.assert_called_once()
        cmd_args = list(mock_subprocess.call_args[0])

        self.assertIn("--embed-metadata", cmd_args)
        self.assertIn("--embed-thumbnail", cmd_args)
        self.assertIn("--write-subs", cmd_args)
        self.assertIn("--write-auto-subs", cmd_args)
        self.assertIn("--convert-subs", cmd_args)
        self.assertIn("lrc", cmd_args)
        self.assertIn("--parse-metadata", cmd_args)
        self.assertIn("%(webpage_url)s:%(meta_comment)s", cmd_args)
        self.assertIn("%(webpage_url)s:%(meta_description)s", cmd_args)


if __name__ == "__main__":
    unittest.main()
