import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("DB_PATH", "ytbot.db")
_lock = threading.Lock()

def _connect(timeout: float = 10.0) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def init_db():
    with _lock:
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA cache_size = -4000;")
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
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_url_map_created_at ON url_map(created_at)')

            # Metadata cache for faster link info responses
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS info_cache (
                    video_id TEXT PRIMARY KEY,
                    info_json TEXT,
                    thumb_path TEXT,
                    created_at INTEGER DEFAULT (strftime('%s','now'))
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_info_cache_created_at ON info_cache(created_at)')
            
            conn.commit()
        finally:
            conn.close()

def set_config(key: str, value: str):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value) if value is not None else None))
            conn.commit()
        finally:
            conn.close()

def get_config(key: str) -> str | None:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def add_download(chat_id: int, from_id: int, video_id: str, title: str, duration: int, download_type: str, filesize: int):
    """Record a download in the history."""
    with _lock:
        conn = _connect()
        try:
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
        finally:
            conn.close()

def get_last_download(chat_id: int, video_id: str, download_type: str) -> int:
    """Get the timestamp of the last time this video was sent to this chat."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT created_at FROM downloads WHERE chat_id = ? AND video_id = ? AND download_type = ? ORDER BY created_at DESC LIMIT 1",
                (chat_id, video_id, download_type)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

def get_stats() -> dict:
    """Get download statistics."""
    with _lock:
        conn = _connect()
        try:
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
            
            return {
                "total": total,
                "last_24h": last_24h,
                "by_type": by_type,
                "total_size": total_size
            }
        finally:
            conn.close()

# Transport statistics tracking (buffered in memory)
_transport_stats_buffer: dict[str, dict[str, int]] = {}
_transport_stats_lock = threading.Lock()
_last_transport_flush = time.time()
TRANSPORT_FLUSH_INTERVAL = 30.0  # seconds

def increment_transport_sent(addr: str):
    """Increment the sent counter for a transport address (buffered in memory)."""
    if not addr or not isinstance(addr, str) or "@" not in addr:
        return
    now = int(time.time())
    should_flush = False
    with _transport_stats_lock:
        if addr not in _transport_stats_buffer:
            _transport_stats_buffer[addr] = {"sent": 0, "recv": 0, "last_sent": 0, "last_recv": 0}
        _transport_stats_buffer[addr]["sent"] += 1
        _transport_stats_buffer[addr]["last_sent"] = now
        global _last_transport_flush
        if now - _last_transport_flush >= TRANSPORT_FLUSH_INTERVAL:
            should_flush = True
    if should_flush:
        flush_transport_stats()

def increment_transport_received(addr: str):
    """Increment the received counter for a transport address (buffered in memory)."""
    if not addr or not isinstance(addr, str) or "@" not in addr:
        return
    now = int(time.time())
    should_flush = False
    with _transport_stats_lock:
        if addr not in _transport_stats_buffer:
            _transport_stats_buffer[addr] = {"sent": 0, "recv": 0, "last_sent": 0, "last_recv": 0}
        _transport_stats_buffer[addr]["recv"] += 1
        _transport_stats_buffer[addr]["last_recv"] = now
        global _last_transport_flush
        if now - _last_transport_flush >= TRANSPORT_FLUSH_INTERVAL:
            should_flush = True
    if should_flush:
        flush_transport_stats()

def flush_transport_stats():
    """Flush buffered transport stats to the database in a single transaction."""
    global _last_transport_flush
    with _transport_stats_lock:
        if not _transport_stats_buffer:
            _last_transport_flush = time.time()
            return
        pending = dict(_transport_stats_buffer)
        _transport_stats_buffer.clear()
        _last_transport_flush = time.time()

    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            for addr, counts in pending.items():
                if not isinstance(addr, str) or "@" not in addr:
                    continue
                sent = int(counts.get("sent", 0))
                recv = int(counts.get("recv", 0))
                last_s = counts.get("last_sent") or None
                last_r = counts.get("last_recv") or None
                cursor.execute('''
                    INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_sent_at, last_received_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(addr) DO UPDATE SET
                        msgs_sent = msgs_sent + excluded.msgs_sent,
                        msgs_received = msgs_received + excluded.msgs_received,
                        last_sent_at = COALESCE(excluded.last_sent_at, transport_stats.last_sent_at),
                        last_received_at = COALESCE(excluded.last_received_at, transport_stats.last_received_at)
                ''', (addr, sent, recv, last_s, last_r))
            conn.commit()
        finally:
            conn.close()

def get_all_transport_stats() -> list[dict]:
    """Get statistics for all tracked transports."""
    flush_transport_stats()
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM transport_stats ORDER BY msgs_sent + msgs_received DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def cleanup_old_records(retention_days: int = 30) -> dict[str, int]:
    """Clean up old records across tables (downloads older than retention_days, url_map older than 7d, info_cache older than 1d)."""
    flush_transport_stats()
    now = int(time.time())
    downloads_cutoff = now - (retention_days * 86400)
    url_cutoff = now - 604800  # 7 days
    cache_cutoff = now - 86400  # 24 hours
    counts = {"downloads": 0, "url_map": 0, "info_cache": 0}

    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM downloads WHERE created_at < ?", (downloads_cutoff,))
            counts["downloads"] = cursor.rowcount
            cursor.execute("DELETE FROM url_map WHERE created_at < ?", (url_cutoff,))
            counts["url_map"] = cursor.rowcount
            cursor.execute("DELETE FROM info_cache WHERE created_at < ?", (cache_cutoff,))
            counts["info_cache"] = cursor.rowcount
            conn.commit()
            return counts
        finally:
            conn.close()

def get_admin_fingerprint():
    """Get the saved admin DC fingerprint."""
    return get_config("admin_dc_fingerprint")

def set_admin_fingerprint(fp):
    """Set the admin DC fingerprint."""
    set_config("admin_dc_fingerprint", fp)

def add_url_mapping(short_id: str, url: str):
    """Store a mapping between a short ID (hash) and a full URL."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO url_map (short_id, url) VALUES (?, ?)", (short_id, url))
            # Cleanup old mappings (older than 7 days) to keep DB small
            cursor.execute("DELETE FROM url_map WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - 604800")
            conn.commit()
        finally:
            conn.close()

def resolve_url(short_id: str) -> str | None:
    """Resolve a short ID back to the original full URL."""
    # Strip leading underscore if present (from command payload)
    if short_id.startswith("_"):
        short_id = short_id[1:]
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT url FROM url_map WHERE short_id = ?", (short_id,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_cached_info(video_id: str) -> tuple[str, str] | None:
    """Get cached metadata JSON and thumbnail path."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            # Only return if not older than 24 hours
            cursor.execute(
                "SELECT info_json, thumb_path FROM info_cache WHERE video_id = ? AND created_at >= CAST(strftime('%s','now') AS INTEGER) - 86400",
                (video_id,)
            )
            row = cursor.fetchone()
            return (row[0], row[1]) if row else None
        finally:
            conn.close()

def set_cached_info(video_id: str, info_json: str, thumb_path: str):
    """Store video metadata and thumbnail path in cache."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO info_cache (video_id, info_json, thumb_path) VALUES (?, ?, ?)",
                (video_id, info_json, thumb_path)
            )
            # Cleanup old cache entries (older than 24h)
            cursor.execute("DELETE FROM info_cache WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - 86400")
            conn.commit()
        finally:
            conn.close()

init_db()
