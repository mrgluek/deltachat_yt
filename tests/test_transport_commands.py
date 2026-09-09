"""
Tests for transport and admin commands in deltachat_yt.
Verifies private chat enforcement on /addtransport and /initadmin,
resilient mode commands, and error sanitization.
"""
import os
import unittest
from unittest.mock import MagicMock, patch
import sys

# Ensure root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import bot

TEST_DB_PATH = "test_yt_cmds.db"


class TestTransportCommands(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB_PATH
        database.init_db()

        self.mock_bot = MagicMock()
        self.mock_event = MagicMock()
        self.mock_event.msg.from_id = 100
        self.mock_event.msg.chat_id = 10
        self.accid = 1

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        for f in (TEST_DB_PATH, f"{TEST_DB_PATH}-wal", f"{TEST_DB_PATH}-shm"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    @patch("bot._send")
    @patch("bot._is_dc_admin")
    def test_addtransport_requires_admin(self, mock_is_admin, mock_send):
        mock_is_admin.return_value = False
        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)
        mock_send.assert_called_once()
        self.assertIn("Only the bot administrator can use /addtransport", mock_send.call_args[0][3])

    @patch("bot._send")
    @patch("bot._is_private_chat")
    @patch("bot._is_dc_admin")
    def test_addtransport_rejected_in_group_chat(self, mock_is_admin, mock_is_private, mock_send):
        mock_is_admin.return_value = True
        mock_is_private.return_value = False
        self.mock_event.payload = "user@example.com mySecretPass"

        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)

        mock_send.assert_called_once()
        self.assertIn("only be used in a private 1:1 chat", mock_send.call_args[0][3])
        self.mock_bot.rpc.add_or_update_transport.assert_not_called()

    @patch("bot._send")
    @patch("bot._is_private_chat")
    @patch("bot._is_dc_admin")
    def test_addtransport_allowed_in_private_chat(self, mock_is_admin, mock_is_private, mock_send):
        mock_is_admin.return_value = True
        mock_is_private.return_value = True
        self.mock_event.payload = "backup@example.com mySecretPass"

        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)

        self.mock_bot.rpc.add_or_update_transport.assert_called_once_with(
            self.accid, {"addr": "backup@example.com", "password": "mySecretPass"}
        )
        mock_send.assert_called_once()
        self.assertIn("Backup transport `backup@example.com` added", mock_send.call_args[0][3])

    @patch("bot._send")
    @patch("bot._is_private_chat")
    @patch("bot._is_dc_admin")
    def test_addtransport_error_is_sanitized(self, mock_is_admin, mock_is_private, mock_send):
        mock_is_admin.return_value = True
        mock_is_private.return_value = True
        self.mock_event.payload = "backup@example.com mySecretPass"
        self.mock_bot.rpc.add_or_update_transport.side_effect = RuntimeError("Internal db timeout / secret path /var/run/secret")

        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)

        mock_send.assert_called_once()
        reply = mock_send.call_args[0][3]
        self.assertIn("Failed to add transport. Check server logs for details.", reply)
        self.assertNotIn("secret path", reply)

    @patch("bot._send")
    @patch("bot._is_private_chat")
    def test_initadmin_rejected_in_group_chat(self, mock_is_private, mock_send):
        mock_is_private.return_value = False
        bot.initadmin_command(self.mock_bot, self.accid, self.mock_event)
        mock_send.assert_called_once()
        self.assertIn("only be used in a private 1:1 chat", mock_send.call_args[0][3])

    @patch("bot._send")
    @patch("bot._is_dc_admin")
    def test_resilient_command_status_and_toggle(self, mock_is_admin, mock_send):
        mock_is_admin.return_value = True

        # Initial status (disabled)
        self.mock_event.payload = ""
        bot.resilient_command(self.mock_bot, self.accid, self.mock_event)
        self.assertIn("currently disabled", mock_send.call_args[0][3])

        # Enable resilient mode
        mock_send.reset_mock()
        self.mock_event.payload = "on"
        bot.resilient_command(self.mock_bot, self.accid, self.mock_event)
        self.assertEqual(database.get_config("resilient"), "1")
        self.assertIn("Resilient sending mode enabled", mock_send.call_args[0][3])

        # Disable resilient mode
        mock_send.reset_mock()
        self.mock_event.payload = "off"
        bot.resilient_command(self.mock_bot, self.accid, self.mock_event)
        self.assertEqual(database.get_config("resilient"), "0")
        self.assertIn("Resilient sending mode disabled", mock_send.call_args[0][3])


if __name__ == "__main__":
    unittest.main()
