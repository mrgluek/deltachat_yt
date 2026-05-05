import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("DB_PATH", "ytbot.db")
_lock = threading.Lock()

def init_db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Config table for admin_dc_email, admin_dc_fingerprint, etc.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Downloads history
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                from_id INTEGER,
                video_id TEXT,
                title TEXT,
                duration INTEGER,
                download_type TEXT,
                filesize INTEGER,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_downloads_chat_id ON downloads(chat_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_downloads_created_at ON downloads(created_at)')
        
        # Transport statistics: track messages sent per relay address
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transport_stats (
                addr TEXT PRIMARY KEY,
                msgs_sent INTEGER DEFAULT 0,
                msgs_received INTEGER DEFAULT 0,
                last_sent_at INTEGER,
                last_received_at INTEGER
            )
        ''')
        
        # URL shortener for full links (to keep commands clickable)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_map (
                short_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        ''')
        # Metadata cache for faster link info responses
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS info_cache (
                video_id TEXT PRIMARY KEY,
                info_json TEXT,
                thumb_path TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        ''')
        
        conn.commit()
        conn.close()

def set_config(key: str, value: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()

def get_config(key: str) -> str:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def add_download(chat_id: int, from_id: int, video_id: str, title: str, duration: int, download_type: str, filesize: int):
    """Record a download in the history."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO downloads (chat_id, from_id, video_id, title, duration, download_type, filesize) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, from_id, video_id, title, duration, download_type, filesize)
        )
        # Keep only last 30 days
        cursor.execute('''
            DELETE FROM downloads 
            WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - 2592000
        ''')
        conn.commit()
        conn.close()

def get_last_download(chat_id: int, video_id: str, download_type: str) -> int:
    """Get the timestamp of the last time this video was sent to this chat."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT created_at FROM downloads WHERE chat_id = ? AND video_id = ? AND download_type = ? ORDER BY created_at DESC LIMIT 1",
            (chat_id, video_id, download_type)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0

def get_stats() -> dict:
    """Get download statistics."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Total downloads
        cursor.execute("SELECT COUNT(*) FROM downloads")
        total = cursor.fetchone()[0]
        
        # Last 24h
        cursor.execute("SELECT COUNT(*) FROM downloads WHERE created_at >= CAST(strftime('%s','now') AS INTEGER) - 86400")
        last_24h = cursor.fetchone()[0]
        
        # By type
        cursor.execute("SELECT download_type, COUNT(*) FROM downloads GROUP BY download_type")
        by_type = dict(cursor.fetchall())
        
        # Total size
        cursor.execute("SELECT COALESCE(SUM(filesize), 0) FROM downloads")
        total_size = cursor.fetchone()[0]
        
        conn.close()
        return {
            "total": total,
            "last_24h": last_24h,
            "by_type": by_type,
            "total_size": total_size
        }

def increment_transport_sent(addr: str):
    """Increment the sent counter for a transport address."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_sent_at)
            VALUES (?, 1, 0, CAST(strftime('%s','now') AS INTEGER))
            ON CONFLICT(addr) DO UPDATE SET
                msgs_sent = msgs_sent + 1,
                last_sent_at = CAST(strftime('%s','now') AS INTEGER)
        ''', (addr,))
        conn.commit()
        conn.close()

def increment_transport_received(addr: str):
    """Increment the received counter for a transport address."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_received_at)
            VALUES (?, 0, 1, CAST(strftime('%s','now') AS INTEGER))
            ON CONFLICT(addr) DO UPDATE SET
                msgs_received = msgs_received + 1,
                last_received_at = CAST(strftime('%s','now') AS INTEGER)
        ''', (addr,))
        conn.commit()
        conn.close()

def get_all_transport_stats() -> list[dict]:
    """Get statistics for all tracked transports."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transport_stats ORDER BY msgs_sent + msgs_received DESC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def get_admin_fingerprint():
    """Get the saved admin DC fingerprint."""
    return get_config("admin_dc_fingerprint")

def set_admin_fingerprint(fp):
    """Set the admin DC fingerprint."""
    set_config("admin_dc_fingerprint", fp)

def add_url_mapping(short_id: str, url: str):
    """Store a mapping between a short ID (hash) and a full URL."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO url_map (short_id, url) VALUES (?, ?)", (short_id, url))
        # Cleanup old mappings (older than 7 days) to keep DB small
        cursor.execute("DELETE FROM url_map WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - 604800")
        conn.commit()
        conn.close()

def resolve_url(short_id: str) -> str:
    """Resolve a short ID back to the original full URL."""
    # Strip leading underscore if present (from command payload)
    if short_id.startswith("_"):
        short_id = short_id[1:]
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM url_map WHERE short_id = ?", (short_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_cached_info(video_id: str) -> tuple[str, str] | None:
    """Get cached metadata JSON and thumbnail path."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Only return if not older than 24 hours
        cursor.execute(
            "SELECT info_json, thumb_path FROM info_cache WHERE video_id = ? AND created_at >= CAST(strftime('%s','now') AS INTEGER) - 86400",
            (video_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return (row[0], row[1]) if row else None

def set_cached_info(video_id: str, info_json: str, thumb_path: str):
    """Store video metadata and thumbnail path in cache."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO info_cache (video_id, info_json, thumb_path) VALUES (?, ?, ?)",
            (video_id, info_json, thumb_path)
        )
        # Cleanup old cache entries (older than 24h)
        cursor.execute("DELETE FROM info_cache WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - 86400")
        conn.commit()
        conn.close()

init_db()
