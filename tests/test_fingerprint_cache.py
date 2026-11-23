"""1.12.2: the own fingerprint is read once per account, not on every message or command."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import deltachat2  # noqa: F401
except ImportError:
    sys.modules['deltachat2'] = MagicMock()
try:
    import deltabot_cli  # noqa: F401
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
    mock_cli = MagicMock()
    mock_cli.BotCli = MockBotCli
    sys.modules['deltabot_cli'] = mock_cli

import database
import dc_fingerprint

tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db.close()
database.DB_PATH = tmp_db.name

import bot

SELF_FP = "D58B5E6C55482F94F9F5B84B6FB9FE3AD2658082"
PEER_FP = "0ECC037BAD768C41CA99B1F5F0B80E7D5DB13122"
ADMIN_FP = "DEC0E8D11FC4686F2C715C56F8C9AD699BC19AD8"
INFO = (f"Messages are end-to-end encrypted.\nFingerprints:\n\nMe:\n{SELF_FP}\n\n"
        f"Bob:\n{PEER_FP}\n\nRelays:\nbob@example.net")


def make_bot():
    b = MagicMock()
    b.rpc.get_chat_securejoin_qr_code.return_value = f"https://i.delta.chat/#{SELF_FP}&v=3&i=a&s=b"
    b.rpc.get_contact.return_value = SimpleNamespace(
        address="bob@example.net", auth_name="Bob", name="", is_key_contact=True)
    b.rpc.get_contact_encryption_info.return_value = INFO
    return b


class TestOwnFingerprintCache(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = tmp_db.name
        database.init_db()
        dc_fingerprint.forget_self_fingerprint()
        database.set_config("admin_dc_email", "admin@example.net")
        database.set_admin_fingerprint(ADMIN_FP)
        bot._user_rate_limits.clear()

    def test_helper_asks_the_core_once(self):
        rpc = make_bot().rpc
        for _ in range(1000):
            self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        self.assertEqual(rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_failures_are_not_cached_and_accounts_are_separate(self):
        rpc = MagicMock()
        rpc.get_chat_securejoin_qr_code.side_effect = [Exception("down"), f"https://i.delta.chat/#{SELF_FP}&v=3"]
        self.assertIsNone(dc_fingerprint.get_self_fingerprint(rpc, 1))
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        rpc.get_chat_securejoin_qr_code.side_effect = None
        rpc.get_chat_securejoin_qr_code.return_value = f"https://i.delta.chat/#{PEER_FP}&v=3"
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 2), PEER_FP)
        dc_fingerprint.forget_self_fingerprint(rpc, 1)
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), PEER_FP)

    def test_1000_link_messages_through_the_rate_limit_cost_at_most_one_qr_call(self):
        b = make_bot()
        for i in range(1000):
            bot._is_rate_limited(b, 1, 10 + (i % 50))
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_1000_admin_commands_cost_at_most_one_qr_call(self):
        b = make_bot()
        msg = SimpleNamespace(chat_id=42, from_id=10, text="/ytms x", id=5)
        event = SimpleNamespace(msg=msg)
        for _ in range(1000):
            bot.ytms_command(b, 1, event)
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_admin_still_recognised_by_fingerprint(self):
        b = make_bot()
        b.rpc.get_contact_encryption_info.return_value = INFO.replace(PEER_FP, ADMIN_FP)
        for _ in range(20):
            self.assertTrue(bot._is_dc_admin(b, 1, 10))
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)


if __name__ == "__main__":
    unittest.main()
