"""
Tests for database operations in deltachat_yt.
Adheres to AGENTS.md database unit testing conventions.
"""
import os
import time
import unittest
from unittest.mock import MagicMock
import sys

# Ensure root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

TEST_DB_PATH = "test_yt_database.db"


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB_PATH
        with database._transport_stats_lock:
            database._transport_stats_buffer.clear()
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        for f in (TEST_DB_PATH, f"{TEST_DB_PATH}-wal", f"{TEST_DB_PATH}-shm"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # ── Config Tests ────────────────────────────────────────────────────────
    def test_config_set_get_roundtrip(self):
        database.set_config("test_key", "test_value")
        self.assertEqual(database.get_config("test_key"), "test_value")

    def test_config_overwrite(self):
        database.set_config("test_key", "v1")
        database.set_config("test_key", "v2")
        self.assertEqual(database.get_config("test_key"), "v2")

    def test_config_missing_key(self):
        self.assertIsNone(database.get_config("nonexistent_key_12345"))

    # ── Admin Fingerprint Tests ─────────────────────────────────────────────
    def test_admin_fingerprint_get_set(self):
        self.assertIsNone(database.get_admin_fingerprint())
        database.set_admin_fingerprint("A1B2C3D4E5F6")
        self.assertEqual(database.get_admin_fingerprint(), "A1B2C3D4E5F6")

    # ── Resilient Mode Flag Tests ───────────────────────────────────────────
    def test_resilient_flag_toggle(self):
        self.assertIsNone(database.get_config("resilient"))
        database.set_config("resilient", "1")
        self.assertEqual(database.get_config("resilient"), "1")
        database.set_config("resilient", "0")
        self.assertEqual(database.get_config("resilient"), "0")

    # ── Downloads & Stats Tests ─────────────────────────────────────────────
    def test_downloads_and_get_stats(self):
        database.add_download(10, 100, "vid1", "Title 1", 120, "video", 5000000)
        database.add_download(10, 100, "vid2", "Title 2", 180, "audio", 2000000)

        stats = database.get_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["last_24h"], 2)
        self.assertEqual(stats["by_type"].get("video"), 1)
        self.assertEqual(stats["by_type"].get("audio"), 1)
        self.assertEqual(stats["total_size"], 7000000)

        last_dl = database.get_last_download(10, "vid1", "video")
        self.assertGreater(last_dl, 0)
        self.assertEqual(database.get_last_download(10, "nonexistent", "video"), 0)

    # ── URL Map & Info Cache Tests ──────────────────────────────────────────
    def test_url_mapping_and_resolve(self):
        database.add_url_mapping("abc1234", "https://youtube.com/watch?v=12345")
        self.assertEqual(database.resolve_url("abc1234"), "https://youtube.com/watch?v=12345")
        self.assertEqual(database.resolve_url("_abc1234"), "https://youtube.com/watch?v=12345")
        self.assertIsNone(database.resolve_url("nonexistent"))

    def test_cached_info_set_and_get(self):
        self.assertIsNone(database.get_cached_info("vid99"))
        database.set_cached_info("vid99", '{"title": "cached"}', "/tmp/thumb.jpg")
        cached = database.get_cached_info("vid99")
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0], '{"title": "cached"}')
        self.assertEqual(cached[1], "/tmp/thumb.jpg")

    # ── Transport Statistics Tests (Buffered) ───────────────────────────────
    def test_transport_stats_accumulation_and_flush(self):
        addr1 = "bot1@example.com"
        addr2 = "bot2@example.com"

        # Increment sent and received in memory buffer
        database.increment_transport_sent(addr1)
        database.increment_transport_sent(addr1)
        database.increment_transport_received(addr1)

        database.increment_transport_sent(addr2)

        # Before flush, buffer has counts
        with database._transport_stats_lock:
            self.assertEqual(database._transport_stats_buffer[addr1]["sent"], 2)
            self.assertEqual(database._transport_stats_buffer[addr1]["recv"], 1)
            self.assertEqual(database._transport_stats_buffer[addr2]["sent"], 1)

        # get_all_transport_stats automatically triggers flush_transport_stats()
        stats = database.get_all_transport_stats()
        self.assertEqual(len(stats), 2)

        stats_map = {s["addr"]: s for s in stats}
        self.assertEqual(stats_map[addr1]["msgs_sent"], 2)
        self.assertEqual(stats_map[addr1]["msgs_received"], 1)
        self.assertEqual(stats_map[addr2]["msgs_sent"], 1)
        self.assertEqual(stats_map[addr2]["msgs_received"], 0)

        # Subsequent increments accumulate properly in database
        database.increment_transport_sent(addr1)
        database.flush_transport_stats()

        stats_after = database.get_all_transport_stats()
        stats_map_after = {s["addr"]: s for s in stats_after}
        self.assertEqual(stats_map_after[addr1]["msgs_sent"], 3)

    def test_transport_stats_ignores_invalid_addresses(self):
        database.increment_transport_sent("")
        database.increment_transport_sent(None)
        database.increment_transport_sent("invalid_address_without_at")
        database.flush_transport_stats()
        self.assertEqual(len(database.get_all_transport_stats()), 0)

    # ── Record Retention Cleanup Tests ──────────────────────────────────────
    def test_cleanup_old_records(self):
        now = int(time.time())
        old_time = now - (35 * 86400)  # 35 days ago
        recent_time = now - 3600       # 1 hour ago

        conn = database._connect()
        cursor = conn.cursor()
        # 1 old and 1 recent download
        cursor.execute(
            "INSERT INTO downloads (chat_id, from_id, video_id, title, duration, download_type, filesize, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1, "old_vid", "Old", 60, "audio", 100, old_time)
        )
        cursor.execute(
            "INSERT INTO downloads (chat_id, from_id, video_id, title, duration, download_type, filesize, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1, "recent_vid", "Recent", 60, "audio", 100, recent_time)
        )
        # 1 old and 1 recent url_map
        cursor.execute("INSERT INTO url_map (short_id, url, created_at) VALUES (?, ?, ?)", ("old_hash", "https://old.url", old_time))
        cursor.execute("INSERT INTO url_map (short_id, url, created_at) VALUES (?, ?, ?)", ("recent_hash", "https://recent.url", recent_time))
        # 1 old and 1 recent info_cache
        cursor.execute("INSERT INTO info_cache (video_id, info_json, thumb_path, created_at) VALUES (?, ?, ?, ?)", ("old_c", "{}", "", old_time))
        cursor.execute("INSERT INTO info_cache (video_id, info_json, thumb_path, created_at) VALUES (?, ?, ?, ?)", ("recent_c", "{}", "", recent_time))
        conn.commit()
        conn.close()

        # Run cleanup
        pruned = database.cleanup_old_records(retention_days=30)
        self.assertEqual(pruned["downloads"], 1)
        self.assertEqual(pruned["url_map"], 1)
        self.assertEqual(pruned["info_cache"], 1)

        # Verify only recent rows remain
        conn = database._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM downloads")
        self.assertEqual(cursor.fetchone()[0], 1)
        cursor.execute("SELECT COUNT(*) FROM url_map")
        self.assertEqual(cursor.fetchone()[0], 1)
        cursor.execute("SELECT COUNT(*) FROM info_cache")
        self.assertEqual(cursor.fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
