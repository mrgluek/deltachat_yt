import asyncio
import os
import sys
import shutil
import tempfile
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


# The 1:31:06 audiobook from the bug report: too long for one audio file
VID = "I6OwrKGEUno"
TOTAL = 5466
INFO = {"title": "Malysh i Karlson", "uploader": "Melodiya", "duration": TOTAL, "extractor": "youtube"}


class TestAudioPartsHelpers(unittest.TestCase):

    def test_short_audio_is_not_split(self):
        self.assertEqual(bot._audio_parts(bot.MAX_DURATION_AUDIO), [])
        self.assertEqual(bot._audio_parts(0), [])

    def test_long_audio_uses_ten_minute_grid(self):
        parts = bot._audio_parts(TOTAL)
        self.assertEqual(len(parts), 10)
        self.assertEqual(parts[0], (0, 600))
        self.assertEqual(parts[-1], (5400, 5466))

    def test_short_tail_is_merged(self):
        parts = bot._audio_parts(4230)  # 7 x 600 + 30 s
        self.assertEqual(parts[-1], (3600, 4230))
        self.assertEqual(len(parts), 7)

    def test_find_audio_part(self):
        self.assertEqual(bot._find_audio_part(TOTAL, 600, 1200)[0], 1)
        self.assertIsNone(bot._find_audio_part(TOTAL, 600, 900)[0])
        self.assertIsNone(bot._find_audio_part(1800, 0, 600)[0])

    def test_part_names(self):
        title, album, artist = bot._audio_part_names(INFO, 2, 10)
        self.assertEqual(title, "Malysh i Karlson (Part 03/10)")
        self.assertEqual(album, "Malysh i Karlson")
        self.assertEqual(artist, "Melodiya")


class TestAudioPartsFlows(unittest.TestCase):

    def setUp(self):
        self.test_db = "test_audio_parts_ytbot.db"
        database.DB_PATH = self.test_db
        database.init_db()
        self.tmp = tempfile.mkdtemp(prefix="audio_parts_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for path in [self.test_db, f"{self.test_db}-wal", f"{self.test_db}-shm"]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _msg(self):
        msg = MagicMock()
        msg.id, msg.chat_id, msg.from_id = 111, 222, 333
        return msg

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=True)
    def test_menu_offers_audio_part_one(self, mock_is_admin, mock_send):
        bot._display_link_info(MagicMock(), 1, self._msg(), VID, dict(INFO), None)
        text = mock_send.call_args[0][3]
        self.assertNotIn("Too long", text)
        self.assertIn(f"💿 Part 1/10 (00:00-10:00, Opus, ~9.4 MB) /ytm_{VID}_0_600", text)
        self.assertIn(f"💾 /ytms_{VID}_0_600", text)

    @patch('bot._send')
    @patch('bot._react')
    @patch('bot._is_dc_admin', return_value=True)
    def test_audio_caption_names_part_and_links_next(self, mock_is_admin, mock_react, mock_send):
        fake = os.path.join(self.tmp, "part.opus")
        with open(fake, "wb") as f:
            f.write(b"OPUS")
        asyncio.run(bot._send_from_cache(
            MagicMock(), 1, self._msg(), f"https://youtu.be/{VID}?start=600&end=1200",
            "audio", fake, info=dict(INFO)))
        caption = mock_send.call_args[0][3]
        self.assertIn("Part 2/10 [10:00-20:00]", caption)
        self.assertIn(f"Next part 3/10 (20:00-30:00): /ytm_{VID}_1200_1800", caption)
        self.assertIn(f"Save to Navidrome: /ytms_{VID}_1200_1800", caption)

    @patch('bot._tag_audio_file')
    def test_navidrome_parts_do_not_overwrite(self, mock_tag):
        src = os.path.join(self.tmp, "src.opus")
        with open(src, "wb") as f:
            f.write(b"OPUS")
        music = os.path.join(self.tmp, "music")
        # Info from a section download may lack the full duration: the caller passes it
        info = {"title": "Malysh i Karlson", "uploader": "Melodiya"}
        p1, _, err1 = bot._save_to_navidrome(src, info, music, video_id=f"{VID}_0_600", total_duration=TOTAL)
        p2, _, err2 = bot._save_to_navidrome(src, info, music, video_id=f"{VID}_600_1200", total_duration=TOTAL)
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertTrue(p1.endswith(os.path.join("Melodiya", "Malysh i Karlson", "01 - Malysh i Karlson.opus")), p1)
        self.assertTrue(p2.endswith(os.path.join("Melodiya", "Malysh i Karlson", "02 - Malysh i Karlson.opus")), p2)
        tag_info = mock_tag.call_args_list[1][0][1]
        self.assertEqual(tag_info["title"], "Malysh i Karlson (Part 02/10)")
        self.assertEqual(tag_info["track_number"], 2)
        self.assertEqual(tag_info["total_tracks"], 10)
        self.assertEqual(tag_info["album"], "Malysh i Karlson")

    @patch("bot._get_navidrome_config")
    @patch("bot._trigger_subsonic_scan", return_value=(True, "Scan initiated"))
    @patch("bot._save_to_navidrome")
    @patch("bot._download_audio")
    @patch("bot._find_cached_file", return_value=None)
    @patch("bot._fetch_video_info_with_fallback")
    @patch("bot._check_disk_space", return_value=True)
    @patch("bot._send")
    @patch("bot._react")
    def test_ytms_long_video_saves_part_one(self, mock_react, mock_send, mock_disk, mock_fetch, mock_cached,
                                            mock_download, mock_save, mock_scan, mock_nav_cfg):
        mock_nav_cfg.return_value = ("https://music.example.com", "admin", "pwd", None, None, self.tmp)

        async def fake_info(video_id):
            return dict(INFO), None, 0
        mock_fetch.side_effect = fake_info

        async def fake_download(video_id, output_dir, duration, **kwargs):
            path = os.path.join(output_dir, "dl.opus")
            with open(path, "wb") as f:
                f.write(b"OPUS")
            return path, dict(INFO), None
        mock_download.side_effect = fake_download
        mock_save.return_value = (os.path.join(self.tmp, "01 - x.opus"), None, None)

        asyncio.run(bot._do_ytms(MagicMock(), 1, self._msg(), VID))

        called_id = mock_download.call_args[0][0]
        self.assertIn("start=0&end=600", called_id)
        self.assertEqual(mock_download.call_args[1]["start_time"], 0)
        self.assertEqual(mock_download.call_args[1]["end_time"], 600)
        self.assertEqual(mock_save.call_args[1]["total_duration"], TOTAL)
        reply = mock_send.call_args[0][3]
        self.assertIn("Saved to Navidrome library!", reply)
        self.assertIn("(Part 01/10)", reply)
        self.assertIn(f"Next part 2/10 (10:00-20:00): /ytms_{VID}_600_1200", reply)


if __name__ == "__main__":
    unittest.main()
