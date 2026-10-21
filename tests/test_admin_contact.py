import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock packages if not installed (CI without the full Delta Chat stack)
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

import bot

FP = "DFF2CAB1FEB7182F997C0A01466AA64DE33D8A39"


class TestAdminKeyContact(unittest.TestCase):
    """Admin warnings must go to the admin's key contact: the address-only one cannot be written to."""

    def setUp(self):
        self.cfg = {"admin_dc_email": "admin@example.org"}
        self.fp = FP
        self.patches = [
            patch("database.get_config", side_effect=lambda k, *a: self.cfg.get(k)),
            patch("database.get_admin_fingerprint", side_effect=lambda: self.fp),
            patch("bot._get_contact_fingerprint", side_effect=lambda b, a, cid, contact=None: {11: "AAAA" * 10, 12: FP}.get(cid)),
        ]
        for p in self.patches:
            p.start()
        contacts = {
            10: {"address": "admin@example.org", "is_key_contact": False, "last_seen": 0},
            11: {"address": "admin@example.org", "is_key_contact": True, "last_seen": 900},
            12: {"address": "admin@example.org", "is_key_contact": True, "last_seen": 100},
        }
        self.dc = MagicMock()
        self.dc.rpc.get_contact_ids.return_value = [10, 11, 12]
        self.dc.rpc.get_contact.side_effect = lambda acc, cid: contacts[cid]
        self.dc.rpc.lookup_contact_id_by_addr.return_value = 10

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_contact_with_admin_fingerprint(self):
        self.assertEqual(bot.admin_key_contact_id(self.dc, 1), 12)

    def test_without_fingerprint_latest_key_contact(self):
        self.fp = ""
        self.assertEqual(bot.admin_key_contact_id(self.dc, 1), 11)

    def test_no_key_contact_falls_back_to_lookup(self):
        self.dc.rpc.get_contact_ids.return_value = []
        self.assertEqual(bot.admin_key_contact_id(self.dc, 1), 10)

    def test_no_admin(self):
        self.cfg = {}
        self.assertIsNone(bot.admin_key_contact_id(self.dc, 1))


if __name__ == "__main__":
    unittest.main()
