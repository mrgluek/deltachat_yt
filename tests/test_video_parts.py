import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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


# Chapters of the video that hit the 50 MB limit: Track 2 is 13:27 long
CHAPTERS = [
    {"start_time": 0.0, "end_time": 697.0, "title": "This Is Britain"},
    {"start_time": 697.0, "end_time": 1504.0, "title": "Life in Britain"},
    {"start_time": 1504.0, "end_time": 1800.0, "title": "Outro"},
]
INFO = {"title": "Britain", "duration": 1800, "chapters": CHAPTERS}


class TestSplitRange(unittest.TestCase):

    def test_short_range_not_split(self):
        self.assertEqual(bot._split_range(0, 600), [(0, 600)])
        self.assertEqual(bot._split_range(1504, 1800), [(1504, 1800)])

    def test_equal_halves(self):
        self.assertEqual(bot._split_range(697, 1504), [(697, 1101), (1101, 1504)])

    def test_three_parts_contiguous_and_bounded(self):
        parts = bot._split_range(100, 1900)
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0][0], 100)
        self.assertEqual(parts[-1][1], 1900)
        for (_, e), (s, _) in zip(parts, parts[1:]):
            self.assertEqual(e, s)
        for s, e in parts:
            self.assertLessEqual(e - s, bot.VIDEO_PART_MAX_SECONDS)

    def test_next_section_is_next_part_then_next_track(self):
        chapters = bot._get_video_chapters(dict(INFO))
        self.assertEqual(bot._next_video_section(chapters, 1, 697, 1101),
                         ("part", "Life in Britain, part 2/2", 1101, 1504))
        self.assertEqual(bot._next_video_section(chapters, 1, 1101, 1504),
                         ("track", "Outro", 1504, 1800))
        # Track 1 (11:37) is split too, so its last part leads to part 1 of Track 2
        self.assertEqual(bot._next_video_section(chapters, 0, 348, 697),
                         ("track", "Life in Britain, part 1/2", 697, 1101))
        self.assertIsNone(bot._next_video_section(chapters, 2, 1504, 1800))

    def test_whole_chapter_range_is_not_a_part(self):
        # An older link for the whole chapter must lead to the next track, not part 2
        chapters = bot._get_video_chapters(dict(INFO))
        self.assertEqual(bot._next_video_section(chapters, 1, 697, 1504),
                         ("track", "Outro", 1504, 1800))


class TestVideoPartsMessages(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.test_db = "test_video_parts_ytbot.db"
        database.DB_PATH = self.test_db
        database.init_db()

    def tearDown(self):
        for path in [self.test_db, f"{self.test_db}-wal", f"{self.test_db}-shm"]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=False)
    def test_menu_offers_first_part_of_long_track(self, mock_is_admin, mock_send):
        bot._display_link_info(MagicMock(), 1, MagicMock(), "AT4HI1s1t6c", dict(INFO), None)
        text = mock_send.call_args[0][3]
        self.assertIn("📼 Track 1 part 1/2 (00:00-05:48) /yt_AT4HI1s1t6c_0_348", text)
        # Audio stays whole
        self.assertIn("/ytm_AT4HI1s1t6c_0_697", text)

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=False)
    def test_menu_for_specific_track_offers_its_first_part(self, mock_is_admin, mock_send):
        bot._display_link_info(MagicMock(), 1, MagicMock(), "https://youtu.be/AT4HI1s1t6c?t=697", dict(INFO), None)
        text = mock_send.call_args[0][3]
        self.assertIn("Track 2 part 1/2 (11:37-18:21) /yt_AT4HI1s1t6c_697_1101", text)
        self.assertIn("/ytm_AT4HI1s1t6c_697_1504", text)

    @patch('bot._send')
    @patch('bot._react')
    @patch('bot._is_dc_admin', return_value=False)
    async def test_caption_names_part_and_links_next_part(self, mock_is_admin, mock_react, mock_send):
        tmpdir = tempfile.mkdtemp(prefix="parts_test_")
        try:
            fake_video = os.path.join(tmpdir, "test.mp4")
            with open(fake_video, "wb") as f:
                f.write(b"VIDEO_BYTES")
            msg = MagicMock()
            msg.chat_id, msg.from_id, msg.id = 101, 202, 555
            await bot._send_from_cache(
                MagicMock(), 1, msg,
                "https://youtu.be/AT4HI1s1t6c?start=697&end=1101",
                "video", fake_video, info=dict(INFO)
            )
            caption = mock_send.call_args[0][3]
            self.assertIn("Track 2. Life in Britain, part 1/2 [11:37-18:21]", caption)
            self.assertIn("Next part: Life in Britain, part 2/2 (18:21-25:04): /yt_AT4HI1s1t6c_1101_1504", caption)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestRangeLinkMenu(unittest.TestCase):

    def setUp(self):
        self.test_db = "test_range_menu_ytbot.db"
        database.DB_PATH = self.test_db
        database.init_db()

    def tearDown(self):
        for path in [self.test_db, f"{self.test_db}-wal", f"{self.test_db}-shm"]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _menu(self, url, duration, mock_send):
        bot._display_link_info(MagicMock(), 1, MagicMock(), url, {"title": "Clip", "duration": duration}, None)
        return mock_send.call_args[0][3]

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=True)
    def test_short_video_keeps_requested_range(self, mock_is_admin, mock_send):
        # ?t=34-41 on a 3:25 video used to offer /yt_ID for the whole video
        text = self._menu("https://youtu.be/aUIXqj47fLg?t=34-41", 205, mock_send)
        self.assertIn("(00:34-00:41, 00:07)", text)
        self.assertIn("/yt_aUIXqj47fLg_34_41", text)
        self.assertIn("/ytm_aUIXqj47fLg_34_41", text)
        self.assertIn("/ytms_aUIXqj47fLg_34_41", text)
        self.assertNotIn("/yt_aUIXqj47fLg ", text)

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=False)
    def test_long_video_respects_end_time(self, mock_is_admin, mock_send):
        # Range shorter than 10 min inside a long video: the end time is no longer replaced by start+600
        text = self._menu("https://youtu.be/aUIXqj47fLg?t=100-200", 3600, mock_send)
        self.assertIn("/yt_aUIXqj47fLg_100_200", text)
        self.assertIn("/ytm_aUIXqj47fLg_100_200", text)

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=False)
    def test_start_only_runs_to_end_of_video(self, mock_is_admin, mock_send):
        text = self._menu("https://youtu.be/aUIXqj47fLg?t=60", 205, mock_send)
        self.assertIn("/yt_aUIXqj47fLg_60_205", text)

    @patch('bot._send')
    @patch('bot._is_dc_admin', return_value=False)
    def test_no_range_short_video_unchanged(self, mock_is_admin, mock_send):
        text = self._menu("aUIXqj47fLg", 205, mock_send)
        self.assertIn("/yt_aUIXqj47fLg ", text)
        self.assertIn("/ytm_aUIXqj47fLg", text)


class TestSectionFormat(unittest.IsolatedAsyncioTestCase):

    async def _format_for_height(self, height):
        # Capture the yt-dlp command line instead of running it
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"some error"))
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
            await bot._download_video("AT4HI1s1t6c", tempfile.gettempdir(), max_height=height,
                                      start_time=697, end_time=1504, player_client="web")
        cmd = mock_exec.call_args_list[0][0]
        return cmd[cmd.index("-f") + 1]

    async def test_360p_prefers_progressive_18(self):
        self.assertTrue((await self._format_for_height(360)).startswith("18/"))

    async def test_lower_heights_do_not_start_with_18(self):
        # Format 18 is 360p; picking it first made the 240p/144p retries download the same file
        for h in (240, 144):
            fmt = await self._format_for_height(h)
            self.assertFalse(fmt.startswith("18"), fmt)
            self.assertTrue(fmt.startswith(f"bv[height<={h}]"), fmt)


if __name__ == "__main__":
    unittest.main()
