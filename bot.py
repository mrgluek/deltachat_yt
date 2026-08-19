import asyncio
import collections
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import contextlib
import urllib.request
import urllib.parse
import hashlib
import secrets

from deltachat2 import events, MsgData
from deltabot_cli import BotCli

import database

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("yt_bot")

VERSION = "1.6.31"

dc_cli = BotCli("ytbot")

# Global references
dc_bot_instance = None
dc_accid = None

# Delta Chat constants
DC_CONTACT_ID_SELF = 1

# Rate limiting: {from_id: last_request_timestamp}
_user_rate_limits: dict[int, float] = {}
RATE_LIMIT_SECONDS = 60

# Anti-spam: {chat_id: {video_id_type: timestamp}}
_chat_anti_spam: dict[int, dict[str, float]] = collections.defaultdict(dict)
ANTI_SPAM_SECONDS = 600  # 10 minutes

# Cache settings
CACHE_DIR = os.path.join("data", "cache")
CACHE_MAX_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
CACHE_MAX_AGE = 24 * 3600  # 24 hours

THUMB_CACHE_DIR = os.path.join("data", "thumbnails")
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)

# Semaphore for yt-dlp concurrency
_download_semaphore = asyncio.Semaphore(5)

# Thread-safe refcounted locks for per-video synchronization
class RefCountLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.refs = 0

_global_lock_mgr = threading.Lock()
_download_locks: dict[str, RefCountLock] = {}

_processed_msg_ids = set()
_processed_msg_lock = threading.Lock()

def _is_duplicate_msg(msg_id: int, handler: str) -> bool:
    with _processed_msg_lock:
        key = f"{handler}_{msg_id}"
        if key in _processed_msg_ids:
            return True
        _processed_msg_ids.add(key)
        if len(_processed_msg_ids) > 1000:
            # Simple cleanup, keep only the latest 500 to avoid memory leak
            latest = list(_processed_msg_ids)[-500:]
            _processed_msg_ids.clear()
            _processed_msg_ids.update(latest)
        return False

@contextlib.contextmanager
def get_download_lock(key: str):
    with _global_lock_mgr:
        if key not in _download_locks:
            _download_locks[key] = RefCountLock()
        ref_lock = _download_locks[key]
        ref_lock.refs += 1
        
    with ref_lock.lock:
        yield
        
    with _global_lock_mgr:
        ref_lock.refs -= 1
        if ref_lock.refs == 0:
            del _download_locks[key]

# Max duration in seconds
MAX_DURATION_VIDEO = 1800  # 30 minutes
MAX_DURATION_AUDIO = 3600  # 60 minutes
CHUNK_DURATION_VIDEO = 600  # 10 minutes (600 seconds)

# Max file size limit
MAX_FILESIZE_MB = 50
MAX_FILESIZE_BYTES = MAX_FILESIZE_MB * 1024 * 1024

# YouTube URL patterns
YT_URL_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)'
    r'([a-zA-Z0-9_-]{11})'
)
YT_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{11}$')


# Other Supported Video URLs (PeerTube, Vimeo, VK, Twitter, Reddit, Insta, TikTok, etc.)
SUPPORTED_URL_RE = re.compile(
    r'https?://(?:www\.|m\.)?(?:'
    r'vimeo\.com/|'
    r'vk\.com/video|'
    r'vkvideo\.ru/|'
    r'twitter\.com/|x\.com/|'
    r'reddit\.com/r/|'
    r'instagram\.com/|'
    r'tiktok\.com/|'
    r'twitch\.tv/|'
    r'bilibili\.com/|'
    r'rutube\.ru/|'
    r'dzen\.ru/|'
    r'ok\.ru/|'
    r'coub\.com/|'
    r'pinterest\.com/|'
    r'soundcloud\.com/|'
    r'imgur\.com/|'
    r'facebook\.com/|'
    r'music\.yandex\.(?:ru|com|by|kz)/|'
    r'[^/]+/w/'  # PeerTube
    r')[^\s]+'
)

YANDEX_PREVIEW_RE = re.compile(
    r'https?://(?:www\.)?yandex\.(?:ru|by|kz|com|ua)/video/preview/\d+'
)

AUDIO_ONLY_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:soundcloud\.com|music\.yandex\.(?:ru|com|by|kz)|music\.youtube\.com)/'
)

def _unescape_json_string(s: str) -> str:
    r"""Safely unescape JSON string values (like \/ and unicode escapes)."""
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s.replace('\\/', '/').replace('\\"', '"')


def _parse_time_param(url: str) -> tuple[int | None, int | None]:
    """Parse start and end times from URL parameters (e.g. t=51, t=51-70, start=10, end=20)."""
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        q_params = parse_qs(parsed.query)
        
        start_time = None
        end_time = None
        
        # 1. Check start and end parameters
        start_val = q_params.get('start')
        end_val = q_params.get('end')
        
        if start_val:
            start_time = _parse_single_time_str(start_val[0])
        if end_val:
            end_time = _parse_single_time_str(end_val[0])
            
        # 2. Check t parameter
        t_val = q_params.get('t')
        if t_val:
            val = t_val[0]
            # Try to split by range separator (- or ,)
            parts = re.split(r'[-–—,]', val)
            if len(parts) >= 2:
                s_parsed = _parse_single_time_str(parts[0])
                e_parsed = _parse_single_time_str(parts[1])
                if s_parsed is not None:
                    start_time = s_parsed
                if e_parsed is not None:
                    end_time = e_parsed
            else:
                s_parsed = _parse_single_time_str(val)
                if s_parsed is not None:
                    start_time = s_parsed
                    
        return start_time, end_time
    except Exception:
        pass
    return None, None


def _parse_single_time_str(val: str) -> int | None:
    """Parse a single time string like '51', '51s', '1m20s', '1h2m3s' into seconds."""
    val = val.strip().lower()
    if not val:
        return None
    if val.isdigit():
        return int(val)
    if val.endswith('s') and val[:-1].isdigit():
        return int(val[:-1])
        
    total_seconds = 0
    pattern = re.compile(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?')
    match = pattern.match(val)
    if match:
        h, m, s = match.groups()
        if h: total_seconds += int(h) * 3600
        if m: total_seconds += int(m) * 60
        if s: total_seconds += int(s)
        return total_seconds
    return None



def _resolve_yandex_preview(yandex_url: str) -> str | None:
    """Resolve Yandex video preview URL to the original video URL."""
    t_param = None
    try:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed_orig = urlparse(yandex_url)
        q_params = parse_qs(parsed_orig.query)
        if 't' in q_params:
            t_param = q_params['t'][0]
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            yandex_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('utf-8', errors='replace')
            
        candidates = []
        # 1. "videoUrl"
        for m in re.findall(r'"videoUrl"\s*:\s*"([^"]+)"', html):
            candidates.append(_unescape_json_string(m))
        # 2. "embedUrl"
        for m in re.findall(r'"embedUrl"\s*:\s*"([^"]+)"', html):
            candidates.append(_unescape_json_string(m))
        # 3. "host" -> "href"
        for m in re.findall(r'"host"\s*:\s*\{[^}]*"href"\s*:\s*"([^"]+)"', html):
            candidates.append(_unescape_json_string(m))

        for candidate in candidates:
            candidate = candidate.strip()
            if YT_URL_RE.search(candidate) or SUPPORTED_URL_RE.search(candidate):
                # Append timestamp parameter if it was in the original Yandex URL
                if t_param:
                    try:
                        parsed_cand = urlparse(candidate)
                        cand_query = parse_qs(parsed_cand.query)
                        if 't' not in cand_query:
                            cand_query['t'] = [t_param]
                            new_query = urlencode(cand_query, doseq=True)
                            candidate = urlunparse(parsed_cand._replace(query=new_query))
                    except Exception:
                        pass
                logger.info(f"Resolved Yandex preview {yandex_url} to: {candidate}")
                return candidate
    except Exception as e:
        logger.error(f"Error resolving Yandex preview URL {yandex_url}: {e}")
    return None


def _parse_yandex_music_url(url: str) -> tuple[str, str | None] | None:
    """Extract track_id and album_id from Yandex Music URL."""
    m = re.search(r'/album/(\d+)/track/(\d+)', url)
    if m:
        return m.group(2), m.group(1)
    m = re.search(r'/track/(\d+)', url)
    if m:
        return m.group(1), None
    return None


def _fetch_yandex_metadata(track_id: str, token: str) -> dict:
    """Fetch track metadata from Yandex Music API using OAuth token."""
    from yandex_music import Client
    yandex_proxy = os.getenv("YANDEX_PROXY") or os.getenv("PROXY")
    old_http = os.environ.get("HTTP_PROXY")
    old_https = os.environ.get("HTTPS_PROXY")
    if yandex_proxy:
        os.environ["HTTP_PROXY"] = yandex_proxy
        os.environ["HTTPS_PROXY"] = yandex_proxy
    try:
        client = Client(token).init()
        tracks = client.tracks(track_id)
        if not tracks:
            raise ValueError("Track not found on Yandex Music.")
        track = tracks[0]
        
        artists = [a.name for a in track.artists if a.name]
        artist_str = ", ".join(artists) if artists else "Unknown Artist"
        
        albums = [al.title for al in track.albums if al.title]
        album_str = albums[0] if albums else "Singles"
        
        year = None
        if track.albums and getattr(track.albums[0], 'year', None):
            year = str(track.albums[0].year)
            
        cover_url = None
        if track.cover_uri:
            cover_url = "https://" + track.cover_uri.replace("%%", "400x400")
            
        lyrics_text = None
        try:
            supplement = track.get_supplement()
            if supplement and supplement.lyrics:
                lyrics_text = supplement.lyrics.full_lyrics
        except Exception:
            pass

        return {
            "id": track_id,
            "title": track.title,
            "duration": track.duration_ms / 1000.0 if track.duration_ms else 0.0,
            "thumbnail": cover_url,
            "artist": artist_str,
            "uploader": artist_str,
            "album": album_str,
            "year": year,
            "lyrics": lyrics_text,
            "ext": "mp3",
            "extractor": "yandexmusic",
        }
    finally:
        if old_http is not None:
            os.environ["HTTP_PROXY"] = old_http
        else:
            os.environ.pop("HTTP_PROXY", None)
        if old_https is not None:
            os.environ["HTTPS_PROXY"] = old_https
        else:
            os.environ.pop("HTTPS_PROXY", None)


def _download_yandex_track(track_id: str, token: str, filepath: str):
    """Download Yandex Music track using OAuth token."""
    from yandex_music import Client
    yandex_proxy = os.getenv("YANDEX_PROXY") or os.getenv("PROXY")
    old_http = os.environ.get("HTTP_PROXY")
    old_https = os.environ.get("HTTPS_PROXY")
    if yandex_proxy:
        os.environ["HTTP_PROXY"] = yandex_proxy
        os.environ["HTTPS_PROXY"] = yandex_proxy
    try:
        client = Client(token).init()
        tracks = client.tracks(track_id)
        if not tracks:
            raise ValueError("Track not found on Yandex Music.")
        track = tracks[0]
        track.download(filepath)
    finally:
        if old_http is not None:
            os.environ["HTTP_PROXY"] = old_http
        else:
            os.environ.pop("HTTP_PROXY", None)
        if old_https is not None:
            os.environ["HTTPS_PROXY"] = old_https
        else:
            os.environ.pop("HTTPS_PROXY", None)


def _make_yt_url(video_id: str) -> str:
    if video_id.startswith("http://") or video_id.startswith("https://"):
        if "music.yandex." in video_id and _active_yandex_tld:
            video_id = re.sub(r'(https?://music\.yandex\.)(?:ru|com|by|kz|uz)(/)', f'\\g<1>{_active_yandex_tld}\\2', video_id)
        return video_id
    return f"https://youtu.be/{video_id}"


def _extract_video_id(text: str) -> str | None:
    """Extract YouTube video ID or recognize supported full URLs / short hashes.
    
    This function handles both raw IDs/URLs and full command strings 
    (e.g. '/yt_ID', '/yt_ID_0_600', or '/yt URL') by using non-anchored searches.
    """
    text = text.strip()
    
    # 0. Check underscore range suffix: e.g. ID_START_END (3RBNboYUlVI_600_1200 or 3RBNboYUlVI_0_600)
    m_range = re.search(r'(?:^|[_ ])([a-zA-Z0-9_-]{11,16})_(\d+)_(\d+)$', text)
    if m_range:
        base_id = m_range.group(1)
        start_sec = m_range.group(2)
        end_sec = m_range.group(3)
        resolved_base = database.resolve_url(base_id) if len(base_id) == 16 else None
        if resolved_base:
            sep = "&" if "?" in resolved_base else "?"
            return f"{resolved_base}{sep}start={start_sec}&end={end_sec}"
        return f"https://youtu.be/{base_id}?start={start_sec}&end={end_sec}"

    # Check query params attached to raw ID
    if ('?' in text or '&' in text) and not text.startswith("http://") and not text.startswith("https://"):
        parts = text.split('?', 1)
        base_id = parts[0].strip()
        query_part = '?' + parts[1] if len(parts) > 1 else ''
        m_base = re.search(r'(?:^|[_ ])([a-zA-Z0-9_-]{11,16})$', base_id)
        if m_base:
            target_id = m_base.group(1)
            resolved_base = database.resolve_url(target_id) if len(target_id) == 16 else None
            if resolved_base:
                sep = "&" if "?" in resolved_base else "?"
                return f"{resolved_base}{sep}{parts[1]}"
            return f"https://youtu.be/{target_id}{query_part}"

    # 1. Check if it's a short hash for a full URL (stored in database)
    m_hash = re.search(r'(?:^|[_ ])([a-f0-9]{16})$', text)
    if m_hash:
        resolved = database.resolve_url(m_hash.group(1))
        if resolved:
            return resolved
        
    # 2. Supported non-YouTube URLs: Return the FULL URL
    m_supported = SUPPORTED_URL_RE.search(text)
    if m_supported:
        return m_supported.group(0)
        
    # 3. YouTube URL -> 11-char ID (unless it has a time parameter)
    m_yt = YT_URL_RE.search(text)
    if m_yt:
        if 't=' in text or 'start=' in text or 'end=' in text:
            m_any_url = re.search(r'https?://[^\s]+', text)
            if m_any_url:
                return m_any_url.group(0)
        return m_yt.group(1)
        
    # 4. Direct YouTube 11-char ID or generic 11-16 char ID
    m_id = re.search(r'(?:^|[_ ])([a-zA-Z0-9_-]{11,16})$', text)
    if m_id:
        if len(m_id.group(1)) == 16:
            resolved = database.resolve_url(m_id.group(1))
            if resolved:
                return resolved
        return m_id.group(1)
        
    # 5. Fallback: if there's an http link anywhere, return it
    if "http://" in text or "https://" in text:
        m_any_url = re.search(r'https?://[^\s]+', text)
        if m_any_url:
            return m_any_url.group(0)

    return None


def _format_duration(seconds: int) -> str:
    if seconds < 0:
        return "?"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_time_range(start_sec: int, end_sec: int) -> str:
    return f"{_format_duration(start_sec)}-{_format_duration(end_sec)}"


def _get_base_video_id(video_id: str) -> str:
    """Extract clean video ID or short hash without query string or time range parameters."""
    base = video_id.split('?')[0].split('&')[0]
    m_range = re.match(r'^([a-zA-Z0-9_-]{11,16})_\d+_\d+$', base)
    if m_range:
        base = m_range.group(1)
    m_yt = YT_URL_RE.search(base)
    if m_yt:
        return m_yt.group(1)
    return base



def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# ── Admin helpers (from deltachat_ntfy pattern) ──

def _get_contact_fingerprint(bot, accid, contact_id, contact=None):
    self_fps = set()
    try:
        bot_addrs = []
        bot_addr = bot.rpc.get_config(accid, "addr")
        if bot_addr: bot_addrs.append(bot_addr.lower().strip())
            
        try:
            transports = bot.rpc.list_transports(accid)
            for t in transports:
                t_addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                if t_addr: bot_addrs.append(t_addr.lower().strip())
        except: pass
        
        if bot_addrs:
            for args in [(accid, contact_id), (contact_id,)]:
                try:
                    enc_info_self = bot.rpc.get_contact_encryption_info(*args)
                    if enc_info_self:
                        blocks = re.split(r'\n\s*\n', enc_info_self.strip())
                        for block in blocks:
                            if any(a in block.lower() for a in bot_addrs):
                                matches = re.findall(r'[0-9a-fA-F]{32,64}', "".join(block.split()).replace(':', ''))
                                self_fps.update(m.upper() for m in matches)
                        break
                except Exception:
                    continue
        if self_fps:
            logger.debug(f"Detected bot's own fingerprints from enc_info: {[f[-8:] for f in self_fps]}")
    except Exception as e:
        logger.error(f"Error detecting self-fingerprint: {e}")

    # Filter fingerprints from contact object
    if contact:
        get_val = getattr(contact, 'get', lambda k: getattr(contact, k, None))
        for attr in ['fingerprint', 'key_fingerprint', 'public_key']:
            val = get_val(attr)
            if val:
                matches = re.findall(r'[0-9a-fA-F]{32,64}', str(val).replace(' ', '').replace(':', ''))
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    return ",".join(valid_matches)
    try:
        fp = bot.rpc.get_contact_config(accid, contact_id, "fp")
        if fp and fp.upper().replace(' ', '') not in self_fps:
            return fp.upper().replace(' ', '')
    except Exception:
        pass

    for args in [(accid, contact_id), (contact_id,)]:
        try:
            enc_info = bot.rpc.get_contact_encryption_info(*args)
            if enc_info:
                cleaned = "".join(enc_info.split()).replace(':', '')
                matches = re.findall(r'[0-9a-fA-F]{32,64}', cleaned)
                # Filter out bot's own fingerprints
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    return ",".join(valid_matches)
        except Exception:
            continue
    return None


def _is_dc_admin(bot, accid, contact_id):
    """Check if the given contact is the bot administrator (by email or fingerprint)."""
    try:
        contact = None
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            pass
        
        if not contact:
            return False

        # Safety check: bot itself is never the admin
        if contact_id == 1:
            return False

        # 1. Check fingerprint (strongest)
        admin_fp = database.get_admin_fingerprint()
        if admin_fp:
            c_fp = _get_contact_fingerprint(bot, accid, contact_id, contact=contact)
            if c_fp:
                # c_fp might be a comma-separated list if multiple keys were found
                if admin_fp.upper() in c_fp.upper().split(','):
                    return True
            
            # If fingerprint is set but didn't match (or couldn't be retrieved), 
            # we REJECT even if email matches (security hardening)
            logger.warning(f"Admin check: Fingerprint mismatch or missing for {contact_id}")
            return False
        
        # 2. Check email (legacy or initial setup before /initadmin)
        sender_email = contact.address
        admin_email = database.get_config("admin_dc_email")
        if admin_email and sender_email and admin_email.lower().strip() == sender_email.lower().strip():
            return True
            
    except Exception as e:
        logger.error(f"Critical error in admin check: {e}")
    return False


def _is_rate_limited(bot, accid, from_id) -> bool:
    """Returns True if user is rate limited. Admin is exempt."""
    if _is_dc_admin(bot, accid, from_id):
        return False
    now = time.time()
    last = _user_rate_limits.get(from_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _user_rate_limits[from_id] = now
    return False


def _send(bot, accid, chat_id, text, file=None):
    """Send a message and track transport stats."""
    msg_data = MsgData(text=text)
    if file:
        msg_data.file = file
        
    try:
        msg_id = bot.rpc.send_msg(accid, chat_id, msg_data)
        
        # Track success stats
        try:
            addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
            if addr:
                database.increment_transport_sent(addr)
        except: pass
        
        return msg_id
    except Exception as e:
        logger.error(f"Failed to send message to chat {chat_id}: {e}")
        return None


def _react(bot, accid, msg_id, emoji: str):
    """Set a reaction on a message."""
    try:
        # emoji_list expects a list of strings
        bot.rpc.send_reaction(accid, msg_id, [emoji] if emoji else [])
    except Exception as e:
        logger.debug(f"Failed to set reaction on msg {msg_id}: {e}")


def _get_cache_id(video_id: str) -> str:
    if video_id.startswith("http://") or video_id.startswith("https://"):
        return hashlib.md5(video_id.encode()).hexdigest()[:16]
    return video_id

def _find_cached_file(video_id: str, download_type: str) -> str | None:
    """Find the cached file path if it exists, checking for different extensions."""
    cache_id = _get_cache_id(video_id)
    if download_type == "video":
        path = os.path.join(CACHE_DIR, f"{cache_id}.mp4")
        if os.path.exists(path):
            return path
    else:
        for ext in [".opus", ".m4a", ".mp3", ".ogg"]:
            path = os.path.join(CACHE_DIR, f"{cache_id}{ext}")
            if os.path.exists(path):
                return path
    return None


# ── yt-dlp wrappers ──

def _find_file_in_dir(directory: str, extensions: list[str] = None, prefix: str = None) -> str | None:
    """Find a file in directory matching extensions and/or prefix. Returns the largest match."""
    if not os.path.isdir(directory):
        return None
    candidates = []
    for f in os.listdir(directory):
        fpath = os.path.join(directory, f)
        if not os.path.isfile(fpath):
            continue
        
        # Exclude known incomplete download extensions
        if f.lower().endswith('.part') or f.lower().endswith('.ytdl') or f.lower().endswith('.temp'):
            continue
            
        match_ext = not extensions or any(f.lower().endswith(ext.lower()) for ext in extensions)
        match_prefix = not prefix or f.lower().startswith(prefix.lower())
        
        if match_ext and match_prefix:
            candidates.append(fpath)
    
    if not candidates and prefix:
        # Fallback: ignore extensions if we have a prefix and no match found
        for f in os.listdir(directory):
            fpath = os.path.join(directory, f)
            if os.path.isfile(fpath):
                # Exclude known incomplete download extensions
                if f.lower().endswith('.part') or f.lower().endswith('.ytdl') or f.lower().endswith('.temp'):
                    continue
                if f.lower().startswith(prefix.lower()):
                    candidates.append(fpath)
                
    if not candidates:
        return None
        
    # Return the largest file (to avoid picking up small .temp or .ytdl files)
    return max(candidates, key=os.path.getsize)


def _is_bot_blocked(bot, accid, msg) -> bool:
    """Return True if the message is from a bot and that bot is NOT whitelisted in ALLOWED_BOT_EMAILS."""
    if not getattr(msg, 'is_bot', False):
        return False
        
    allowed_bots_env = os.environ.get("ALLOWED_BOT_EMAILS", "")
    allowed_bots = [e.strip().lower() for e in allowed_bots_env.split(",") if e.strip()]
    
    try:
        contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_email = contact.address.lower().strip() if contact and contact.address else ""
    except Exception:
        sender_email = ""
        
    if sender_email and sender_email in allowed_bots:
        return False  # Allowed
        
    return True  # Blocked

# Proxy settings
PROXY = os.getenv("PROXY")
YANDEX_PROXY = os.getenv("YANDEX_PROXY")
BACKUP_PROXY = os.getenv("BACKUP_PROXY")

_active_yandex_tld = None

def _clean_error(err: str) -> str:
    """Clean up raw yt-dlp error messages to be user-friendly."""
    if not err:
        return "Unknown error"
    err_lower = err.lower()
    if "the page needs to be reloaded" in err_lower:
        return "YouTube error: The page needs to be reloaded (cookies in data/cookies.txt may be expired, invalid, or flagged by YouTube bot protection)."
    if "unable to download video data: http error 403: forbidden" in err_lower or "http error 403: forbidden" in err_lower:
        return "YouTube error: HTTP Error 403: Forbidden (access blocked by YouTube bot protection/IP restriction)."
    if "argument of type 'bool'" in err_lower:
        return "Yandex Music error: Content is unavailable (might be restricted to Russia/CIS, require a subscription, or Yandex is captcha-blocking the request)."
    if "uploader has not made this video available in your country" in err_lower:
        m = re.search(r'(This video is available in[^\n\r]+)', err, re.IGNORECASE)
        if m:
            info = m.group(1).strip()
            return f"This video is not available in the bot's country/region ({info})"
        return "This video is not available in the bot's country/region."
    return err

async def _fetch_video_info(video_id: str, use_cookies: bool = True, custom_proxy: str = None, player_client: str = None) -> tuple[dict | None, str | None]:
    """Fetch video metadata without downloading. Returns (info, error_msg)."""
    url = _make_yt_url(video_id)
    if "music.yandex." in url:
        token = os.getenv("YANDEX_TOKEN")
        if not token:
            return None, "YANDEX_TOKEN is not set. Yandex Music now requires a token due to API changes. Please configure it in your .env."
        parsed = _parse_yandex_music_url(url)
        if not parsed:
            return None, "Invalid Yandex Music URL format."
        track_id, album_id = parsed
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, _fetch_yandex_metadata, track_id, token)
            return info, None
        except Exception as e:
            logger.error(f"Yandex Music native info fetch failed: {e}")
            return None, f"Yandex Music error: {e}"

    cmd = [
        "yt-dlp", "--no-playlist", "--dump-json", "--no-warnings",
        "--no-check-certificate", "--geo-bypass",
        "--js-runtimes", "deno:/root/.deno/bin/deno",
        "--no-cache-dir",
        "--no-config",
        "--add-header", "Accept-Language: en-US,en;q=0.9",
    ]
    if player_client:
        cmd.extend(["--extractor-args", f"youtube:player_client={player_client}"])
    
    active_proxy = custom_proxy if custom_proxy is not None else PROXY
    if "yandex." in url and YANDEX_PROXY and custom_proxy is None:
        active_proxy = YANDEX_PROXY
    if active_proxy:
        cmd.extend(["--proxy", active_proxy])
        
    cookies_path = os.path.join("data", "cookies.txt")
    if use_cookies and os.path.exists(cookies_path):
        _sanitize_cookies_file(cookies_path)
        cmd.extend(["--cookies", cookies_path])
        
    cmd.append(url)
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and stdout:
            return json.loads(stdout), None
        
        err = stderr.decode(errors='replace').strip()
        cleaned_err = _clean_error(err)
        if not player_client and ("403" in err or "Forbidden" in err):
            logger.info(f"Received 403 Forbidden for info fetch on {video_id}. Retrying with mobile player_client...")
            return await _fetch_video_info(video_id, use_cookies=use_cookies, custom_proxy=custom_proxy, player_client="android,ios,web")

        logger.warning(f"yt-dlp info fetch failed for {video_id}: {err}")
        return None, cleaned_err[:200]
    except asyncio.TimeoutError:
        return None, "Timeout (30s)"
    except Exception as e:
        logger.error(f"Failed to fetch info for {video_id}: {e}")
        return None, str(e)


def _get_fallback_configs() -> list[dict]:
    cookies_exist = os.path.exists(os.path.join("data", "cookies.txt"))
    configs = []
    configs.append({"use_cookies": True, "proxy": None, "desc": "default proxy with cookies" if cookies_exist else "default proxy without cookies"})
    if cookies_exist:
        configs.append({"use_cookies": False, "proxy": None, "desc": "default proxy without cookies"})
    if BACKUP_PROXY:
        configs.append({"use_cookies": False, "proxy": BACKUP_PROXY, "desc": "backup proxy without cookies"})
        if cookies_exist:
            configs.append({"use_cookies": True, "proxy": BACKUP_PROXY, "desc": "backup proxy with cookies"})
    return configs


async def _fetch_video_info_with_fallback(video_id: str) -> tuple[dict | None, str | None, int]:
    """Fetch video metadata with cookie/proxy fallback attempts. Returns (info, error_msg, successful_config_idx)."""
    configs = _get_fallback_configs()
    info = None
    error = None
    successful_idx = 0
    
    for idx, cfg in enumerate(configs):
        info, error = await _fetch_video_info(video_id, use_cookies=cfg["use_cookies"], custom_proxy=cfg["proxy"])
        if info:
            successful_idx = idx
            break
        else:
            logger.info(f"Failed to fetch video info using {cfg['desc']}: {error}. Trying next config...")
            
    return info, error, successful_idx


async def _download_video(video_id: str, output_dir: str, max_height: int = 480, start_time: int = None, end_time: int = None, use_cookies: bool = True, custom_proxy: str = None, player_client: str = None) -> tuple[str | None, dict | None, str | None]:
    """Download video. Returns (filepath, info_dict, error_string)."""
    out_template = os.path.join(output_dir, "%(id)s_%(title).50s.%(ext)s")
    if start_time or end_time:
        max_duration = 7200  # Allow up to 2 hours if trimming is requested
    else:
        max_duration = MAX_DURATION_VIDEO
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--match-filter", f"duration<={max_duration}",
        "-f", f"bv[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b/best",
    ]
    if not start_time and not end_time:
        cmd.extend(["--max-filesize", f"{MAX_FILESIZE_MB}M"])
    cmd.extend([
        "--merge-output-format", "mp4",
        "--no-abort-on-error",
        "--ignore-errors",
        "--no-warnings",
        "--no-check-certificate", "--geo-bypass",
        "--js-runtimes", "deno:/root/.deno/bin/deno",
        "--no-cache-dir",
        "--no-config",
        "--add-header", "Accept-Language: en-US,en;q=0.9",
        "--print-json",
        "-o", out_template,
    ])
    if player_client:
        cmd.extend(["--extractor-args", f"youtube:player_client={player_client}"])
    
    url = _make_yt_url(video_id)
    active_proxy = custom_proxy if custom_proxy is not None else PROXY
    if "yandex." in url and YANDEX_PROXY and custom_proxy is None:
        active_proxy = YANDEX_PROXY
    if active_proxy:
        cmd.extend(["--proxy", active_proxy])
        
    cookies_path = os.path.join("data", "cookies.txt")
    if use_cookies and os.path.exists(cookies_path):
        _sanitize_cookies_file(cookies_path)
        cmd.extend(["--cookies", cookies_path])
        
    cmd.append(url)
    
    try:
        async with _download_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            err = stderr.decode(errors='replace').strip()
            if not player_client and ("403" in err or "Forbidden" in err or "requested format is not available" in err.lower()):
                logger.info(f"Received 403/format issue downloading video for {video_id}. Retrying with mobile player_client...")
                return await _download_video(video_id, output_dir, max_height, start_time, end_time, use_cookies=use_cookies, custom_proxy=custom_proxy, player_client="android,ios,web")

            if "duration" in err.lower() or "filter" in err.lower():
                return None, None, f"⏱ Video is longer than {MAX_DURATION_VIDEO // 60} minutes"
            if "max-filesize" in err.lower() or "filesize" in err.lower():
                return None, None, f"📦 Video exceeds {MAX_FILESIZE_MB} MB size limit"
            
            cleaned_err = _clean_error(err)
            logger.warning(f"Download failed for {video_id}: {err}")
            return None, None, f"yt-dlp error: {cleaned_err[:200]}"

        if not stdout:
            err = stderr.decode(errors='replace').strip()
            logger.warning(f"yt-dlp video returned no stdout for {video_id}. Stderr: {err}")
            
            if "filesize" in err.lower():
                return None, None, f"📦 Video exceeds {MAX_FILESIZE_MB} MB size limit"
            if "duration" in err.lower():
                return None, None, f"⏱ Video is longer than {MAX_DURATION_VIDEO // 60} minutes"
            
            return None, None, "⚠️ Video was filtered out (possibly too large or restricted)"

        info = json.loads(stdout) if stdout else {}
        filepath = info.get("_filename") or info.get("filename")
        if not filepath or not os.path.exists(filepath):
            if filepath:
                base = os.path.splitext(filepath)[0]
                for ext in ['.mp4', '.mkv', '.webm']:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        filepath = candidate
                        break
            if not filepath or not os.path.exists(filepath):
                search_prefix = video_id
                if video_id.startswith("http://") or video_id.startswith("https://"):
                    m = YT_URL_RE.search(video_id)
                    search_prefix = m.group(1) if m else None
                filepath = _find_file_in_dir(output_dir, ['.mp4', '.mkv', '.webm'], prefix=search_prefix)
        if filepath and os.path.exists(filepath):
            if start_time or end_time:
                trimmed_filepath = os.path.splitext(filepath)[0] + "_trimmed.mp4"
                trim_duration = (end_time - (start_time or 0)) if end_time else None
                trim_cmd = [
                    "ffmpeg", "-y", "-nostdin"
                ]
                if start_time:
                    trim_cmd.extend(["-ss", str(start_time)])
                trim_cmd.extend(["-i", filepath])
                if trim_duration is not None:
                    trim_cmd.extend(["-t", str(trim_duration)])
                trim_cmd.extend([
                    "-c", "copy",
                    trimmed_filepath
                ])
                try:
                    logger.info(f"Trimming video starting from {start_time or 0}s (duration: {trim_duration or 'inf'}s) locally using ffmpeg...")
                    proc_trim = await asyncio.create_subprocess_exec(
                        *trim_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        stdin=asyncio.subprocess.DEVNULL
                    )
                    await proc_trim.communicate()
                    if proc_trim.returncode == 0 and os.path.exists(trimmed_filepath):
                        os.remove(filepath)
                        filepath = trimmed_filepath
                    else:
                        logger.error(f"ffmpeg video trim failed with code {proc_trim.returncode}")
                except Exception as e:
                    logger.error(f"Error during local ffmpeg video trim: {e}")

            size = os.path.getsize(filepath)
            if size > MAX_FILESIZE_BYTES:
                os.remove(filepath)
                return None, info, f"📦 Video exceeds {MAX_FILESIZE_MB} MB size limit"
            return filepath, info, None
        
        # Check if there is a partial file indicating a size limit abort
        search_prefix = video_id
        if video_id.startswith("http://") or video_id.startswith("https://"):
            m = YT_URL_RE.search(video_id)
            search_prefix = m.group(1) if m else None
        
        if search_prefix:
            for f in os.listdir(output_dir):
                if f.lower().startswith(search_prefix.lower()) and (f.lower().endswith('.part') or f.lower().endswith('.ytdl')):
                    return None, info, f"📦 Video exceeds {MAX_FILESIZE_MB} MB size limit"
        
        logger.error(f"Video file not found for {video_id}. Expected: {filepath}. Dir contents: {os.listdir(output_dir)}")
        return None, info, "Download completed but file not found"
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except:
            pass
        return None, None, "⏱ Download timed out (5 min limit)"
    except Exception as e:
        logger.error(f"Error in _download_video for {video_id}: {e}")
        return None, None, f"Error: {e}"


def _extract_clean_lyrics(raw_content: str) -> str:
    """Extract clean plain-text lyrics from VTT, SRT, or LRC subtitle content."""
    if not raw_content:
        return ""
    lines = []
    prev_line = None
    for line in raw_content.splitlines():
        line = line.strip()
        if not line or line.startswith('WEBVTT') or line.startswith('Kind:') or line.startswith('Language:') or line.startswith('NOTE'):
            continue
        if re.match(r'^\d+$', line):
            continue
        # Remove timestamps: [00:12.34], 00:00:01.000 --> 00:00:04.000
        line = re.sub(r'\[\d{2}:\d{2}[\.:]\d{2,3}\]', '', line)
        line = re.sub(r'\d{2}:\d{2}:\d{2}[\.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[\.,]\d{3}.*', '', line)
        # Remove HTML tags & karaoke timing tags like <00:00:01.000>
        line = re.sub(r'<[^>]+>', '', line)
        line = line.strip()
        if line and line != prev_line:
            lines.append(line)
            prev_line = line
    return '\n'.join(lines)


def _convert_subtitles_to_lrc(sub_content: str) -> str:
    """Convert VTT / SRT / LRC content to normalized synchronized LRC format."""
    if not sub_content:
        return ""
    # If already standard LRC format (has lines starting with [mm:ss.xx])
    lrc_pattern = re.compile(r'\[\d{2}:\d{2}[\.:]\d{2,3}\]')
    if lrc_pattern.search(sub_content) and "-->" not in sub_content:
        clean_lrc = []
        for line in sub_content.splitlines():
            line = line.strip()
            if line:
                clean_lrc.append(line)
        return '\n'.join(clean_lrc)
    
    # Parse VTT / SRT cues
    lrc_lines = []
    blocks = re.split(r'\n\s*\n', sub_content.strip())
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        ts_match = None
        text_lines = []
        for l in lines:
            m = re.search(r'(\d{2}):(\d{2}):(\d{2})[\.,](\d{2,3})\s*-->', l)
            if m:
                hours, minutes, seconds, ms = m.groups()
                total_min = int(hours) * 60 + int(minutes)
                hundredths = int(ms[:2])
                ts_match = f"[{total_min:02d}:{int(seconds):02d}.{hundredths:02d}]"
            elif not l.startswith('WEBVTT') and not l.startswith('Kind:') and not l.startswith('Language:') and not re.match(r'^\d+$', l):
                cleaned = re.sub(r'<[^>]+>', '', l).strip()
                if cleaned:
                    text_lines.append(cleaned)
        if ts_match and text_lines:
            lrc_lines.append(f"{ts_match} {' '.join(text_lines)}")
    return '\n'.join(lrc_lines)


def _embed_lyrics_in_audio(audio_path: str, lyrics_text: str):
    """Embed lyrics into audio metadata tags (Vorbis comment for Opus/FLAC, USLT for MP3, ©lyr for MP4/M4A)."""
    if not lyrics_text or not audio_path or not os.path.exists(audio_path):
        return
    try:
        import mutagen
        ext = os.path.splitext(audio_path)[1].lower()
        if ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            audio = OggOpus(audio_path)
            audio["lyrics"] = lyrics_text
            audio["unsyncedlyrics"] = lyrics_text
            audio.save()
            logger.info(f"Embedded lyrics into Opus Vorbis comments: {audio_path}")
        elif ext == ".mp3":
            from mutagen.id3 import ID3, USLT, ID3NoHeaderError
            try:
                tags = ID3(audio_path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics_text))
            tags.save(audio_path)
            logger.info(f"Embedded lyrics into MP3 ID3 USLT tag: {audio_path}")
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            audio = MP4(audio_path)
            audio["\xa9lyr"] = lyrics_text
            audio.save()
            logger.info(f"Embedded lyrics into MP4/M4A tag: {audio_path}")
        elif ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(audio_path)
            audio["lyrics"] = lyrics_text
            audio.save()
            logger.info(f"Embedded lyrics into FLAC tag: {audio_path}")
    except Exception as e:
        logger.warning(f"Could not embed lyrics into {audio_path}: {e}")


def _tag_audio_file(filepath: str, info: dict, webpage_url: str = None):
    """Embed comprehensive metadata tags (Title, Artist, Album, Album Artist, Year, Cover Art, URL, Lyrics) into audio file."""
    if not filepath or not os.path.exists(filepath) or not info:
        return
    try:
        import mutagen
        ext = os.path.splitext(filepath)[1].lower()
        title = info.get("track") or info.get("title") or ""
        artist = info.get("artist") or info.get("uploader") or info.get("channel") or info.get("creator") or ""
        album = info.get("album") or "Singles"
        year = str(info.get("release_year") or info.get("year") or "")
        lyrics = info.get("lyrics") or ""
        url = webpage_url or info.get("webpage_url") or ""

        # Fetch cover bytes if thumbnail URL is provided
        cover_bytes = None
        thumbnail_url = info.get("thumbnail")
        if thumbnail_url and (thumbnail_url.startswith("http://") or thumbnail_url.startswith("https://")):
            try:
                req = urllib.request.Request(thumbnail_url, headers={'User-Agent': 'DeltaChatYTBot/1.0'})
                with urllib.request.urlopen(req, timeout=10) as r:
                    cover_bytes = r.read()
            except Exception as e:
                logger.debug(f"Could not download thumbnail for audio tagging from {thumbnail_url}: {e}")

        if ext == ".mp3":
            from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, COMM, APIC, USLT, ID3NoHeaderError
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()
            if title:
                tags["TIT2"] = TIT2(encoding=3, text=title)
            if artist:
                tags["TPE1"] = TPE1(encoding=3, text=artist)
                tags["TPE2"] = TPE2(encoding=3, text=artist)  # Album Artist
            if album:
                tags["TALB"] = TALB(encoding=3, text=album)
            if year:
                tags["TDRC"] = TDRC(encoding=3, text=year)
            if url:
                tags["COMM:desc:eng"] = COMM(encoding=3, lang='eng', desc='description', text=url)
                tags["COMM::eng"] = COMM(encoding=3, lang='eng', desc='', text=url)
            if lyrics:
                tags.delall("USLT")
                tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics))
            if cover_bytes:
                tags.delall("APIC")
                tags.add(APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,  # Front cover
                    desc='Cover',
                    data=cover_bytes
                ))
            tags.save(filepath, v2_version=3)
            logger.info(f"Successfully tagged MP3 with ID3v2.3: {filepath}")

        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            from mutagen.flac import Picture
            import base64
            audio = OggOpus(filepath)
            if title:
                audio["title"] = title
            if artist:
                audio["artist"] = artist
                audio["albumartist"] = artist
            if album:
                audio["album"] = album
            if year:
                audio["date"] = year
            if url:
                audio["description"] = url
                audio["comment"] = url
            if lyrics:
                audio["lyrics"] = lyrics
                audio["unsyncedlyrics"] = lyrics
            if cover_bytes:
                try:
                    pic = Picture()
                    pic.data = cover_bytes
                    pic.type = 3
                    pic.mime = "image/jpeg"
                    pic.desc = "Cover"
                    audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
                except Exception as e:
                    logger.debug(f"Could not embed picture in Vorbis comments: {e}")
            audio.save()
            logger.info(f"Successfully tagged Opus with Vorbis comments: {filepath}")

        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(filepath)
            if title:
                audio["\xa9nam"] = title
            if artist:
                audio["\xa9ART"] = artist
                audio["aART"] = artist
            if album:
                audio["\xa9alb"] = album
            if year:
                audio["\xa9day"] = year
            if url:
                audio["\xa9des"] = url
                audio["\xa9cmt"] = url
            if lyrics:
                audio["\xa9lyr"] = lyrics
            if cover_bytes:
                audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            logger.info(f"Successfully tagged MP4/M4A: {filepath}")

        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(filepath)
            if title:
                audio["title"] = title
            if artist:
                audio["artist"] = artist
                audio["albumartist"] = artist
            if album:
                audio["album"] = album
            if year:
                audio["date"] = year
            if url:
                audio["description"] = url
                audio["comment"] = url
            if lyrics:
                audio["lyrics"] = lyrics
            if cover_bytes:
                pic = Picture()
                pic.data = cover_bytes
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                audio.add_picture(pic)
            audio.save()
            logger.info(f"Successfully tagged FLAC: {filepath}")

    except Exception as e:
        logger.warning(f"Error tagging audio file {filepath}: {e}")


def _process_subtitles_and_lyrics(output_dir: str, safe_id: str, audio_path: str) -> tuple[str | None, str | None]:
    """
    Discovers downloaded subtitles in output_dir, converts to standardized .lrc,
    and embeds clean lyrics text into audio_path.
    Returns (lrc_filepath, clean_lyrics_text).
    """
    sub_files = []
    if os.path.exists(output_dir):
        for fname in os.listdir(output_dir):
            if fname.startswith(safe_id) and (fname.endswith('.lrc') or fname.endswith('.vtt') or fname.endswith('.srt')):
                sub_files.append(os.path.join(output_dir, fname))
            
    if not sub_files:
        return None, None
        
    chosen_sub = sub_files[0]
    for s in sub_files:
        if s.endswith('.lrc'):
            chosen_sub = s
            break
            
    try:
        with open(chosen_sub, 'r', encoding='utf-8', errors='ignore') as f:
            raw_content = f.read()
            
        clean_lyrics = _extract_clean_lyrics(raw_content)
        lrc_content = _convert_subtitles_to_lrc(raw_content)
        
        lrc_path = os.path.join(output_dir, f"{safe_id}.lrc")
        if lrc_content:
            with open(lrc_path, 'w', encoding='utf-8') as f:
                f.write(lrc_content)
                
        if clean_lyrics and audio_path and os.path.exists(audio_path):
            _embed_lyrics_in_audio(audio_path, clean_lyrics)
            
        return lrc_path if os.path.exists(lrc_path) else chosen_sub, clean_lyrics
    except Exception as e:
        logger.warning(f"Error processing subtitles for {safe_id}: {e}")
        return None, None


async def _download_audio(video_id: str, output_dir: str, duration: int, start_time: int = None, end_time: int = None, use_cookies: bool = True, custom_proxy: str = None, player_client: str = None) -> tuple[str | None, dict | None, str | None]:
    url = _make_yt_url(video_id)
    if "music.yandex." in url:
        token = os.getenv("YANDEX_TOKEN")
        if not token:
            return None, None, "YANDEX_TOKEN is not set. Yandex Music now requires a token due to API changes. Please configure it in your .env."
        parsed = _parse_yandex_music_url(url)
        if not parsed:
            return None, None, "Invalid Yandex Music URL format."
        track_id, album_id = parsed
        safe_id = _get_cache_id(video_id)
        final_filepath = os.path.join(output_dir, f"{safe_id}.mp3")
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, _fetch_yandex_metadata, track_id, token)
            await loop.run_in_executor(None, _download_yandex_track, track_id, token, final_filepath)
            filepath = final_filepath
            
            # If duration is long (> 10 min), transcode to mono/opus at 64k to save bandwidth/size
            if duration > 600:
                opus_filepath = os.path.join(output_dir, f"{safe_id}.opus")
                transcode_cmd = [
                    "ffmpeg", "-y", "-nostdin",
                    "-i", filepath,
                    "-ac", "1", "-ar", "24000", "-b:a", "64k",
                    "-c:a", "libopus",
                    opus_filepath
                ]
                proc = await asyncio.create_subprocess_exec(
                    *transcode_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                if os.path.exists(opus_filepath):
                    try:
                        os.remove(filepath)
                    except:
                        pass
                    filepath = opus_filepath

            # If trimming was requested, trim now
            if start_time or end_time:
                ext = os.path.splitext(filepath)[1]
                trimmed_filepath = os.path.join(output_dir, f"{safe_id}_trimmed{ext}")
                trim_cmd = ["ffmpeg", "-y", "-nostdin"]
                if start_time:
                    trim_cmd.extend(["-ss", str(start_time)])
                trim_cmd.extend(["-i", filepath])
                trim_duration = None
                if end_time:
                    trim_duration = end_time - (start_time or 0)
                if trim_duration is not None:
                    trim_cmd.extend(["-t", str(trim_duration)])
                trim_cmd.extend([
                    "-c", "copy" if ext != ".opus" else "libopus",
                    trimmed_filepath
                ])
                proc = await asyncio.create_subprocess_exec(
                    *trim_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                if os.path.exists(trimmed_filepath):
                    try:
                        os.remove(filepath)
                    except:
                        pass
                    filepath = trimmed_filepath

            # Tag the audio file with Title, Artist, Album, Album Artist, Year, Cover Art, URL, Lyrics
            _tag_audio_file(filepath, info, webpage_url=url)
            
            # If lyrics are present from Yandex Music, also generate companion .lrc file in output_dir
            if info.get("lyrics"):
                lrc_path = os.path.join(output_dir, f"{safe_id}.lrc")
                if not os.path.exists(lrc_path):
                    lrc_content = _convert_subtitles_to_lrc(info["lyrics"])
                    if lrc_content:
                        with open(lrc_path, "w", encoding="utf-8") as f:
                            f.write(lrc_content)

            return filepath, info, None
        except Exception as e:
            logger.error(f"Yandex Music native download failed: {e}")
            return None, None, f"Yandex Music error: {e}"

    if start_time:
        if end_time:
            effective_duration = max(0, end_time - start_time)
        else:
            effective_duration = max(0, duration - start_time)
    elif end_time:
        effective_duration = max(0, end_time)
    else:
        effective_duration = duration

    if effective_duration <= 600:
        # Keep original format, preferring opus (for YouTube), then m4a (AAC), then best audio/muxed stream to avoid transcoding
        fmt = "best"
        format_selector = "ba[acodec=opus]/ba[ext=m4a]/ba/b/best"
        pp_args = []
    else:
        # For long audio (> 10 min), transcode and compress to Opus 64k mono to stay under 30MB limit
        fmt = "opus"
        format_selector = None
        pp_args = [
            "--postprocessor-args",
            "ffmpeg:-ac 1 -ar 24000 -b:a 64k"
        ]

    safe_id = _get_cache_id(video_id)
    out_template = os.path.join(output_dir, f"{safe_id}.%(ext)s")
    
    if start_time or end_time:
        max_duration = 7200  # Allow up to 2 hours if trimming is requested
    else:
        max_duration = MAX_DURATION_AUDIO
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--match-filter", f"duration<={max_duration}",
    ]
    if format_selector:
        cmd.extend(["-f", format_selector])

    cmd.extend([
        "-x",
        "--audio-format", fmt,
        "--embed-metadata",
        "--embed-thumbnail",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*,ru.*,orig,-live_chat",
        "--convert-subs", "lrc",
        "--no-abort-on-error",
        "--ignore-errors",
        "--parse-metadata", "%(webpage_url)s:%(meta_comment)s",
        "--parse-metadata", "%(webpage_url)s:%(meta_description)s",
        "--no-warnings",
        "--no-check-certificate", "--geo-bypass",
        "--js-runtimes", "deno:/root/.deno/bin/deno",
        "--no-cache-dir",
        "--no-config",
        "--add-header", "Accept-Language: en-US,en;q=0.9",
        "--print-json",
        "-o", out_template,
    ])
    if player_client:
        cmd.extend(["--extractor-args", f"youtube:player_client={player_client}"])
    if pp_args:
        cmd.extend(pp_args)
    
    active_proxy = custom_proxy if custom_proxy is not None else PROXY
    if active_proxy:
        cmd.extend(["--proxy", active_proxy])
        
    cookies_path = os.path.join("data", "cookies.txt")
    if use_cookies and os.path.exists(cookies_path):
        _sanitize_cookies_file(cookies_path)
        cmd.extend(["--cookies", cookies_path])
        
    cmd.append(_make_yt_url(video_id))
    
    try:
        async with _download_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            err = stderr.decode(errors='replace').strip()
            if not player_client and ("403" in err or "Forbidden" in err or "requested format is not available" in err.lower()):
                logger.info(f"Received 403/format issue downloading audio for {video_id}. Retrying with mobile player_client...")
                return await _download_audio(video_id, output_dir, duration, start_time=start_time, end_time=end_time, use_cookies=use_cookies, custom_proxy=custom_proxy, player_client="android,ios,web")

            if "duration" in err.lower() or "filter" in err.lower():
                return None, None, f"⏱ Audio is longer than {MAX_DURATION_AUDIO // 60} minutes"
            
            cleaned_err = _clean_error(err)
            logger.warning(f"Audio download failed for {video_id}: {err}")
            return None, None, f"yt-dlp error: {cleaned_err[:200]}"

        if not stdout:
            err = stderr.decode(errors='replace').strip()
            logger.warning(f"yt-dlp audio returned no stdout for {video_id}. Stderr: {err}")
            if "duration" in err.lower():
                return None, None, f"⏱ Audio is longer than {MAX_DURATION_AUDIO // 60} minutes"
            return None, None, "⚠️ Audio was filtered out or restricted"

        info = json.loads(stdout.decode(errors='replace').strip())

        filepath = None
        expected_path = os.path.join(output_dir, f"{safe_id}.opus")
        if os.path.exists(expected_path):
            filepath = expected_path
        else:
            json_path = info.get("_filename") or info.get("filename")
            if json_path:
                base = os.path.splitext(json_path)[0]
                for ext in ['.opus', '.mp3', '.m4a', '.webm']:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        filepath = candidate
                        break
            if not filepath:
                filepath = _find_file_in_dir(output_dir, ['.opus', '.mp3', '.m4a', '.webm'], prefix=safe_id)

        if filepath and os.path.exists(filepath):
            # Process and embed lyrics into audio file & generate standardized .lrc
            _process_subtitles_and_lyrics(output_dir, safe_id, filepath)
            # Ensure comprehensive tags and cover art are embedded
            _tag_audio_file(filepath, info, webpage_url=_make_yt_url(video_id))
            if start_time or end_time:
                ext = os.path.splitext(filepath)[1]
                trimmed_filepath = os.path.splitext(filepath)[0] + f"_trimmed{ext}"
                trim_duration = (end_time - (start_time or 0)) if end_time else None
                trim_cmd = [
                    "ffmpeg", "-y", "-nostdin"
                ]
                if start_time:
                    trim_cmd.extend(["-ss", str(start_time)])
                trim_cmd.extend(["-i", filepath])
                if trim_duration is not None:
                    trim_cmd.extend(["-t", str(trim_duration)])
                trim_cmd.extend([
                    "-c", "copy",
                    trimmed_filepath
                ])
                try:
                    logger.info(f"Trimming audio starting from {start_time or 0}s (duration: {trim_duration or 'inf'}s) locally using ffmpeg...")
                    proc_trim = await asyncio.create_subprocess_exec(
                        *trim_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        stdin=asyncio.subprocess.DEVNULL
                    )
                    await proc_trim.communicate()
                    if proc_trim.returncode == 0 and os.path.exists(trimmed_filepath):
                        os.remove(filepath)
                        filepath = trimmed_filepath
                    else:
                        logger.error(f"ffmpeg audio trim failed with code {proc_trim.returncode}")
                except Exception as e:
                    logger.error(f"Error during local ffmpeg audio trim: {e}")

            size = os.path.getsize(filepath)
            if size > MAX_FILESIZE_BYTES:
                os.remove(filepath)
                return None, info, f"📦 Audio file exceeds {MAX_FILESIZE_MB} MB"
            return filepath, info, None
        
        # Check if there is a partial file indicating a size limit abort
        for f in os.listdir(output_dir):
            if f.lower().startswith(safe_id.lower()) and (f.lower().endswith('.part') or f.lower().endswith('.ytdl')):
                return None, info, f"📦 Audio file exceeds {MAX_FILESIZE_MB} MB"
        
        logger.error(f"Audio file not found for {video_id}. Expected: {filepath}. Dir contents: {os.listdir(output_dir)}")
        return None, info, "Download completed but file not found"
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except:
            pass
        return None, None, "⏱ Download timed out (5 min limit)"
    except Exception as e:
        logger.error(f"Error in _download_audio for {video_id}: {e}")
        return None, None, f"Error: {e}"


def _get_navidrome_config() -> tuple[str | None, str | None, str | None, str | None, str | None, str]:
    """Get Navidrome / Subsonic configuration from environment or database."""
    url = os.getenv("NAVIDROME_URL") or os.getenv("SUBSONIC_URL") or database.get_config("navidrome_url")
    user = os.getenv("NAVIDROME_USER") or os.getenv("SUBSONIC_USER") or database.get_config("navidrome_user")
    password = os.getenv("NAVIDROME_PASSWORD") or os.getenv("SUBSONIC_PASSWORD") or database.get_config("navidrome_password")
    token = os.getenv("NAVIDROME_TOKEN") or os.getenv("SUBSONIC_TOKEN") or database.get_config("navidrome_token")
    salt = os.getenv("NAVIDROME_SALT") or os.getenv("SUBSONIC_SALT") or database.get_config("navidrome_salt")
    music_dir = os.getenv("NAVIDROME_MUSIC_DIR") or os.getenv("MUSIC_DIR") or database.get_config("navidrome_music_dir")
    if not music_dir:
        if os.path.exists("/music") and os.path.isdir("/music"):
            music_dir = "/music"
        else:
            music_dir = os.path.join("data", "music")
    return url, user, password, token, salt, music_dir


def _sanitize_filename(name: str, max_length: int = 100) -> str:
    """Sanitize directory and file names for cross-platform compatibility."""
    if not name:
        return "Unknown"
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', '_', str(name))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip('. ')
    if not cleaned:
        return "Unknown"
    return cleaned[:max_length].rstrip('. ')


def _trigger_subsonic_scan(server_url: str, user: str, password: str = None, token: str = None, salt: str = None) -> tuple[bool, str]:
    """Trigger a library scan on Navidrome / Subsonic server using REST API."""
    if token and salt:
        auth_token = token
        auth_salt = salt
    elif password:
        auth_salt = secrets.token_hex(8)
        auth_token = hashlib.md5((password + auth_salt).encode('utf-8')).hexdigest()
    else:
        return False, "No password or token+salt configured for Navidrome authentication."
    
    base_url = server_url.rstrip('/')
    params = {
        'u': user,
        't': auth_token,
        's': auth_salt,
        'v': '1.16.1',
        'c': 'DeltaChatYTBot',
        'f': 'json'
    }
    query_string = urllib.parse.urlencode(params)
    scan_url = f"{base_url}/rest/startScan.view?{query_string}"
    
    try:
        req = urllib.request.Request(
            scan_url,
            headers={'User-Agent': 'DeltaChatYTBot/1.0'}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            sub_resp = data.get('subsonic-response', {})
            if sub_resp.get('status') == 'ok':
                scan_status = sub_resp.get('scanStatus', {})
                count = scan_status.get('count')
                if count is not None:
                    return True, f"Scan initiated ({count} files indexed)"
                return True, "Scan initiated successfully"
            else:
                err = sub_resp.get('error', {})
                err_msg = err.get('message') or f"Code {err.get('code')}"
                return False, f"Subsonic error: {err_msg}"
    except Exception as e:
        logger.error(f"Error triggering Navidrome scan at {base_url}: {e}")
        return False, f"Connection failed: {e}"


def _sanitize_cookies_file(cookies_path: str = os.path.join("data", "cookies.txt")) -> bool:
    """
    Sanitize and normalize cookies.txt on disk in-place.
    Ensures '# Netscape HTTP Cookie File' magic header is present on line 1,
    fixes domain_specified flags to prevent standard library AssertionError (e.g. vk.com\tTRUE -> vk.com\tFALSE),
    and converts irregular whitespace to tabs.
    """
    if not cookies_path or not os.path.exists(cookies_path):
        return False

    try:
        with open(cookies_path, 'r', encoding='utf-8', errors='replace') as f:
            raw_lines = f.readlines()

        cleaned_lines = ["# Netscape HTTP Cookie File\n"]

        for line in raw_lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Skip existing magic headers since we already put one at line 1
            if stripped.startswith("# Netscape HTTP Cookie File") or stripped.startswith("# HTTP Cookie File"):
                continue

            prefix = ""
            cookie_line = line
            if cookie_line.startswith("#HttpOnly_"):
                prefix = "#HttpOnly_"
                cookie_line = cookie_line[len("#HttpOnly_"):]
            elif cookie_line.startswith("#"):
                cleaned_lines.append(line if line.endswith("\n") else line + "\n")
                continue

            parts = cookie_line.rstrip("\r\n").split("\t")
            if len(parts) < 7:
                # Fallback if separated by multiple spaces
                parts = re.split(r'\t+|\s{2,}', cookie_line.rstrip("\r\n"))

            if len(parts) >= 7:
                domain = parts[0]
                initial_dot = domain.startswith(".")
                expected_flag = "TRUE" if initial_dot else "FALSE"
                parts[1] = expected_flag
                cleaned_lines.append(prefix + "\t".join(parts) + "\n")

        # Write back to disk atomically
        temp_file = cookies_path + ".tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            f.writelines(cleaned_lines)
        os.replace(temp_file, cookies_path)
        return True
    except Exception as e:
        logger.warning(f"Failed to sanitize {cookies_path}: {e}")
        return False


def _load_cookiejar(cookies_path: str):
    """Safely load Netscape cookies into MozillaCookieJar, ensuring file is sanitized first."""
    import http.cookiejar
    jar = http.cookiejar.MozillaCookieJar()
    if not cookies_path or not os.path.exists(cookies_path):
        return jar

    _sanitize_cookies_file(cookies_path)

    try:
        jar.load(cookies_path, ignore_discard=True, ignore_expires=True)
    except Exception as e:
        logger.warning(f"Failed to load sanitized cookies from {cookies_path}: {e}")

    return jar


def _check_youtube_status() -> tuple[bool, str]:
    """Check YouTube cookie configuration, authentication, and session status."""
    cookies_path = os.path.join("data", "cookies.txt")
    if not os.path.exists(cookies_path):
        return False, "Guest mode (no cookies.txt configured)"

    cookie_jar = _load_cookiejar(cookies_path)

    # Check for YouTube / Google cookies
    yt_cookies = [c for c in cookie_jar if "youtube.com" in c.domain or "google.com" in c.domain]
    if not yt_cookies:
        return False, "Guest mode (cookies.txt loaded, but contains no YouTube cookies)"

    # Check for login session cookies
    login_cookies = [c for c in yt_cookies if c.name in ("LOGIN_INFO", "SAPISID", "SID", "SSID", "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-1PSID", "__Secure-3PSID")]
    if not login_cookies:
        return False, "Guest mode (cookies loaded, but no active login session tokens found)"

    handlers = [urllib.request.HTTPCookieProcessor(cookie_jar)]
    active_proxy = PROXY
    if active_proxy:
        handlers.append(urllib.request.ProxyHandler({'http': active_proxy, 'https': active_proxy}))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ]

    try:
        req = urllib.request.Request("https://www.youtube.com/")
        with opener.open(req, timeout=10) as response:
            html = response.read().decode('utf-8', errors='replace')
            
            # Check for YouTube bot-block / reload challenge
            if "The page needs to be reloaded" in html:
                return False, 'Session EXPIRED or FLAGGED ("The page needs to be reloaded")'
            
            if "captcha" in html.lower() or "recaptcha" in html.lower() or "consent.youtube.com" in response.geturl():
                return False, "CAPTCHA / verification challenge required"

            # Check if session is logged in
            is_logged_in = False
            if re.search(r'["\']LOGGED_IN["\']\s*:\s*true', html, re.IGNORECASE):
                is_logged_in = True
            elif re.search(r'ytcfg\.set\(.*["\']LOGGED_IN["\']\s*:\s*true', html, re.DOTALL | re.IGNORECASE):
                is_logged_in = True

            if not is_logged_in and re.search(r'["\']LOGGED_IN["\']\s*:\s*false', html, re.IGNORECASE):
                return False, "Session EXPIRED or INVALID (logged out)"

            # Try to extract account/channel name or handle
            account_name = None
            patterns = [
                r'"accountName"\s*:\s*\{\s*"simpleText"\s*:\s*"([^"]+)"',
                r'"channelHandle"\s*:\s*"(@[^"]+)"',
                r'"avatar"\s*:\s*\{[^}]*"accessibility"\s*:\s*\{\s*"accessibilityData"\s*:\s*\{\s*"label"\s*:\s*"Account profile photo[^"]*for\s+([^"]+)"',
                r'"avatar"\s*:\s*\{[^}]*"accessibility"\s*:\s*\{\s*"accessibilityData"\s*:\s*\{\s*"label"\s*:\s*"([^"]+)"',
                r'"USER_NAME"\s*:\s*"([^"]+)"',
            ]
            for pat in patterns:
                m = re.search(pat, html)
                if m:
                    candidate = m.group(1).strip()
                    if candidate and not candidate.startswith("Account profile"):
                        account_name = candidate
                        break

            if account_name:
                return True, f"Logged in as {account_name}"
            elif is_logged_in:
                return True, "Session ACTIVE (Logged in)"
            else:
                return True, "Cookies loaded (Tokens present)"
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return False, "HTTP 403 Forbidden (Blocked/Rate-limited by YouTube)"
        if e.code in (301, 302, 303, 307):
            return False, "Session EXPIRED (Redirected to login)"
        return False, f"HTTP Error {e.code}: {e.reason}"
    except Exception as e:
        return False, f"Connection check failed ({e})"


def _check_yandex_status() -> tuple[bool, str]:
    """Check Yandex Music authentication and Plus subscription status."""
    global _active_yandex_tld

    token = os.getenv("YANDEX_TOKEN")
    if token:
        try:
            from yandex_music import Client
            yandex_proxy = os.getenv("YANDEX_PROXY") or os.getenv("PROXY")
            old_http = os.environ.get("HTTP_PROXY")
            old_https = os.environ.get("HTTPS_PROXY")
            if yandex_proxy:
                os.environ["HTTP_PROXY"] = yandex_proxy
                os.environ["HTTPS_PROXY"] = yandex_proxy
            try:
                client = Client(token).init()
                status = client.account_status()
                display_name = getattr(getattr(client, 'me', None), 'account', None)
                name = getattr(display_name, 'display_name', None) or getattr(display_name, 'login', None) or "User"
                if status.plus.has_plus:
                    _active_yandex_tld = 'ru'
                    return True, f"Plus ACTIVE via YANDEX_TOKEN (Account: {name})"
                else:
                    return False, f"Plus INACTIVE via YANDEX_TOKEN (Account: {name})"
            finally:
                if old_http is not None:
                    os.environ["HTTP_PROXY"] = old_http
                else:
                    os.environ.pop("HTTP_PROXY", None)
                if old_https is not None:
                    os.environ["HTTPS_PROXY"] = old_https
                else:
                    os.environ.pop("HTTPS_PROXY", None)
        except Exception as e:
            return False, f"Token verification failed ({e})"

    cookies_path = os.path.join("data", "cookies.txt")
    if not os.path.exists(cookies_path):
        return False, "Not configured (guest mode)"

    cookie_jar = _load_cookiejar(cookies_path)

    yandex_cookies = [cookie for cookie in cookie_jar if "yandex" in cookie.domain]
    if not yandex_cookies:
        return False, "Not configured (no Yandex cookies in cookies.txt)"

    # Gather Yandex domains present in cookies
    yandex_tlds = set()
    for cookie in cookie_jar:
        m = re.search(r'\byandex\.(ru|by|kz|uz|com)\b', cookie.domain)
        if m:
            yandex_tlds.add(m.group(1))

    tlds_to_try = list(yandex_tlds) if yandex_tlds else ['ru', 'by', 'kz', 'uz', 'com']

    handlers = [urllib.request.HTTPCookieProcessor(cookie_jar)]
    active_proxy = YANDEX_PROXY or PROXY
    if active_proxy:
        handlers.append(urllib.request.ProxyHandler({'http': active_proxy, 'https': active_proxy}))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        ("Referer", "https://music.yandex.ru/"),
        ("X-Requested-With", "XMLHttpRequest")
    ]

    last_err = None
    for tld in tlds_to_try:
        try:
            req = urllib.request.Request(f"https://music.yandex.{tld}/handlers/library.jsx")
            with opener.open(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                owner = data.get("owner", {})
                login = owner.get("login")
                name = owner.get("name") or login or "User"

                # Check a test track to verify plus/access on this domain
                track_req = urllib.request.Request(f"https://music.yandex.{tld}/handlers/track.jsx?track=150402031:41648883")
                with opener.open(track_req, timeout=10) as tr_response:
                    tr_data = json.loads(tr_response.read().decode('utf-8'))
                    if "track" in tr_data:
                        _active_yandex_tld = tld
                        return True, f"Plus ACTIVE on music.yandex.{tld} (Account: {name})"
                    else:
                        return False, f"Logged in on music.yandex.{tld} as {name} (Plus INACTIVE)"
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode('utf-8', errors='replace')
                if "captcha" in body.lower() or ("запросы" in body and "автоматические" in body):
                    last_err = f"CAPTCHA blocked on .{tld}"
                    continue
            except Exception:
                pass
            if e.code == 404:
                last_err = f"Session not found on .{tld}"
            else:
                last_err = f"HTTP {e.code} on .{tld}"
        except Exception as e:
            last_err = str(e)

    return False, f"Cookies EXPIRED, INVALID or BLOCKED ({last_err or 'unknown error'})"


def _check_navidrome_status() -> tuple[bool, str]:
    """Check Navidrome connectivity, authentication, and music directory."""
    nav_url, nav_user, nav_password, nav_token, nav_salt, music_dir = _get_navidrome_config()
    has_auth = bool(nav_password or (nav_token and nav_salt))
    if not nav_url or not nav_user or not has_auth:
        return False, "Not configured (set NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD or NAVIDROME_TOKEN+SALT)"

    if nav_token and nav_salt:
        auth_token = nav_token
        auth_salt = nav_salt
    else:
        auth_salt = secrets.token_hex(8)
        auth_token = hashlib.md5((nav_password + auth_salt).encode('utf-8')).hexdigest()

    base_url = nav_url.rstrip('/')
    params = {
        'u': nav_user,
        't': auth_token,
        's': auth_salt,
        'v': '1.16.1',
        'c': 'DeltaChatYTBot',
        'f': 'json'
    }
    ping_url = f"{base_url}/rest/ping.view?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(ping_url, headers={'User-Agent': 'DeltaChatYTBot/1.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            sub_resp = data.get('subsonic-response', {})
            if sub_resp.get('status') == 'ok':
                server_ver = sub_resp.get('serverVersion') or sub_resp.get('version') or "OK"
                server_type = str(sub_resp.get('type') or 'Navidrome').capitalize()
                dir_exists = os.path.exists(music_dir) and os.path.isdir(music_dir)
                dir_status = "folder OK" if dir_exists else "folder missing"
                return True, f"{server_type} v{server_ver} ({dir_status}: `{music_dir}`)"
            else:
                err = sub_resp.get('error', {})
                return False, f"Subsonic error: {err.get('message', 'Unknown')} (code {err.get('code')})"
    except Exception as e:
        return False, f"Connection failed ({e})"


def _save_to_navidrome(filepath: str, info: dict, music_dir: str, video_id: str = None) -> tuple[str | None, str | None, str | None]:
    """
    Save downloaded audio file (and companion .lrc file if present) into Navidrome music directory organized as Artist/Album/Title.ext.
    Returns (destination_audio_path, destination_lrc_path, error_message).
    """
    try:
        artist = (
            info.get("artist")
            or info.get("uploader")
            or info.get("channel")
            or info.get("creator")
            or "Unknown Artist"
        )
        album = info.get("album") or "Singles"
        title = info.get("track") or info.get("title") or "Unknown Track"
        
        artist_clean = _sanitize_filename(artist)
        album_clean = _sanitize_filename(album)
        title_clean = _sanitize_filename(title)
        
        ext = os.path.splitext(filepath)[1].lower()
        if not ext:
            ext = ".opus"
            
        target_dir = os.path.join(music_dir, artist_clean, album_clean)
        os.makedirs(target_dir, exist_ok=True)
        
        dest_filename = f"{title_clean}{ext}"
        dest_path = os.path.join(target_dir, dest_filename)
        
        shutil.copy2(filepath, dest_path)
        logger.info(f"Saved audio file to Navidrome directory: {dest_path}")

        # Check for companion .lrc file
        dest_lrc = None
        lrc_src = os.path.splitext(filepath)[0] + ".lrc"
        if not os.path.exists(lrc_src) and video_id:
            candidate = os.path.join(CACHE_DIR, f"{_get_cache_id(video_id)}.lrc")
            if os.path.exists(candidate):
                lrc_src = candidate
        if os.path.exists(lrc_src):
            dest_lrc = os.path.join(target_dir, f"{title_clean}.lrc")
            shutil.copy2(lrc_src, dest_lrc)
            logger.info(f"Saved companion lyrics file to Navidrome directory: {dest_lrc}")

        return dest_path, dest_lrc, None
    except Exception as e:
        logger.error(f"Failed to save audio to Navidrome directory: {e}")
        return None, None, str(e)


def _run_ytms(bot, accid, msg, video_id: str):
    """Run /ytms in a background thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_ytms(bot, accid, msg, video_id))
    finally:
        loop.close()


async def _do_ytms(bot, accid, msg, video_id: str):
    """Download audio, tag it, save it into Navidrome library folder, and trigger Subsonic scan."""
    chat_id = msg.chat_id
    req_msg_id = msg.id
    
    # Check Navidrome config
    nav_url, nav_user, nav_password, nav_token, nav_salt, music_dir = _get_navidrome_config()
    has_auth = bool(nav_password or (nav_token and nav_salt))
    if not nav_url or not nav_user or not has_auth:
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, "⚠️ Navidrome is not configured. Please set NAVIDROME_URL, NAVIDROME_USER, and either NAVIDROME_PASSWORD or NAVIDROME_TOKEN+NAVIDROME_SALT in your .env configuration.")
        return

    # 0. Resolve Yandex preview URL if target is a Yandex preview link
    if YANDEX_PREVIEW_RE.search(video_id):
        resolved = _resolve_yandex_preview(video_id)
        if resolved:
            video_id = resolved
        else:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, "❌ Could not extract video link from Yandex preview.")
            return

    logger.info(f"Starting _do_ytms for {video_id} in chat {chat_id}")
    
    process_key = f"ytms_{chat_id}_{video_id}"
    with _processing_lock:
        if process_key in _processing:
            return
        _processing.add(process_key)

    try:
        if not _check_disk_space(bot, accid, msg):
            return

        _react(bot, accid, req_msg_id, "⏳")

        # 1. Fetch info
        configs = _get_fallback_configs()
        info, error, successful_config_index = await _fetch_video_info_with_fallback(video_id)
        if not info:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, f"❌ Could not fetch audio info: {error or 'Unknown error'}")
            return
        
        duration = int(info.get("duration", 0))
        if duration > MAX_DURATION_AUDIO:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, f"❌ Audio duration ({_format_duration(duration)}) exceeds maximum allowed ({MAX_DURATION_AUDIO // 60} minutes).")
            return

        # 2. Check if already in cache, or download
        cache_path = _find_cached_file(video_id, "audio")
        filepath = cache_path
        
        if not filepath:
            with get_download_lock(video_id + "audio"):
                filepath = _find_cached_file(video_id, "audio")
                if not filepath:
                    last_error = None
                    for idx in range(successful_config_index, len(configs)):
                        cfg = configs[idx]
                        tmpdir = tempfile.mkdtemp(prefix="ytms_")
                        try:
                            start_time, end_time = _parse_time_param(video_id)
                            dl_path, dl_info, dl_err = await _download_audio(
                                video_id, tmpdir, duration,
                                start_time=start_time, end_time=end_time,
                                use_cookies=cfg["use_cookies"], custom_proxy=cfg["proxy"]
                            )
                            if dl_err:
                                last_error = dl_err
                                continue
                            if dl_path and os.path.exists(dl_path):
                                os.makedirs(CACHE_DIR, exist_ok=True)
                                actual_ext = os.path.splitext(dl_path)[1].lower()
                                safe_cache_id = _get_cache_id(video_id)
                                saved_cache = os.path.join(CACHE_DIR, f"{safe_cache_id}{actual_ext}")
                                shutil.move(dl_path, saved_cache)
                                filepath = saved_cache
                                lrc_candidate = os.path.join(tmpdir, f"{safe_cache_id}.lrc")
                                if os.path.exists(lrc_candidate):
                                    shutil.move(lrc_candidate, os.path.join(CACHE_DIR, f"{safe_cache_id}.lrc"))
                                if dl_info:
                                    info = dl_info
                                break
                        finally:
                            shutil.rmtree(tmpdir, ignore_errors=True)
                    if not filepath:
                        _react(bot, accid, req_msg_id, "❌")
                        _send(bot, accid, chat_id, f"❌ {last_error or 'Download failed'}")
                        return

        # 3. Save to Navidrome directory
        _react(bot, accid, req_msg_id, "⌛")
        dest_path, dest_lrc, save_err = _save_to_navidrome(filepath, info, music_dir, video_id=video_id)
        if save_err:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, f"❌ Failed to save to Navidrome directory: {save_err}")
            return

        # 4. Trigger Subsonic REST library scan
        scan_ok, scan_msg = _trigger_subsonic_scan(
            nav_url, nav_user, password=nav_password, token=nav_token, salt=nav_salt
        )

        # 5. Format response message
        title = info.get("track") or info.get("title") or video_id
        artist = (
            info.get("artist")
            or info.get("uploader")
            or info.get("channel")
            or info.get("creator")
            or "Unknown Artist"
        )
        album = info.get("album") or "Singles"
        rel_path = os.path.relpath(dest_path, music_dir)
        dur_str = _format_duration(duration) if duration else "?"
        filesize = os.path.getsize(filepath)
        size_str = _format_size(filesize)
        ext_str = os.path.splitext(filepath)[1].lower().replace(".", "").upper()
        lyrics_suffix = " + 📝 Lyrics" if dest_lrc else ""

        caption = (
            f"💾 **Saved to Navidrome library!**\n\n"
            f"🎵 **Title:** {title}\n"
            f"👤 **Artist:** {artist}\n"
            f"💿 **Album:** {album}\n"
            f"📁 **Path:** `{rel_path}` ({dur_str}, {size_str}, {ext_str}{lyrics_suffix})\n"
            f"🔄 **Navidrome Scan:** {scan_msg}\n\n"
            f"🔗 {_make_yt_url(video_id)}"
        )

        _send(bot, accid, chat_id, caption)
        _react(bot, accid, req_msg_id, "☑️")
        database.add_download(chat_id, msg.from_id, video_id, title, duration, "audio_navidrome", filesize)

    except Exception as e:
        logger.error(f"Error in _do_ytms for {video_id}: {e}", exc_info=True)
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ Error saving to Navidrome: {e}")
    finally:
        with _processing_lock:
            _processing.discard(process_key)


def _run_download(bot, accid, msg, video_id: str, download_type: str):
    """Run download in a background thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_download(bot, accid, msg, video_id, download_type))
    finally:
        loop.close()


_anti_spam_warnings: dict[str, float] = {}

def _check_disk_space(bot, accid, msg) -> bool:
    """Returns True if there is enough space, False if blocked. Warns admin if low."""
    usage = shutil.disk_usage(CACHE_DIR)
    free_ratio = usage.free / usage.total
    
    if free_ratio < 0.10:
        _react(bot, accid, msg.id, "❌")
        _send(bot, accid, msg.chat_id, "❌ Download unavailable: server is out of disk space.")
        return False
        
    if free_ratio < 0.20:
        last_warn = getattr(_check_disk_space, "last_warn", 0)
        if time.time() - last_warn > 3600:
            _check_disk_space.last_warn = time.time()
            admin_email = database.get_config("admin_dc_email")
            if admin_email:
                try:
                    admin_chat = bot.rpc.create_chat_by_contact_id(
                        accid, bot.rpc.create_contact(accid, admin_email, "")
                    )
                    _send(bot, accid, admin_chat, f"⚠️ SYSTEM WARNING: Disk space is below 20%! Only {usage.free // (1024**3)} GB left.")
                except Exception as e:
                    bot.logger.error(f"Failed to warn admin about disk space: {e}")
    return True


_processing = set()
_processing_lock = threading.Lock()

async def _do_download(bot, accid, msg, video_id: str, download_type: str):
    """Actual download + send logic."""
    chat_id = msg.chat_id
    req_msg_id = msg.id

    # 0. Resolve Yandex preview URL if target is a Yandex preview link
    if YANDEX_PREVIEW_RE.search(video_id):
        resolved = _resolve_yandex_preview(video_id)
        if resolved:
            video_id = resolved
        else:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, "❌ Could not extract video link from Yandex preview.")
            return

    # 0.5. If it's an audio-only platform, force audio download type
    if download_type == "video" and AUDIO_ONLY_URL_RE.search(video_id):
        download_type = "audio"

    logger.info(f"Starting _do_download for {video_id} (type={download_type}) in chat {chat_id}")
    
    process_key = f"{chat_id}_{video_id}_{download_type}"
    with _processing_lock:
        if process_key in _processing:
            # Silently debounce duplicate concurrent requests
            return
        _processing.add(process_key)
        
    try:
        # 1. Anti-spam check (per chat)
        last_sent = database.get_last_download(chat_id, video_id, download_type)
        if time.time() - last_sent < ANTI_SPAM_SECONDS:
            _react(bot, accid, req_msg_id, "ℹ️")
            
            warning_key = f"{chat_id}_{video_id}_{download_type}"
            last_warning = _anti_spam_warnings.get(warning_key, 0)
            if time.time() - last_warning > 10:
                _anti_spam_warnings[warning_key] = time.time()
                _send(bot, accid, chat_id, "ℹ️ This video was already sent to this chat recently. Scroll up! 👆")
            return


        # 1.5 Disk space check
        if not _check_disk_space(bot, accid, msg):
            return
    
        # 2. Check cache first
        cache_path = _find_cached_file(video_id, download_type)
        if cache_path:
            os.utime(cache_path, None)
            await _send_from_cache(bot, accid, msg, video_id, download_type, cache_path)
            return
    
        # 3. Fetch info to know duration for audio strategy
        configs = _get_fallback_configs()
        info, error, successful_config_index = await _fetch_video_info_with_fallback(video_id)

        if not info:
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, f"❌ Could not fetch video info: {error or 'Unknown error'}")
            return
        
        duration = int(info.get("duration", 0))
    
        # 4. Wait for lock if already downloading same ID
        with get_download_lock(video_id + download_type):
            cache_path = _find_cached_file(video_id, download_type)
            if cache_path:
                # Re-check anti-spam inside the lock for the current chat
                # This prevents duplicate sends if the user double-tapped the download link
                last_sent_after_lock = database.get_last_download(chat_id, video_id, download_type)
                if time.time() - last_sent_after_lock < ANTI_SPAM_SECONDS:
                    warning_key = f"{chat_id}_{video_id}_{download_type}"
                    if time.time() - _anti_spam_warnings.get(warning_key, 0) > 10:
                        _anti_spam_warnings[warning_key] = time.time()
                        _send(bot, accid, chat_id, "ℹ️ This video was already sent to this chat recently. Scroll up! 👆")
                    return
    
                await _send_from_cache(bot, accid, msg, video_id, download_type, cache_path, info)
                return
    
            # ⏳ React: downloading
            _react(bot, accid, req_msg_id, "⏳")
    
            last_error = None
            for idx in range(successful_config_index, len(configs)):
                cfg = configs[idx]
                tmpdir = tempfile.mkdtemp(prefix="ytbot_")
                try:
                    start_time, end_time = _parse_time_param(video_id)
                    if start_time and start_time > 7200:
                        _react(bot, accid, req_msg_id, "❌")
                        _send(bot, accid, chat_id, "❌ Start time parameter is too large (maximum is 2 hours)")
                        shutil.rmtree(tmpdir, ignore_errors=True)
                        return
                    if end_time and end_time > 7200:
                        _react(bot, accid, req_msg_id, "❌")
                        _send(bot, accid, chat_id, "❌ End time parameter is too large (maximum is 2 hours)")
                        shutil.rmtree(tmpdir, ignore_errors=True)
                        return
                    if start_time and end_time and end_time <= start_time:
                        _react(bot, accid, req_msg_id, "❌")
                        _send(bot, accid, chat_id, "❌ End time must be after start time")
                        shutil.rmtree(tmpdir, ignore_errors=True)
                        return

                    if start_time:
                        if end_time:
                            effective_duration = max(0, end_time - start_time)
                        else:
                            effective_duration = max(0, duration - start_time)
                    elif end_time:
                        effective_duration = max(0, end_time)
                    else:
                        effective_duration = duration

                    if download_type == "video":
                        initial_height = 360 if effective_duration > 600 else 480
                        heights_to_try = [480, 360, 240, 144] if initial_height == 480 else [360, 240, 144]
                        filepath = None
                        info = None
                        error = None
                        for h in heights_to_try:
                            filepath, info, error = await _download_video(
                                video_id, tmpdir, max_height=h, 
                                start_time=start_time, end_time=end_time, 
                                use_cookies=cfg["use_cookies"], custom_proxy=cfg["proxy"]
                            )
                            if filepath and os.path.exists(filepath):
                                break
                            
                            is_size_or_format_err = error and any(term in error.lower() for term in [f"{MAX_FILESIZE_MB} mb", "30 mb", "filtered", "not available", "format"])
                            if is_size_or_format_err:
                                logger.info(f"Retrying {video_id} with lower resolution than {h}p because of size/format limit ({error})...")
                                continue
                            else:
                                break
                        
                        if error and any(term in error.lower() for term in [f"{MAX_FILESIZE_MB} mb", "30 mb", "filtered", "not available", "format"]):
                            short_id = video_id
                            if video_id.startswith("http://") or video_id.startswith("https://"):
                                m = YT_URL_RE.search(video_id)
                                short_id = m.group(1) if m else video_id
                            error = f"📦 Video exceeds {MAX_FILESIZE_MB} MB size limit even at lower resolutions. Try audio instead: /ytm_{short_id}"
                    else:
                        filepath, info, error = await _download_audio(
                            video_id, tmpdir, duration, 
                            start_time=start_time, end_time=end_time, 
                            use_cookies=cfg["use_cookies"], custom_proxy=cfg["proxy"]
                        )
    
                    if error:
                        last_error = error
                        logger.info(f"Download attempt using {cfg['desc']} failed for {video_id}: {error}.")
                        if idx < len(configs) - 1:
                            logger.info("Retrying with next configuration...")
                            continue
                        _react(bot, accid, req_msg_id, "❌")
                        _send(bot, accid, chat_id, f"❌ {error}")
                        return
    
                    if not filepath or not os.path.exists(filepath):
                        last_error = "Download failed: file not found"
                        logger.info(f"Download file check using {cfg['desc']} failed for {video_id}.")
                        if idx < len(configs) - 1:
                            logger.info("Retrying with next configuration...")
                            continue
                        _react(bot, accid, req_msg_id, "❌")
                        _send(bot, accid, chat_id, f"❌ {last_error}")
                        return
    
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    actual_ext = os.path.splitext(filepath)[1].lower()
                    safe_cache_id = _get_cache_id(video_id)
                    cache_path = os.path.join(CACHE_DIR, f"{safe_cache_id}{actual_ext}")
                    shutil.move(filepath, cache_path)
                    
                    lrc_candidate = os.path.join(tmpdir, f"{safe_cache_id}.lrc")
                    if os.path.exists(lrc_candidate):
                        shutil.move(lrc_candidate, os.path.join(CACHE_DIR, f"{safe_cache_id}.lrc"))
                    
                    await _send_from_cache(bot, accid, msg, video_id, download_type, cache_path, info)
                    return
    
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
    
            _react(bot, accid, req_msg_id, "❌")
            _send(bot, accid, chat_id, f"❌ {last_error or 'Download failed after retry'}")
            
    finally:
        with _processing_lock:
            _processing.discard(process_key)


async def _send_from_cache(bot, accid, msg, video_id, download_type, filepath, info=None):
    """Send a file from the cache to the chat."""
    chat_id = msg.chat_id
    req_msg_id = msg.id
    
    _react(bot, accid, req_msg_id, "⌛")

    if not info:
        info, _, _ = await _fetch_video_info_with_fallback(video_id)

    title = (info or {}).get("title", video_id)
    total_duration = (info or {}).get("duration", 0)
    duration = total_duration
    full_url = _extract_video_id(video_id) or video_id
    start_time, end_time = _parse_time_param(full_url)
    if duration:
        if start_time:
            if end_time:
                duration = max(0, end_time - start_time)
            else:
                duration = max(0, duration - start_time)
        elif end_time:
            duration = max(0, end_time)
    filesize = os.path.getsize(filepath)
    dur_str = _format_duration(int(duration)) if duration else "?"
    size_str = _format_size(filesize)

    ext = os.path.splitext(filepath)[1].lower().replace(".", "").upper()
    clean_base = _get_base_video_id(video_id)
    if download_type == "video":
        chunk_s = start_time or 0
        chunk_e = end_time if end_time else (chunk_s + int(duration or 0))
        
        range_suffix = ""
        if start_time is not None or end_time is not None or (total_duration and total_duration > 600):
            range_str = _format_time_range(chunk_s, chunk_e)
            range_suffix = f" [{range_str}]"
            
        caption = f"📺 {title}{range_suffix} ({dur_str}, {size_str}, {ext})\n\n🔗 {_make_yt_url(clean_base)}"
        
        # Check if there is a next chunk to offer
        if total_duration and total_duration > chunk_e:
            next_s = chunk_e
            next_e = min(total_duration, next_s + 600)
            next_range_str = _format_time_range(next_s, next_e)
            
            clean_base = _get_base_video_id(video_id)
            if clean_base.startswith("http://") or clean_base.startswith("https://"):
                short_id = _get_cache_id(clean_base)
                database.add_url_mapping(short_id, clean_base)
            else:
                short_id = clean_base
                
            next_cmd = f"/yt_{short_id}_{next_s}_{next_e}"
            caption += f"\n\n▶️ Next chunk ({next_range_str}): {next_cmd}"
    else:
        caption = f"🎵 {title} ({dur_str}, {size_str}, {ext})\n\n🔗 {_make_yt_url(video_id)}"

    _send(bot, accid, chat_id, caption, file=filepath)

    logger.info(f"Successfully sent {download_type} '{title}' (duration={dur_str}, size={size_str}, format={ext}) to chat {chat_id}")

    _react(bot, accid, req_msg_id, "☑️")

    database.add_download(chat_id, msg.from_id, video_id, title, int(duration or 0), download_type, filesize)


def _handle_download_command(bot, accid, event, download_type: str, payload: str):
    """Common handler for /yt and /ytm commands."""
    msg = event.msg
    
    logger.info(f"Received download command /{download_type == 'video' and 'yt' or 'ytm'} (payload='{payload}') in chat {msg.chat_id} from {msg.from_id}")
    
    if _is_duplicate_msg(msg.id, "cmd"):
        return
        
    video_id = None
    
    # 1. Try to extract from stripped payload (removing /yt or /ytm command prefix)
    cmd_prefix = "/ytm" if download_type == "audio" else "/yt"
    stripped_payload = payload
    if payload.startswith(cmd_prefix):
        stripped_payload = payload[len(cmd_prefix):]
        if stripped_payload.startswith("_"):
            stripped_payload = stripped_payload[1:]
        stripped_payload = stripped_payload.strip()
        
    if stripped_payload:
        video_id = _extract_video_id(stripped_payload)
        
    # 2. Check quote reply if no video ID was found in the direct payload
    if not video_id:
        quote = getattr(msg, "quote", None) or (msg.get("quote") if isinstance(msg, dict) else None)
        if quote:
            quoted_text = ""
            if isinstance(quote, dict):
                quoted_text = quote.get("text", "")
            else:
                quoted_text = getattr(quote, "text", "")
                
            if quoted_text:
                video_id = _extract_video_id(quoted_text)

    if not video_id:
        _send(bot, accid, msg.chat_id,
              f"Usage: /{download_type == 'video' and 'yt' or 'ytm'} <youtube_url_or_video_id>")
        return

    if _is_rate_limited(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, f"⏱ Please wait {RATE_LIMIT_SECONDS}s between downloads.")
        return

    t = threading.Thread(target=_run_download, args=(bot, accid, msg, video_id, download_type), daemon=True)
    t.start()


# ── Delta Chat command handlers ──

@dc_cli.on(events.NewMessage(command="/yt", is_bot=None))
def yt_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    _handle_download_command(bot, accid, event, "video", event.msg.text)


@dc_cli.on(events.NewMessage(command="/ytm", is_bot=None))
def ytm_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    _handle_download_command(bot, accid, event, "audio", event.msg.text)


@dc_cli.on(events.NewMessage(command="/ytms", is_bot=None))
def ytms_command(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    if accid != dc_accid:
        return
    msg = event.msg
    if _is_duplicate_msg(msg.id, "cmd"):
        return

    logger.info(f"Received /ytms command in chat {msg.chat_id} from {msg.from_id}")

    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /ytms.")
        return

    payload = (msg.text or "").strip()
    video_id = None
    stripped_payload = payload
    if payload.startswith("/ytms"):
        stripped_payload = payload[len("/ytms"):]
        if stripped_payload.startswith("_"):
            stripped_payload = stripped_payload[1:]
        stripped_payload = stripped_payload.strip()

    if stripped_payload:
        video_id = _extract_video_id(stripped_payload)

    if not video_id:
        quote = getattr(msg, "quote", None) or (msg.get("quote") if isinstance(msg, dict) else None)
        if quote:
            quoted_text = ""
            if isinstance(quote, dict):
                quoted_text = quote.get("text", "")
            else:
                quoted_text = getattr(quote, "text", "")
            if quoted_text:
                video_id = _extract_video_id(quoted_text)

    if not video_id:
        _send(bot, accid, msg.chat_id, "Usage: /ytms <youtube_url_or_video_id>")
        return

    t = threading.Thread(target=_run_ytms, args=(bot, accid, msg, video_id), daemon=True)
    t.start()


@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    logger.info(f"Received /help command in chat {msg.chat_id} from {msg.from_id}")
    help_text = _get_help_text(bot, accid, msg.from_id)
    _send(bot, accid, msg.chat_id, help_text)


@dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    """Show configured transports (mail relays) and their status."""
    msg = event.msg
    logger.info(f"Received /transports command in chat {msg.chat_id} from {msg.from_id}")
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /transports.")
        return

    try:
        transports = bot.rpc.list_transports(accid)
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to list transports: {e}")
        return

    if not transports:
        _send(bot, accid, msg.chat_id, "No transports configured.")
        return

    # Get connectivity status
    connectivity_label = "❓ Unknown"
    try:
        connectivity = bot.rpc.get_connectivity(accid)
        if connectivity >= 4000:
            connectivity_label = "🟢 Connected"
        elif connectivity >= 3000:
            connectivity_label = "🔄 Working"
        elif connectivity >= 2000:
            connectivity_label = "🟡 Connecting"
        else:
            connectivity_label = "🔴 Not connected"
    except Exception:
        pass

    # Get connectivity HTML to parse per-transport status
    connectivity_html = ""
    try:
        connectivity_html = bot.rpc.get_connectivity_html(accid)
    except Exception:
        pass

    # Get resilient sending mode status
    resilient_on = False
    try:
        resilient_on = database.get_config("resilient") == "1"
    except Exception:
        pass

    # Get per-transport statistics
    stats_map = {}
    for s in database.get_all_transport_stats():
        stats_map[s['addr']] = s

    active_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
    transport_addrs = []
    for t in transports:
        addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
        transport_addrs.append(addr)

    reply = f"🔌 **Mail Relays (Transports)**\n\nStatus: {connectivity_label}\n\n"

    import re
    for addr in transport_addrs:
        # Determine status label from HTML
        status_label = "❓ Unknown"
        if connectivity_html:
            domain = addr.split('@')[-1] if '@' in addr else addr
            pattern = rf'class="([^"]+)\s+dot".*?<b>{re.escape(domain)}:</b>\s*([^<]+)'
            match = re.search(pattern, connectivity_html, re.IGNORECASE)
            if match:
                color = match.group(1).lower()
                status_text = match.group(2).strip().lower()
                if "yellow" in color or "connecting" in status_text:
                    status_label = "🟡 Connecting"
                elif "green" in color:
                    status_label = "🔄 Working"
                elif "red" in color or "lost" in status_text or "error" in status_text:
                    status_label = "🔴 Not connected"

        is_used = resilient_on or (addr == active_addr)
        used_str = " ✔︎ Used for sending:" if is_used else ":"
        reply += f"**{status_label}**{used_str} `{addr}`\n"

        stats = stats_map.get(addr)
        if stats:
            reply += f"  📤 Sent: {stats['msgs_sent']}  📥 Received: {stats['msgs_received']}\n"
            if stats.get('last_sent_at'):
                import datetime
                last_sent = datetime.datetime.fromtimestamp(stats['last_sent_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last sent: {last_sent}\n"
            if stats.get('last_received_at'):
                import datetime
                last_recv = datetime.datetime.fromtimestamp(stats['last_received_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last received: {last_recv}\n"
        else:
            reply += f"  📤 Sent: 0  📥 Received: 0\n"
        reply += "\n"

    reply += f"Total transports: {len(transport_addrs)}"
    _send(bot, accid, msg.chat_id, reply)

@dc_cli.on(events.NewMessage(command="/addtransport"))
def addtransport_command(bot, accid, event):
    """Add a backup mail relay (transport). Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /addtransport.")
        return

    payload = event.payload.strip() if event.payload else ""
    if not payload:
        _send(bot, accid, msg.chat_id, 
            "Usage:\n"
            "/addtransport DCACCOUNT:server.example\n"
            "/addtransport user@example.com password123"
        )
        return

    try:
        if payload.startswith("DCACCOUNT:"):
            bot.rpc.add_transport_from_qr(accid, payload)
            _send(bot, accid, msg.chat_id, "✅ Backup transport added via chatmail URI.")
        else:
            parts = payload.split(None, 1)
            if len(parts) < 2:
                _send(bot, accid, msg.chat_id, 
                    "❌ For email accounts, provide both address and password:\n"
                    "/addtransport user@example.com password123"
                )
                return
            addr, password = parts[0], parts[1]
            bot.rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
            _send(bot, accid, msg.chat_id, f"✅ Backup transport `{addr}` added.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to add transport: {e}")

@dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    """Set a specific transport as primary. Admin only."""
    msg = event.msg
    logger.info(f"Received /setprimary command (payload='{event.payload}') in chat {msg.chat_id} from {msg.from_id}")
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /setprimary.")
        return

    addr = event.payload.strip() if event.payload else ""
    if not addr:
        _send(bot, accid, msg.chat_id, "Usage: /setprimary user@example.com")
        return

    try:
        bot.rpc.set_config(accid, "configured_addr", addr)
        _send(bot, accid, msg.chat_id, f"✅ Primary address (`configured_addr`) is now `{addr}`.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to set primary address: {e}")

@dc_cli.on(events.NewMessage(command="/resilient"))
def resilient_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /resilient.")
        return

    arg = event.payload.strip().lower() if event.payload else ""

    try:
        current = database.get_config("resilient") == "1"
        if not arg:
            status = "enabled" if current else "disabled"
            _send(bot, accid, msg.chat_id, f"ℹ️ Resilient sending mode is currently {status}.")
            return

        if arg in ("on", "1", "true"):
            database.set_config("resilient", "1")
            _send(bot, accid, msg.chat_id, "✅ Resilient sending mode enabled. Each outgoing message will be sent via all connected transports.")
        elif arg in ("off", "0", "false"):
            database.set_config("resilient", "0")
            _send(bot, accid, msg.chat_id, "❌ Resilient sending mode disabled.")
        else:
            _send(bot, accid, msg.chat_id, "❌ Invalid argument. Use '/resilient on', '/resilient off', or '/resilient' to get status.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to update resilient mode: {e}")

@dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    """Remove a mail relay (transport). Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _send(bot, accid, msg.chat_id, "❌ Only the bot administrator can use /rmtransport.")
        return

    addr = event.payload.strip() if event.payload else ""
    if not addr:
        _send(bot, accid, msg.chat_id, "Usage: /rmtransport user@example.com")
        return

    try:
        transports = bot.rpc.list_transports(accid)
        transport_addrs = []
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            transport_addrs.append(a)
        if len(transport_addrs) <= 1:
            _send(bot, accid, msg.chat_id, "❌ Cannot remove the last transport.")
            return
        if addr not in transport_addrs:
            _send(bot, accid, msg.chat_id, f"❌ Transport `{addr}` not found.")
            return
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to check transports: {e}")
        return

    try:
        bot.rpc.delete_transport(accid, addr)
        _send(bot, accid, msg.chat_id, f"✅ Transport `{addr}` removed.")
    except Exception as e:
        _send(bot, accid, msg.chat_id, f"❌ Failed to remove transport: {e}")


@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    s = database.get_stats()
    videos = s["by_type"].get("video", 0)
    audios = s["by_type"].get("audio", 0)
    os.makedirs(CACHE_DIR, exist_ok=True)
    usage = shutil.disk_usage(CACHE_DIR if os.path.exists(CACHE_DIR) else ".")
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    free_pct = (usage.free / usage.total) * 100

    is_admin = _is_dc_admin(bot, accid, event.msg.from_id)
    
    # Log for debugging
    addr = "unknown"
    try:
        c = bot.rpc.get_contact(accid, event.msg.from_id)
        addr = c.address
    except: pass
    
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    logger.info(f"Stats requested by {addr} (id={event.msg.from_id}), is_admin={is_admin} [AdminConfig: email={admin_email}, fp={admin_fp}]")

    reply = (
        f"📊 **YT Bot Statistics**\n\n"
        f"Total downloads: {s['total']} ({videos} video, {audios} audio)\n"
        f"Last 24h: {s['last_24h']}\n"
        f"Total data: {_format_size(s['total_size'])}\n"
    )

    if is_admin:
        yt_ok, yt_msg = _check_youtube_status()
        yt_icon = "🟢" if yt_ok else "🔴" if ("expired" in yt_msg.lower() or "error" in yt_msg.lower() or "flagged" in yt_msg.lower() or "failed" in yt_msg.lower() or "blocked" in yt_msg.lower() or "captcha" in yt_msg.lower()) else "⚪"

        ym_ok, ym_msg = _check_yandex_status()
        ym_icon = "🟢" if ym_ok else "🟡" if "inactive" in ym_msg.lower() else "🔴" if ("expired" in ym_msg.lower() or "error" in ym_msg.lower() or "failed" in ym_msg.lower() or "blocked" in ym_msg.lower() or "captcha" in ym_msg.lower()) else "⚪"

        nav_ok, nav_msg = _check_navidrome_status()
        nav_icon = "🟢" if nav_ok else "🔴" if ("error" in nav_msg.lower() or "failed" in nav_msg.lower()) else "⚪"
        reply += (
            f"\n💾 **Disk Space (Admin only)**\n"
            f"Free: {free_gb:.1f} GB of {total_gb:.1f} GB ({free_pct:.1f}%)\n"
            f"\n▶️ **YouTube:** {yt_icon} {yt_msg}\n"
            f"🎵 **Yandex Music:** {ym_icon} {ym_msg}\n"
            f"📻 **Navidrome:** {nav_icon} {nav_msg}\n"
        )
    _send(bot, accid, event.msg.chat_id, reply)


@dc_cli.on(events.NewMessage(command="/donate"))
def donate_command(bot, accid, event):
    msg = event.msg
    _send(bot, accid, msg.chat_id,
          "❤️ Support Bot Development\n\n"
          "If you find this bot useful, you can support its development:\n\n"
          "☕️ Ko-fi: https://ko-fi.com/gluek (🌍 world cards, paypal)\n"
          "🚀 Tribute: https://web.tribute.tg/d/IWb (🇷🇺 russian cards, SBP)\n\n"
          "Thank you! 🙏")


@dc_cli.on(events.NewMessage(command="/initadmin"))
def initadmin_command(bot, accid, event):
    msg = event.msg
    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()

    if admin_email or admin_fp:
        _send(bot, accid, msg.chat_id, "❌ Admin is already set. Use `set_admin.py` on the server to change.")
        return

    contact = bot.rpc.get_contact(accid, msg.from_id)
    email = contact.address
    database.set_config("admin_dc_email", email)

    fp = _get_contact_fingerprint(bot, accid, msg.from_id, contact=contact)
    if fp:
        first_fp = fp.split(',')[0]
        database.set_admin_fingerprint(first_fp)
        _send(bot, accid, msg.chat_id,
              f"✅ You are now the admin!\n\nEmail: `{email}`\nFingerprint: `{first_fp[-8:]}`")
    else:
        _send(bot, accid, msg.chat_id,
              f"✅ You are now the admin!\n\nEmail: `{email}`\n⚠️ Fingerprint not available yet (will be used after key exchange).")


def _get_help_text(bot, accid, from_id):
    contact = bot.rpc.get_contact(accid, from_id)
    sender_email = contact.address

    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"🤖 **YouTube Bot v{VERSION}**\n"
        f"I download YouTube videos and audio.\n\n"
        f"**Commands:**\n"
        f"/yt <url> — Download video (MP4 360-480p, ≤{MAX_FILESIZE_MB}MB)\n"
        f"/yt_<video_id> — Download video by ID\n"
        f"/ytm <url> — Download audio (Opus 128kbps stereo < 10 min, 64kbps mono >= 10 min, ≤{MAX_FILESIZE_MB}MB)\n"
        f"/ytm_<video_id> — Download audio by ID\n"
        f"/stats — Download statistics\n"
        f"/donate — Support development ❤️\n"
        f"/help — This message\n\n"
        f"💡 _You can also just paste a YouTube link and I'll show you download options._\n\n"
        f"⏱ Max duration: video {MAX_DURATION_VIDEO // 60}m, audio {MAX_DURATION_AUDIO // 60}m | Max file: {MAX_FILESIZE_MB} MB\n"
    )

    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    is_actually_admin = _is_dc_admin(bot, accid, from_id)
    
    if not admin_email:
        help_text += "\n/initadmin — Claim bot ownership\n"
    elif is_actually_admin:
        fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
        help_text += f"\n👑 **Admin:** `{admin_email}`{fp_suffix}\n"
        nav_ok, nav_msg = _check_navidrome_status()
        nav_icon = "🟢" if nav_ok else "🔴" if ("error" in nav_msg.lower() or "failed" in nav_msg.lower()) else "⚪"
        help_text += f"📻 **Navidrome:** {nav_icon} `{nav_msg}`\n"
        help_text += "\n**Admin Commands:**\n"
        help_text += "/ytms <url> — Save audio to Navidrome library\n"
        help_text += "/ytms_<video_id> — Save audio to Navidrome by ID\n"
        help_text += "/transports — Show configured mail relays & stats\n"
        help_text += "/addtransport — Add a backup mail relay\n"
        help_text += "/rmtransport <addr> — Remove a mail relay\n"
        help_text += "/setprimary <addr> — Switch the primary mail relay\n"
        help_text += "/resilient — Toggle resilient sending mode (all relays)\n"

    return help_text


# ── YouTube link auto-detection and /yt_ID, /ytm_ID handlers ──

def _handle_yandex_preview(bot, accid, msg, yandex_url: str):
    """Resolve Yandex preview URL and pass to standard info handler."""
    resolved = _resolve_yandex_preview(yandex_url)
    if resolved:
        _handle_link_info(bot, accid, msg, resolved)
    else:
        _react(bot, accid, msg.id, "❌")
        _send(bot, accid, msg.chat_id, "❌ Could not extract video link from Yandex preview.")


@dc_cli.on(events.NewMessage(is_bot=None))
def on_new_message(bot, accid, event):
    if _is_bot_blocked(bot, accid, event.msg):
        return
    msg = event.msg
    
    if _is_duplicate_msg(msg.id, "text"):
        return
        
    # 0. Safety checks: ignore info msgs, wrong account, or outbound msgs
    if msg.is_info or accid != dc_accid:
        return

    # Check if outbound using standard self contact ID
    if msg.from_id == DC_CONTACT_ID_SELF:
        return

    # Track receiving stats
    try:
        addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if addr:
            database.increment_transport_received(addr)
    except Exception:
        pass

    text = (msg.text or "").strip()
    if not text:
        return

    # 1. Ignore ALL commands (they are handled by @dc_cli.on(command=...))
    if text.startswith('/'):
        return

    logger.debug(f"Processing potential link in message {msg.id}: {text[:50]}...")
    
    # 2. Auto-detect YouTube links and respond with info
    yt_match = YT_URL_RE.search(text)
    if yt_match:
        if 't=' in text or 'start=' in text:
            url_match = re.search(r'https?://[^\s]+', text)
            video_id = url_match.group(0) if url_match else yt_match.group(0)
        else:
            video_id = yt_match.group(1)
        logger.info(f"Auto-detected YouTube link in chat {msg.chat_id} from {msg.from_id}: {video_id}")
        _react(bot, accid, msg.id, "🤖")
        t = threading.Thread(target=_handle_link_info, args=(bot, accid, msg, video_id), daemon=True)
        t.start()
        return

    # 2.6. Auto-detect Yandex Video Preview links
    yandex_match = YANDEX_PREVIEW_RE.search(text)
    if yandex_match:
        yandex_url = yandex_match.group(0)
        logger.info(f"Auto-detected Yandex preview link in chat {msg.chat_id} from {msg.from_id}: {yandex_url}")
        _react(bot, accid, msg.id, "🤖")
        t = threading.Thread(target=_handle_yandex_preview, args=(bot, accid, msg, yandex_url), daemon=True)
        t.start()
        return

    # 2.5. Auto-detect other supported links (Vimeo, Twitter, Insta, PeerTube, etc.)
    supported_match = SUPPORTED_URL_RE.search(text)
    if supported_match:
        video_id = supported_match.group(0) # Full URL
        logger.info(f"Auto-detected supported link in chat {msg.chat_id} from {msg.from_id}: {video_id}")
        _react(bot, accid, msg.id, "🤖")
        t = threading.Thread(target=_handle_link_info, args=(bot, accid, msg, video_id), daemon=True)
        t.start()
        return

    # 3. Welcome new users in private chats
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        is_private = False
        if isinstance(chat_info, dict):
            is_private = (chat_info.get("type") == 1)
        else:
            is_private = (getattr(chat_info, "type", 1) == 1)

        if is_private:
            greeted_key = f"greeted_{msg.from_id}"
            if not database.get_config(greeted_key):
                help_text = _get_help_text(bot, accid, msg.from_id)
                _send(bot, accid, msg.chat_id, help_text)
                database.set_config(greeted_key, "1")
    except Exception as e:
        logger.error(f"Greeting check error: {e}")


def _handle_link_info(bot, accid, msg, video_id: str):
    """Fetch video info and reply with download commands (with caching)."""
    # 1. Check Cache
    cached = database.get_cached_info(video_id)
    if cached:
        info_json, cached_thumb = cached
        try:
            info = json.loads(info_json)
            # Check if thumb still exists
            thumb_path = cached_thumb if cached_thumb and os.path.exists(cached_thumb) else None
            _display_link_info(bot, accid, msg, video_id, info, thumb_path)
            return
        except Exception as e:
            logger.error(f"Failed to load cached info for {video_id}: {e}")

    # 2. Fetch fresh info
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        info, error, _ = loop.run_until_complete(_fetch_video_info_with_fallback(video_id))
    finally:
        loop.close()

    if not info:
        if error and ("This video is not available" in error or "Private video" in error):
             _send(bot, accid, msg.chat_id, f"❌ {error}")
        return

    # 3. Handle thumbnail (persist it)
    thumb_path = None
    thumbnail_url = info.get("thumbnail")
    if thumbnail_url:
        try:
            safe_id = _get_cache_id(video_id)
            persist_thumb = os.path.join(THUMB_CACHE_DIR, f"{safe_id}.jpg")
            urllib.request.urlretrieve(thumbnail_url, persist_thumb)
            thumb_path = persist_thumb
        except Exception as e:
            logger.error(f"Failed to download thumbnail: {e}")

    # 4. Save to Cache
    try:
        database.set_cached_info(video_id, json.dumps(info), thumb_path or "")
    except Exception as e:
        logger.error(f"Failed to cache info for {video_id}: {e}")

    # 5. Display
    _display_link_info(bot, accid, msg, video_id, info, thumb_path)


def _display_link_info(bot, accid, msg, video_id: str, info: dict, thumb_path: str | None):
    """Helper to format and send the link info message."""
    title = info.get("title", "Unknown")
    original_duration = info.get("duration", 0)
    duration = original_duration
    start_time, end_time = _parse_time_param(video_id)
    if duration:
        if start_time:
            if end_time:
                duration = max(0, end_time - start_time)
            else:
                duration = max(0, duration - start_time)
        elif end_time:
            duration = max(0, end_time)
        
    dur_str = _format_duration(int(duration)) if duration else "?"

    audio_fmt = "Opus"
    
    # Size estimation
    target_height = 360 if duration > 600 else 480
    video_size_str = "?? MB"
    audio_size_str = "?? MB"
    if duration:
        # Audio format and size estimation
        if duration <= 600:
            # We prefer opus (for YouTube), then m4a (AAC) if available, otherwise check first available format extension
            has_opus = False
            has_m4a = False
            has_mp3 = False
            for f in info.get('formats', []):
                if f.get('vcodec') == 'none':
                    acodec = f.get('acodec') or ''
                    ext = f.get('ext') or ''
                    if 'opus' in acodec:
                        has_opus = True
                    elif 'm4a' in ext or 'aac' in ext:
                        has_m4a = True
                    elif 'mp3' in ext:
                        has_mp3 = True
            
            if has_opus:
                audio_fmt = "Opus"
            elif has_m4a:
                audio_fmt = "M4A"
            elif has_mp3:
                audio_fmt = "MP3"
            else:
                audio_fmt = "Audio"

            # Look for the format we will download: best opus, best m4a, otherwise best audio
            best_opus_f = None
            best_m4a_f = None
            best_any_f = None
            for f in info.get('formats', []):
                if f.get('vcodec') == 'none':
                    acodec = f.get('acodec') or ''
                    ext = f.get('ext') or ''
                    abr = f.get('abr') or 128
                    
                    if 'opus' in acodec:
                        if not best_opus_f or abr > (best_opus_f.get('abr') or 0):
                            best_opus_f = f
                    elif 'm4a' in ext or 'aac' in ext:
                        if not best_m4a_f or abr > (best_m4a_f.get('abr') or 0):
                            best_m4a_f = f
                    if not best_any_f or abr > (best_any_f.get('abr') or 0):
                        best_any_f = f
            
            target_f = best_opus_f or best_m4a_f or best_any_f
            if target_f:
                fs = target_f.get('filesize') or target_f.get('filesize_approx')
                if fs:
                    if start_time and original_duration:
                        fs = fs * (duration / original_duration)
                    audio_mb = fs / 1048576
                else:
                    abr = target_f.get('abr') or 128
                    audio_mb = (duration * abr) / 8192
            else:
                audio_mb = (duration * 128) / 8192
        else:
            # For > 10m, we transcode to Opus 64k mono
            audio_fmt = "Opus"
            audio_mb = (duration * 64) / 8192
            
        audio_size_str = f"~{audio_mb:.1f} MB"
        
        # Video estimation
        video_mb = 0
        for f in info.get('formats', []):
            if f.get('height') == target_height and f.get('vcodec') != 'none':
                fs = f.get('filesize') or f.get('filesize_approx')
                if fs:
                    if start_time and original_duration:
                        fs = fs * (duration / original_duration)
                    video_mb = fs / 1048576
                    break
        
        if not video_mb:
            # 480p ~0.06 MB/s, 360p ~0.035 MB/s
            rate = 0.035 if target_height == 360 else 0.06
            video_mb = duration * rate
            
        video_size_str = f"~{min(video_mb, float(MAX_FILESIZE_MB)):.1f} MB"

    video_url = _make_yt_url(video_id)

    if video_id.startswith("http://") or video_id.startswith("https://"):
        short_id = _get_cache_id(video_id)
        database.add_url_mapping(short_id, video_id)
    else:
        short_id = video_id

    aud_cmd = f"/ytm_{short_id}"

    if original_duration > 600:
        chunk_s = start_time or 0
        chunk_e = min(original_duration, chunk_s + 600)
        part_num = (chunk_s // 600) + 1
        range_label = _format_time_range(chunk_s, chunk_e)
        vid_cmd = f"/yt_{short_id}_{chunk_s}_{chunk_e}"
        part_prefix = f"Part {part_num} " if part_num > 1 or original_duration > 600 else ""
        video_btn = f"📼 {part_prefix}({range_label}) {vid_cmd}"
    else:
        vid_cmd = f"/yt_{short_id}"
        can_video = duration <= MAX_DURATION_VIDEO
        video_btn = f"📼 {target_height}p ({video_size_str}) {vid_cmd}" if can_video else f"📼 Too long (> {MAX_DURATION_VIDEO // 60}m)"

    can_audio = duration <= MAX_DURATION_AUDIO
    audio_btn = f"💿 {audio_fmt} ({audio_size_str}) {aud_cmd}" if can_audio else f"💿 Too long (> {MAX_DURATION_AUDIO // 60}m)"

    is_audio_only = bool(AUDIO_ONLY_URL_RE.search(video_url))
    is_admin = _is_dc_admin(bot, accid, msg.from_id)
    nav_btn = f"💾 /ytms_{short_id}" if is_admin and can_audio else ""

    if is_audio_only:
        btn_line = f"{audio_btn}   {nav_btn}" if nav_btn else audio_btn
        lines = [
            f"🎵 [Audio: \"{title}\" ({dur_str})]({video_url})",
            "",
            btn_line
        ]
    else:
        btn_line = f"{video_btn}   {audio_btn}"
        if nav_btn:
            btn_line += f"   {nav_btn}"
        lines = [
            f"📺 [Video: \"{title}\" ({dur_str})]({video_url})",
            "",
            btn_line
        ]

    _send(bot, accid, msg.chat_id, "\n".join(lines), file=thumb_path)


async def _cache_cleaner_loop():
    """Background task to keep cache within limits (2GB, 24h)."""
    while True:
        try:
            if not os.path.exists(CACHE_DIR):
                await asyncio.sleep(3600)
                continue

            now = time.time()
            files = []
            total_size = 0

            for f in os.listdir(CACHE_DIR):
                path = os.path.join(CACHE_DIR, f)
                if not os.path.isfile(path):
                    continue
                
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
                
                if now - mtime > CACHE_MAX_AGE:
                    logger.info(f"Removing old cache file: {f}")
                    os.remove(path)
                    continue
                
                files.append((path, mtime, size))
                total_size += size

            # If still over size limit, remove oldest files
            if total_size > CACHE_MAX_SIZE:
                # Sort by mtime (oldest first)
                files.sort(key=lambda x: x[1])
                for path, mtime, size in files:
                    logger.info(f"Cache limit exceeded, removing oldest: {os.path.basename(path)}")
                    os.remove(path)
                    total_size -= size
                    if total_size <= CACHE_MAX_SIZE:
                        break

            # Also clean thumbnails older than CACHE_MAX_AGE
            if os.path.exists(THUMB_CACHE_DIR):
                for f in os.listdir(THUMB_CACHE_DIR):
                    path = os.path.join(THUMB_CACHE_DIR, f)
                    if os.path.isfile(path) and now - os.path.getmtime(path) > CACHE_MAX_AGE:
                        os.remove(path)

        except Exception as e:
            logger.error(f"Error in cache cleaner: {e}")
            
        await asyncio.sleep(3600)  # Run once an hour


def _run_background_loop():
    """Run the async background loop for cache cleaning."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(_cache_cleaner_loop())
    loop.run_forever()



resilient_lock = threading.Lock()

def _setup_resilient_mode(bot):
    original_send_msg = bot.rpc.send_msg

    def patched_send_msg(account_id, chat_id, msg_data):
        try:
            is_resilient = database.get_config("resilient") == "1"
        except Exception:
            is_resilient = False

        if not is_resilient:
            return original_send_msg(account_id, chat_id, msg_data)

        try:
            transports = bot.rpc.list_transports(account_id)
        except Exception:
            transports = []

        if len(transports) <= 1:
            return original_send_msg(account_id, chat_id, msg_data)

        initial_addr = None
        try:
            initial_addr = bot.rpc.get_config(account_id, "configured_addr") or bot.rpc.get_config(account_id, "addr")
        except Exception:
            pass

        # 1. Send the message normally via the current primary transport (non-blocking queueing)
        try:
            msg_id = original_send_msg(account_id, chat_id, msg_data)
            bot.logger.info(f"Resilient send: initial msg queued with ID {msg_id} on transport {initial_addr}.")
        except Exception as send_err:
            bot.logger.error(f"Resilient send: failed to queue initial message: {send_err}")
            return None

        # Background worker to handle resending to other transports sequentially
        def bg_resend_worker(m_id, init_addr, t_list):
            bot.logger.info(f"Resilient send: starting background sender for msg {m_id}")
            with resilient_lock:
                bot.logger.info(f"Resilient send bg: waiting for initial delivery of msg {m_id} on {init_addr}...")
                start_time = time.time()
                delivered = False
                while time.time() - start_time < 10:
                    try:
                        msg_snapshot = bot.rpc.get_message(account_id, m_id)
                        state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                        if state in (26, 28):
                            bot.logger.info(f"Resilient send bg: initial msg {m_id} delivered successfully on {init_addr}.")
                            delivered = True
                            break
                        if state == 24:
                            bot.logger.warning(f"Resilient send bg: initial msg {m_id} failed on {init_addr}.")
                            break
                    except Exception as poll_err:
                        bot.logger.debug(f"Resilient send bg initial poll error: {poll_err}")
                    time.sleep(0.5)

                if not delivered:
                    bot.logger.warning(f"Resilient send bg: initial msg {m_id} did not deliver on {init_addr} within timeout.")

                # 2. Resend on all other transports
                for t in t_list:
                    t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
                    if not t_addr or (init_addr and t_addr.lower() == init_addr.lower()):
                        continue

                    bot.logger.info(f"Resilient send bg: switching primary transport to {t_addr}")
                    try:
                        bot.rpc.set_config(account_id, "configured_addr", t_addr)
                        time.sleep(1)
                    except Exception as switch_err:
                        bot.logger.error(f"Resilient send bg: failed to switch transport to {t_addr}: {switch_err}")
                        continue

                    try:
                        bot.logger.info(f"Resilient send bg: resending msg {m_id} on transport {t_addr}...")
                        bot.rpc.resend_messages(account_id, [m_id])

                        # Wait up to 10 seconds for the resent message to be delivered/failed
                        start_time = time.time()
                        delivered = False
                        while time.time() - start_time < 10:
                            try:
                                msg_snapshot = bot.rpc.get_message(account_id, m_id)
                                state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                                if state in (26, 28):
                                    bot.logger.info(f"Resilient send bg: msg {m_id} delivered successfully on {t_addr}.")
                                    delivered = True
                                    break
                                if state == 24:
                                    bot.logger.warning(f"Resilient send bg: msg {m_id} failed on {t_addr}.")
                                    break
                            except Exception as poll_err:
                                bot.logger.debug(f"Resilient send bg poll error: {poll_err}")
                            time.sleep(0.5)

                        if not delivered:
                            bot.logger.warning(f"Resilient send bg: msg {m_id} did not deliver on {t_addr} within timeout.")
                    except Exception as resend_err:
                        bot.logger.error(f"Resilient send bg: failed to resend message on transport {t_addr}: {resend_err}")

                # 3. Restore the initial primary transport configuration
                if init_addr:
                    try:
                        bot.logger.info(f"Resilient send bg: restoring initial primary transport to {init_addr}")
                        bot.rpc.set_config(account_id, "configured_addr", init_addr)
                    except Exception as restore_err:
                        bot.logger.error(f"Resilient send bg: failed to restore transport to {init_addr}: {restore_err}")

        # Start the background thread for resilient sending
        threading.Thread(target=bg_resend_worker, args=(msg_id, initial_addr, transports), daemon=True).start()

        return msg_id

    bot.rpc.send_msg = patched_send_msg


_message_failover_attempts = {}

@dc_cli.on(events.RawEvent(events.EventType.MSG_FAILED))
def on_msg_failed(bot, accid, event):
    """Handle message sending failures by switching to a backup transport temporarily with backoff."""
    try:
        if database.get_config("resilient") == "1":
            return
    except Exception:
        pass

    msg_id = getattr(event, 'msg_id', None)
    if not msg_id:
        return

    try:
        global _message_failover_attempts
        if len(_message_failover_attempts) > 1000:
            _message_failover_attempts.clear()

        # Retrieve or initialize tracking state for this message
        state = _message_failover_attempts.get(msg_id)
        if state is None:
            state = {'count': 0, 'transports': set()}
            _message_failover_attempts[msg_id] = state

        # Stop retrying if we reached the maximum attempt limit (e.g. 10 attempts)
        if state['count'] >= 10:
            return

        state['count'] += 1

        # Retrieve message and verify it is indeed in failed state (state 24)
        try:
            msg_snapshot = bot.rpc.get_message(accid, msg_id)
            msg_state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
            if msg_state != 24:
                return
        except Exception:
            return

        # Fetch chat details to include in logs (checking both snake_case and camelCase key fallbacks)
        chat_id = None
        if isinstance(msg_snapshot, dict):
            chat_id = msg_snapshot.get('chat_id') or msg_snapshot.get('chatId')
        else:
            chat_id = getattr(msg_snapshot, 'chat_id', getattr(msg_snapshot, 'chatId', None))
            
        chat_name = "Unknown"

        if chat_id:
            try:
                chat_info = bot.rpc.get_full_chat_by_id(accid, chat_id)
                if isinstance(chat_info, dict):
                    chat_name = chat_info.get('name', 'Unknown')
                else:
                    chat_name = getattr(chat_info, 'name', 'Unknown')
            except Exception:
                pass

        # Check if it's a permanent E2E encryption failure
        msg_error = msg_snapshot.get('error') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'error', None)
        if msg_error:
            msg_error_lower = msg_error.lower()
            if "encryption" in msg_error_lower or "unencrypted" in msg_error_lower or "шифр" in msg_error_lower or "зашифр" in msg_error_lower:
                bot.logger.warning(
                    f"Permanent E2E encryption failure for message {msg_id} in chat '{chat_name}' (ID: {chat_id}): {msg_error}. "
                    f"Stopping failover attempts immediately."
                )
                return

        # List all configured transports
        try:
            transports = bot.rpc.list_transports(accid)
        except Exception:
            transports = []

        if len(transports) <= 1:
            bot.logger.info(f"Message {msg_id} failed to send, but only {len(transports)} transport(s) configured. Cannot failover.")
            return

        current_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if not current_addr:
            return

        # Find current transport index
        current_idx = -1
        for idx, t in enumerate(transports):
            t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
            if t_addr and t_addr.lower() == current_addr.lower():
                current_idx = idx
                break

        if current_idx == -1:
            bot.logger.warning(f"Current transport {current_addr} not found in transports list.")
            current_idx = 0

        # Try to find the next transport
        next_idx = (current_idx + 1) % len(transports)
        next_t = transports[next_idx]
        next_addr = next_t.get('addr') if isinstance(next_t, dict) else getattr(next_t, 'addr', None)

        if not next_addr or next_addr.lower() == current_addr.lower():
            bot.logger.info("No alternative transport available for failover.")
            return

        # Check if we have already tried this transport for this message
        if next_addr.lower() in state['transports']:
            if len(state['transports']) >= len(transports):
                bot.logger.warning(f"All available transports have been tried for message {msg_id}. Stopping failover.")
                return

        state['transports'].add(current_addr.lower())

        # Calculate exponential backoff delay: 5, 10, 20, 40, 80, 160... seconds (max 5 minutes)
        delay = min(300, 5 * (2 ** (state['count'] - 1)))
        bot.logger.warning(
            f"Resilient Failover: Message {msg_id} (Chat: {chat_name}, ID: {chat_id}) failed on {current_addr} (attempt {state['count']}/10). "
            f"Scheduling resend on transport {next_addr} in {delay}s."
        )

        init_addr = current_addr

        # Schedule the resend asynchronously using a non-blocking Timer thread
        def delayed_resend():
            try:
                bot.logger.info(f"Executing scheduled resend for message {msg_id} in chat '{chat_name}' (ID: {chat_id}) on transport {next_addr}...")
                with resilient_lock:
                    # Switch configured_addr to next transport temporarily
                    bot.rpc.set_config(accid, "configured_addr", next_addr)
                    time.sleep(1) # Give core a moment to reconfigure
                    
                    bot.rpc.resend_messages(accid, [msg_id])
                    
                    # Wait up to 10 seconds for the resent message to be delivered/failed
                    start_time = time.time()
                    delivered = False
                    while time.time() - start_time < 10:
                        try:
                            raw_msg = bot.rpc.get_message(accid, msg_id)
                            if raw_msg:
                                from deltachat2 import AttrDict
                                msg_snapshot = AttrDict(raw_msg)
                                state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                                if state in (26, 28):
                                    bot.logger.info(f"Resilient Failover bg: msg {msg_id} delivered successfully on {next_addr}.")
                                    delivered = True
                                    break
                                if state == 24:
                                    bot.logger.warning(f"Resilient Failover bg: msg {msg_id} failed on {next_addr}.")
                                    break
                        except Exception as poll_err:
                            bot.logger.debug(f"Resilient Failover bg poll error: {poll_err}")
                        time.sleep(0.5)

                    if not delivered:
                        bot.logger.warning(f"Resilient Failover bg: msg {msg_id} did not deliver on {next_addr} within timeout.")

            except Exception as resend_err:
                bot.logger.warning(f"Error executing scheduled resend for message {msg_id} in chat '{chat_name}' (ID: {chat_id}): {resend_err}")
                err_str = str(resend_err).lower()
                if "e2e encryption" in err_str or "encryption" in err_str:
                    bot.logger.warning(f"E2E encryption error detected during resend of msg {msg_id} in chat '{chat_name}'. Stopping further failovers.")
                    try:
                        _message_failover_attempts[msg_id]['count'] = 10
                    except Exception:
                        pass
            finally:
                # Always restore the initial primary transport address!
                try:
                    bot.logger.info(f"Resilient Failover bg: restoring primary transport to {init_addr}")
                    bot.rpc.set_config(accid, "configured_addr", init_addr)
                except Exception as restore_err:
                    bot.logger.error(f"Resilient Failover bg: failed to restore transport to {init_addr}: {restore_err}")

        import threading
        threading.Timer(delay, delayed_resend).start()



    except Exception as e:
        bot.logger.error(f"Error handling message failover for message {msg_id}: {e}")


@dc_cli.on_init
def on_init(bot, args):
    global dc_bot_instance, dc_accid
    bot.logger.info(f"Initializing Delta Chat YouTube Bot v{VERSION}...")
    dc_bot_instance = bot
    _setup_resilient_mode(bot)
    
    # Ensure cache dir exists
    os.makedirs(CACHE_DIR, exist_ok=True)
    
    # Start background loop thread
    bg_thread = threading.Thread(target=_run_background_loop, daemon=True)
    bg_thread.start()

    for accid in bot.rpc.get_all_account_ids():
        global dc_accid
        dc_accid = accid
        logger.info(f"Initialized with accid {accid}")
        bot.rpc.set_config(accid, "displayname", "YT Bot")
        bot.rpc.set_config(accid, "selfstatus",
                           "I download YouTube videos and audio. Send /help for commands.")
        bot.rpc.set_config(accid, "delete_device_after", "3600")
        try:
            bot.rpc.set_config(accid, "download_limit", "1")
            bot.logger.info("Configured auto-download limit (1 byte) in on_init.")
        except Exception as e:
            bot.logger.warning(f"Could not configure storage optimization in on_init: {e}")
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            for icon_name in ["icon.png", os.path.join("data", "icon.png")]:
                icon_path = os.path.join(base_dir, icon_name)
                if os.path.exists(icon_path):
                    bot.rpc.set_config(accid, "selfavatar", icon_path)
                    break
        except Exception as e:
            bot.logger.warning(f"Could not set avatar: {e}")


def _check_cookies_on_startup(bot):
    """Check YouTube and Yandex Music accounts and configuration on startup."""
    # Check YouTube status
    yt_ok, yt_msg = _check_youtube_status()
    yt_icon = "✅" if yt_ok else "ℹ️" if ("guest" in yt_msg.lower() or "not configured" in yt_msg.lower()) else "⚠️"
    bot.logger.info(f"YouTube Status: {yt_icon} {yt_msg}")
    print(f"YouTube Status: {yt_icon} {yt_msg}")

    # Check Yandex Music status
    ym_ok, ym_msg = _check_yandex_status()
    ym_icon = "✅" if ym_ok else "ℹ️" if ("guest" in ym_msg.lower() or "not configured" in ym_msg.lower()) else "⚠️"
    bot.logger.info(f"Yandex Music Status: {ym_icon} {ym_msg}")
    print(f"Yandex Music Status: {ym_icon} {ym_msg}")


def setup_custom_command_parser(bot, allowed_prefixes):
    original_parse_command = bot._parse_command

    def custom_parse_command(accid: int, event) -> None:
        text = event.msg.text
        if not text:
            original_parse_command(accid, event)
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0]
        
        if "@" in cmd:
            cmd_name, suffix = cmd.split("@", 1)
            suffix_lower = suffix.lower()
            
            if suffix_lower:
                try:
                    self_address = bot.rpc.get_contact(accid, 1).address.lower()
                except Exception:
                    self_address = ""
                
                matched = False
                for p in allowed_prefixes:
                    if suffix_lower.startswith(p.lower()) or p.lower().startswith(suffix_lower):
                        matched = True
                        break
                if not matched and self_address and suffix_lower == self_address:
                    matched = True
                
                if matched:
                    new_text = cmd_name
                    if len(parts) > 1:
                        new_text += " " + parts[1]
                    
                    original_text = event.msg.text
                    event.msg["text"] = new_text
                    try:
                        original_parse_command(accid, event)
                    finally:
                        event.msg["text"] = original_text
                else:
                    event.command = ""
                    event.payload = ""
            else:
                original_parse_command(accid, event)
        else:
            original_parse_command(accid, event)
            
            if event.command in ("/help", "/stats"):
                try:
                    chat = bot.rpc.get_chat(accid, event.msg.chat_id)
                    is_group = getattr(chat, "chat_type", "Single") != "Single"
                except Exception:
                    is_group = False
                
                if is_group:
                    try:
                        contacts = bot.rpc.get_chat_contacts(accid, event.msg.chat_id)
                        bot_count = 0
                        for contact_id in contacts:
                            if contact_id == 1:
                                bot_count += 1
                                continue
                            c = bot.rpc.get_contact(accid, contact_id)
                            if getattr(c, "is_bot", False):
                                bot_count += 1
                                if bot_count > 1:
                                    break
                        if bot_count > 1:
                            event.command = ""
                            event.payload = ""
                    except Exception:
                        pass

    bot._parse_command = custom_parse_command


@dc_cli.on_start
def on_start(bot, _args):
    global dc_bot_instance, dc_accid
    setup_custom_command_parser(bot, ["yt"])
    dc_bot_instance = bot
    bot.logger.info(f"Starting Delta Chat YouTube Bot v{VERSION}...")
    print(f"\n🤖 Delta Chat YouTube Bot v{VERSION}")
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        dc_accid = accounts[0]
        try:
            bot.rpc.set_config(dc_accid, "download_limit", "1")
            bot.rpc.set_config(dc_accid, "delete_device_after", "3600")
            bot.logger.info("Successfully set auto-download limit to 1 byte and delete_device_after to 1 hour to optimize storage.")
        except Exception as e:
            bot.logger.error(f"Failed to set storage optimization settings in on_start: {e}")
            
        # Check cookies asynchronously on startup
        threading.Thread(target=_check_cookies_on_startup, args=(bot,), daemon=True).start()

        # Check Navidrome status asynchronously on startup
        def _check_navidrome_on_startup():
            nav_ok, nav_msg = _check_navidrome_status()
            icon = "✅" if nav_ok else "⚠️"
            logger.info(f"Navidrome Status: {icon} {nav_msg}")
            print(f"Navidrome Status: {icon} {nav_msg}")

        threading.Thread(target=_check_navidrome_on_startup, daemon=True).start()

        allowed_bots_env = os.environ.get("ALLOWED_BOT_EMAILS", "")
        allowed_bots = [e.strip().lower() for e in allowed_bots_env.split(",") if e.strip()]
        if allowed_bots:
            logger.info(f"Whitelisted bot emails: {', '.join(allowed_bots)}")
        else:
            logger.info("No whitelisted bot emails configured (other bots will be ignored).")
        
        # Show configured admin and transports
        admin_email = database.get_config("admin_dc_email")
        admin_fp = database.get_admin_fingerprint()
        if admin_email:
            fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
            print(f"Bot Administrator: {admin_email}{fp_suffix}")
            
        try:
            transports = bot.rpc.list_transports(dc_accid)
            print("\n" + "=" * 50)
            print("Configured Bot Transports (Relays):")
            for t in transports:
                a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                print(f" - {a}")
        except Exception:
            pass

        try:
            import io
            try:
                import qrcode
            except ImportError:
                qrcode = None

            qrdata = bot.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            print("\nTo add this bot, scan the QR code or copy the link:\n")
            if qrcode:
                qr = qrcode.QRCode(version=1, box_size=1, border=2)
                qr.add_data(qrdata)
                qr.make(fit=True)
                f = io.StringIO()
                qr.print_ascii(out=f)
                print(f.getvalue())
            print(qrdata)
            print("\n" + "=" * 50 + "\n")
        except Exception as e:
            bot.logger.error(f"Failed to generate QR code: {e}")


if __name__ == "__main__":
    import sys
    
    # Handle 'init transport' CLI command
    if len(sys.argv) > 2 and sys.argv[1] == "init" and sys.argv[2] == "transport":
        if len(sys.argv) < 5:
            print("Usage: python bot.py init transport <email> <password>")
            sys.exit(1)
            
        addr, password = sys.argv[3], sys.argv[4]
        
        # We need to manually initialize RPC to add transport without starting the bot
        from deltachat2 import Rpc, IOTransport
        from appdirs import user_config_dir
        
        config_dir = user_config_dir("ytbot")
        accounts_dir = os.path.join(config_dir, "accounts")
        
        try:
            with IOTransport(accounts_dir=accounts_dir) as trans:
                rpc = Rpc(trans)
                accids = rpc.get_all_account_ids()
                if not accids:
                    print("Error: No accounts configured. Run 'python bot.py init addr password' first.")
                    sys.exit(1)
                    
                rpc.add_or_update_transport(accids[0], {"addr": addr, "password": password})
                print(f"Success: Backup transport {addr} added.")
        except Exception as e:
            print(f"Error adding transport: {e}")
            sys.exit(1)
        sys.exit(0)

    if len(sys.argv) == 1:
        sys.argv.append("serve")
    dc_cli.start()
