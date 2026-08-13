import unittest
import os
import sys
import tempfile
import shutil
from unittest.mock import patch, MagicMock

# Mock deltabot_cli and deltachat2
class MockBotCli:
    def __init__(self, *args, **kwargs):
        pass
    def on(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator
    def on_init(self, func):
        return func
    def on_start(self, func):
        return func

sys.modules['deltabot_cli'] = MagicMock()
sys.modules['deltabot_cli'].BotCli = MockBotCli
sys.modules['deltachat2'] = MagicMock()
sys.modules['deltachat2.events'] = MagicMock()

import database
import bot


class TestLyricsAndSubtitles(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="lyrics_test_")
        self.test_db = os.path.join(self.temp_dir, "test_lyrics.db")
        database.DB_PATH = self.test_db
        database.init_db()
        bot.CACHE_DIR = os.path.join(self.temp_dir, "cache")
        os.makedirs(bot.CACHE_DIR, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_extract_clean_lyrics_from_vtt(self):
        """Test parsing and cleaning lyrics from WebVTT subtitles."""
        vtt_sample = """WEBVTT
Kind: captions
Language: en

00:00:01.360 --> 00:00:03.040
[♪♪♪]

00:00:18.640 --> 00:00:21.880
♪ We're no strangers to love ♪

00:00:22.640 --> 00:00:26.960 align:start position:0%
<c>♪ You know</c><c.colorE5E5E5> the rules</c>
<c>and so do I ♪</c>
"""
        lyrics = bot._extract_clean_lyrics(vtt_sample)
        self.assertIn("♪ We're no strangers to love ♪", lyrics)
        self.assertIn("♪ You know the rules", lyrics)
        self.assertIn("and so do I ♪", lyrics)
        self.assertNotIn("WEBVTT", lyrics)
        self.assertNotIn("00:00:18.640", lyrics)
        self.assertNotIn("<c>", lyrics)

    def test_extract_clean_lyrics_from_lrc(self):
        """Test extracting plain text lyrics from synchronized LRC."""
        lrc_sample = """[ti:Never Gonna Give You Up]
[ar:Rick Astley]
[00:18.64]We're no strangers to love
[00:22.64]You know the rules and so do I
[00:27.04]A full commitment's what I'm thinking of
"""
        lyrics = bot._extract_clean_lyrics(lrc_sample)
        self.assertIn("We're no strangers to love", lyrics)
        self.assertIn("You know the rules and so do I", lyrics)
        self.assertNotIn("[00:18.64]", lyrics)

    def test_convert_subtitles_to_lrc_vtt(self):
        """Test converting VTT cues to synchronized LRC format."""
        vtt_sample = """WEBVTT

00:01:18.640 --> 00:01:21.880
We're no strangers to love

00:01:22.500 --> 00:01:26.960
You know the rules
and so do I
"""
        lrc = bot._convert_subtitles_to_lrc(vtt_sample)
        self.assertIn("[01:18.64] We're no strangers to love", lrc)
        self.assertIn("[01:22.50] You know the rules and so do I", lrc)

    def test_convert_subtitles_to_lrc_passthrough(self):
        """Test that already-valid LRC is normalized."""
        lrc_sample = """[00:15.20] Hello World
[00:20.50] Second Line
"""
        result = bot._convert_subtitles_to_lrc(lrc_sample)
        self.assertEqual(result, "[00:15.20] Hello World\n[00:20.50] Second Line")

    def test_embed_lyrics_in_audio_mutagen(self):
        """Test _embed_lyrics_in_audio calls appropriate mutagen tags."""
        dummy_opus = os.path.join(self.temp_dir, "test.opus")
        with open(dummy_opus, "wb") as f:
            f.write(b"dummy_data")

        mock_mutagen = MagicMock()
        mock_ogg = MagicMock()
        mock_mutagen.oggopus.OggOpus.return_value = mock_ogg

        with patch.dict(sys.modules, {"mutagen": mock_mutagen, "mutagen.oggopus": mock_mutagen.oggopus}):
            bot._embed_lyrics_in_audio(dummy_opus, "Test Lyrics Line 1\nTest Lyrics Line 2")
            mock_ogg.__setitem__.assert_any_call("lyrics", "Test Lyrics Line 1\nTest Lyrics Line 2")
            mock_ogg.__setitem__.assert_any_call("unsyncedlyrics", "Test Lyrics Line 1\nTest Lyrics Line 2")
            mock_ogg.save.assert_called_once()

    def test_process_subtitles_and_lyrics(self):
        """Test discovery of subtitle files in directory, LRC generation and embedding."""
        output_dir = os.path.join(self.temp_dir, "dl_out")
        os.makedirs(output_dir, exist_ok=True)
        safe_id = "test_vid_xyz"

        # Create audio and vtt file
        audio_file = os.path.join(output_dir, f"{safe_id}.opus")
        with open(audio_file, "wb") as f:
            f.write(b"OPUS_DATA")

        vtt_file = os.path.join(output_dir, f"{safe_id}.en.vtt")
        with open(vtt_file, "w", encoding="utf-8") as f:
            f.write("""WEBVTT

00:00:10.000 --> 00:00:15.000
Line One of Lyrics
""")

        with patch("bot._embed_lyrics_in_audio") as mock_embed:
            lrc_path, clean_lyrics = bot._process_subtitles_and_lyrics(output_dir, safe_id, audio_file)
            self.assertIsNotNone(lrc_path)
            self.assertTrue(os.path.exists(lrc_path))
            self.assertTrue(lrc_path.endswith(f"{safe_id}.lrc"))
            self.assertIn("Line One of Lyrics", clean_lyrics)
            mock_embed.assert_called_once_with(audio_file, "Line One of Lyrics")

            with open(lrc_path, "r", encoding="utf-8") as f:
                content = f.read()
                self.assertIn("[00:10.00] Line One of Lyrics", content)


if __name__ == "__main__":
    unittest.main()
