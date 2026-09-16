import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot


class TestSecurity(unittest.TestCase):
    def test_is_safe_url_blocks_localhost_and_loopback(self):
        self.assertFalse(bot.is_safe_url("http://localhost/video"))
        self.assertFalse(bot.is_safe_url("http://127.0.0.1:8080/w/test"))
        self.assertFalse(bot.is_safe_url("http://[::1]/video"))

    def test_is_safe_url_blocks_private_and_cloud_metadata(self):
        self.assertFalse(bot.is_safe_url("http://10.0.0.1/video"))
        self.assertFalse(bot.is_safe_url("http://192.168.1.1/video"))
        self.assertFalse(bot.is_safe_url("http://172.16.0.1/video"))
        self.assertFalse(bot.is_safe_url("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(bot.is_safe_url("http://metadata.google.internal/computeMetadata/v1/"))

    def test_is_safe_url_blocks_local_domain_suffixes(self):
        self.assertFalse(bot.is_safe_url("http://myhost.local/w/video"))
        self.assertFalse(bot.is_safe_url("http://server.lan/w/video"))
        self.assertFalse(bot.is_safe_url("http://router.home/w/video"))
        self.assertFalse(bot.is_safe_url("http://service.internal/w/video"))
        self.assertFalse(bot.is_safe_url("http://test.localdomain/w/video"))

    def test_is_safe_url_blocks_dns_resolving_to_private_ips(self):
        with patch('socket.getaddrinfo') as mock_dns:
            mock_dns.return_value = [(2, 1, 6, '', ('127.0.0.1', 0))]
            self.assertFalse(bot.is_safe_url("https://rebinding.evil.org/w/video"))

            mock_dns.return_value = [(2, 1, 6, '', ('10.1.2.3', 0))]
            self.assertFalse(bot.is_safe_url("https://internal.evil.org/w/video"))

            mock_dns.return_value = [(2, 1, 6, '', ('93.184.216.34', 0))]
            self.assertTrue(bot.is_safe_url("https://public-video.org/video"))

    def test_peertube_regex_does_not_match_bare_local_ips(self):
        self.assertIsNone(bot.SUPPORTED_URL_RE.search("http://127.0.0.1/w/abc12345"))
        self.assertIsNone(bot.SUPPORTED_URL_RE.search("http://localhost/w/abc12345"))
        self.assertIsNone(bot.SUPPORTED_URL_RE.search("http://169.254.169.254/w/abc12345"))
        # Valid domain with TLD matches
        self.assertIsNotNone(bot.SUPPORTED_URL_RE.search("https://peertube.tv/w/abc12345"))

    def test_download_thumbnail_blocks_unsafe_urls(self):
        with patch('urllib.request.urlopen') as mock_urlopen:
            result = bot._download_thumbnail("http://127.0.0.1:8080/thumb.jpg", "test_id")
            self.assertIsNone(result)
            mock_urlopen.assert_not_called()

    def test_handle_link_info_rejects_unsafe_urls(self):
        mock_bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.id = 123
        mock_msg.chat_id = 456

        with patch('bot._send') as mock_send:
            bot._handle_link_info(mock_bot, 1, mock_msg, "http://127.0.0.1:9000/w/test")
            mock_bot.rpc.send_reaction.assert_called_with(1, 123, ["❌"])
            mock_send.assert_called_with(mock_bot, 1, 456, "❌ Cannot download internal, local, or private network targets.")


if __name__ == "__main__":
    unittest.main()
