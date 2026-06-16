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
import hashlib

from deltachat2 import events, MsgData
from deltabot_cli import BotCli

import database

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("yt_bot")

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
    r'https?://(?:www\.)?(?:soundcloud\.com|music\.yandex\.(?:ru|com|by|kz))/'
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


def _make_yt_url(video_id: str) -> str:
    if video_id.startswith("http://") or video_id.startswith("https://"):
        return video_id
    return f"https://youtu.be/{video_id}"


def _extract_video_id(text: str) -> str | None:
    """Extract YouTube video ID or recognize supported full URLs / short hashes.
    
    This function handles both raw IDs/URLs and full command strings 
    (e.g. '/yt_ID' or '/yt URL') by using non-anchored searches.
    """
    text = text.strip()
    
    # 0. Check if it's a short hash for a full URL (stored in database)
    # We look for a 16-char hex hash at the end of the string, preceded by _ or space
    m_hash = re.search(r'(?:^|[_ ])([a-f0-9]{16})$', text)
    if m_hash:
        resolved = database.resolve_url(m_hash.group(1))
        if resolved:
            return resolved
        
    # 1. Supported non-YouTube URLs: Return the FULL URL
    m_supported = SUPPORTED_URL_RE.search(text)
    if m_supported:
        return m_supported.group(0)
        
    # 2. YouTube URL -> 11-char ID (unless it has a time parameter)
    m_yt = YT_URL_RE.search(text)
    if m_yt:
        if 't=' in text or 'start=' in text:
            m_any_url = re.search(r'https?://[^\s]+', text)
            if m_any_url:
                return m_any_url.group(0)
        return m_yt.group(1)
        
    # 3. Direct YouTube 11-char ID or generic 11-16 char ID
    # Matches IDs at the end of the string, preceded by start of string, space, or underscore.
    m_id = re.search(r'(?:^|[_ ])([a-zA-Z0-9_-]{11,16})$', text)
    if m_id:
        # Extra check: if it's 16 chars, it might be a hash we missed in step 0
        if len(m_id.group(1)) == 16:
            resolved = database.resolve_url(m_id.group(1))
            if resolved:
                return resolved
        return m_id.group(1)
        
    # 4. Fallback: if there's an http link anywhere, return it
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
    return f"{m}:{s:02d}"


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
    """Send a message and track transport stats with failover."""
    msg_data = MsgData(text=text)
    if file:
        msg_data.file = file
        
    # Try to determine how many attempts we should make based on number of transports
    try:
        transports = bot.rpc.list_transports(accid)
        max_attempts = max(2, len(transports))
    except Exception:
        transports = []
        max_attempts = 2

    for attempt in range(max_attempts):
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
            error_str = str(e).lower()
            logger.warning(f"Attempt {attempt + 1} failed to send message: {e}")
            
            # List of strings that suggest a transport/network level failure
            transport_errors = ["network", "timeout", "connection", "unreachable", "smtp", "status 0", "socket", "refused", "auth"]
            
            if attempt < max_attempts - 1 and any(err in error_str for err in transport_errors):
                try:
                    current_addr = bot.rpc.get_config(accid, "addr")
                    if not transports:
                        transports = bot.rpc.list_transports(accid)
                    
                    if len(transports) > 1:
                        for t in transports:
                            t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
                            if t_addr and t_addr != current_addr:
                                logger.info(f"Switching transport from {current_addr} to backup: {t_addr}")
                                try:
                                    bot.rpc.set_config(accid, "addr", t_addr)
                                    t_pw = t.get('password') if isinstance(t, dict) else getattr(t, 'password', None)
                                    if t_pw:
                                        bot.rpc.set_config(accid, "mail_pw", t_pw)
                                    time.sleep(2)
                                    break 
                                except Exception as set_e:
                                    logger.error(f"Failed to switch transport: {set_e}")
                                    continue
                except: pass
            else:
                break

    logger.error(f"Final failure sending msg to chat {chat_id} after {max_attempts} attempts.")
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
        
        match_ext = not extensions or any(f.lower().endswith(ext.lower()) for ext in extensions)
        match_prefix = not prefix or f.lower().startswith(prefix.lower())
        
        if match_ext and match_prefix:
            candidates.append(fpath)
    
    if not candidates and prefix:
        # Fallback: ignore extensions if we have a prefix and no match found
        for f in os.listdir(directory):
            fpath = os.path.join(directory, f)
            if os.path.isfile(fpath) and f.lower().startswith(prefix.lower()):
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

async def _fetch_video_info(video_id: str) -> tuple[dict | None, str | None]:
    """Fetch video metadata without downloading. Returns (info, error_msg)."""
    url = _make_yt_url(video_id)
    cmd = [
        "yt-dlp", "--no-playlist", "--dump-json", "--no-warnings",
        "--no-check-certificate", "--geo-bypass",
        "--extractor-args", "youtube:player_client=android,web",
        "--js-runtimes", "deno:/root/.deno/bin/deno",
        "--no-cache-dir",
        "--no-config",
        "--add-header", "Accept-Language: en-US,en;q=0.9",
    ]
    
    if PROXY:
        cmd.extend(["--proxy", PROXY])
        
    cookies_path = os.path.join("data", "cookies.txt")
    if os.path.exists(cookies_path):
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
        # Clean up huge regional restriction errors
        if "uploader has not made this video available in your country" in err:
            err = "This video is not available in the bot's country/region."
            
        logger.error(f"yt-dlp info fetch failed for {video_id}: {err}")
        return None, err[:200]
    except asyncio.TimeoutError:
        return None, "Timeout (30s)"
    except Exception as e:
        logger.error(f"Failed to fetch info for {video_id}: {e}")
        return None, str(e)


async def _download_video(video_id: str, output_dir: str, max_height: int = 480, start_time: int = None, end_time: int = None) -> tuple[str | None, dict | None, str | None]:
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
        "-f", f"b[ext=mp4][height<={max_height}]/bv[ext=mp4][height<={max_height}]+ba[ext=m4a]/b[height<={max_height}]/b",
    ]
    if not start_time and not end_time:
        cmd.extend(["--max-filesize", "30M"])
    cmd.extend([
        "--merge-output-format", "mp4",
        "--no-warnings",
        "--no-check-certificate", "--geo-bypass",
        "--extractor-args", "youtube:player_client=android,web",
        "--js-runtimes", "deno:/root/.deno/bin/deno",
        "--no-cache-dir",
        "--no-config",
        "--add-header", "Accept-Language: en-US,en;q=0.9",
        "--print-json",
        "-o", out_template,
    ])
    
    if PROXY:
        cmd.extend(["--proxy", PROXY])
        
    cookies_path = os.path.join("data", "cookies.txt")
    if os.path.exists(cookies_path):
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
            if "duration" in err.lower() or "filter" in err.lower():
                return None, None, f"⏱ Video is longer than {MAX_DURATION_VIDEO // 60} minutes"
            if "max-filesize" in err.lower() or "filesize" in err.lower():
                return None, None, "📦 Video exceeds 30 MB size limit"
            return None, None, f"yt-dlp error: {err[:200]}"

        if not stdout:
            err = stderr.decode(errors='replace').strip()
            logger.warning(f"yt-dlp video returned no stdout for {video_id}. Stderr: {err}")
            
            if "filesize" in err.lower():
                return None, None, "📦 Video exceeds 30 MB size limit"
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
            if size > 30 * 1024 * 1024:
                os.remove(filepath)
                return None, info, "📦 Video exceeds 30 MB size limit"
            return filepath, info, None
        
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


async def _download_audio(video_id: str, output_dir: str, duration: int, start_time: int = None, end_time: int = None) -> tuple[str | None, dict | None, str | None]:
    """Download audio. Returns (filepath, info_dict, error_string)."""
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
        # Keep original format, preferring opus (for YouTube), then m4a (AAC) to avoid transcoding
        fmt = "best"
        format_selector = "ba[acodec=opus]/ba[ext=m4a]/ba"
        pp_args = []
    else:
        # For long audio (> 10 min), transcode and compress to Opus 64k mono to stay under 30MB limit
        fmt = "opus"
        format_selector = None
        pp_args = ["--postprocessor-args", "ffmpeg:-ac 1 -ar 24000 -b:a 64k"]

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
        "--no-warnings",
        "--no-check-certificate", "--geo-bypass",
        "--js-runtimes", "deno:/root/.deno/bin/deno",
        "--no-cache-dir",
        "--no-config",
        "--add-header", "Accept-Language: en-US,en;q=0.9",
        "--print-json",
        "-o", out_template,
    ])
    if pp_args:
        cmd.extend(pp_args)
    
    if PROXY:
        cmd.extend(["--proxy", PROXY])
        
    cookies_path = os.path.join("data", "cookies.txt")
    if os.path.exists(cookies_path):
        cmd.extend(["--cookies", cookies_path])
        
    cmd.append(_make_yt_url(video_id))
    
    try:
        async with _download_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

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
            if size > 30 * 1024 * 1024:
                os.remove(filepath)
                return None, info, "📦 Audio file exceeds 30 MB"
            return filepath, info, None
        
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
        info, error = await _fetch_video_info(video_id)
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
            for attempt in range(2):
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
                        filepath, info, error = await _download_video(video_id, tmpdir, max_height=initial_height, start_time=start_time, end_time=end_time)
                        if initial_height == 480 and error and ("30 MB" in error or "filtered" in error.lower()):
                            logger.info(f"Retrying {video_id} with 360p because of size/filter...")
                            filepath, info, error = await _download_video(video_id, tmpdir, max_height=360, start_time=start_time, end_time=end_time)
                    else:
                        filepath, info, error = await _download_audio(video_id, tmpdir, duration, start_time=start_time, end_time=end_time)
    
                    if error:
                        last_error = error
                        if "file not found" in error.lower() and attempt == 0:
                            continue
                        _react(bot, accid, req_msg_id, "❌")
                        _send(bot, accid, chat_id, f"❌ {error}")
                        return
    
                    if not filepath or not os.path.exists(filepath):
                        last_error = "Download failed: file not found"
                        if attempt == 0:
                            continue
                        _react(bot, accid, req_msg_id, "❌")
                        _send(bot, accid, chat_id, f"❌ {last_error}")
                        return
    
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    actual_ext = os.path.splitext(filepath)[1].lower()
                    cache_path = os.path.join(CACHE_DIR, f"{_get_cache_id(video_id)}{actual_ext}")
                    shutil.move(filepath, cache_path)
                    
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
        info, _ = await _fetch_video_info(video_id)

    title = (info or {}).get("title", video_id)
    duration = (info or {}).get("duration", 0)
    start_time, end_time = _parse_time_param(video_id)
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
    if download_type == "video":
        caption = f"📺 {title} ({dur_str}, {size_str}, {ext})\n\n🔗 {_make_yt_url(video_id)}"
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
    usage = shutil.disk_usage(CACHE_DIR)
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
        reply += (
            f"\n💾 **Disk Space (Admin only)**\n"
            f"Free: {free_gb:.1f} GB of {total_gb:.1f} GB ({free_pct:.1f}%)\n"
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
        f"I download YouTube videos and audio.\n\n"
        f"**Commands:**\n"
        f"/yt <url> — Download video (MP4 360-480p, ≤30MB)\n"
        f"/yt_<video_id> — Download video by ID\n"
        f"/ytm <url> — Download audio (Opus 128kbps stereo < 10 min, 64kbps mono >= 10 min, ≤30MB)\n"
        f"/ytm_<video_id> — Download audio by ID\n"
        f"/stats — Download statistics\n"
        f"/donate — Support development ❤️\n"
        f"/help — This message\n\n"
        f"💡 _You can also just paste a YouTube link and I'll show you download options._\n\n"
        f"⏱ Max duration: video {MAX_DURATION_VIDEO // 60}m, audio {MAX_DURATION_AUDIO // 60}m | Max file: 30 MB\n"
    )

    admin_email = database.get_config("admin_dc_email")
    admin_fp = database.get_admin_fingerprint()
    is_actually_admin = _is_dc_admin(bot, accid, from_id)
    
    if not admin_email:
        help_text += "\n/initadmin — Claim bot ownership\n"
    elif is_actually_admin:
        fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
        help_text += f"\n👑 **Admin:** `{admin_email}`{fp_suffix}\n"
        help_text += "\n**Admin Commands:**\n"
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
        info, error = loop.run_until_complete(_fetch_video_info(video_id))
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
            
        video_size_str = f"~{min(video_mb, 30.0):.1f} MB"

    can_video = duration <= MAX_DURATION_VIDEO
    can_audio = duration <= MAX_DURATION_AUDIO

    if video_id.startswith("http://") or video_id.startswith("https://"):
        short_id = _get_cache_id(video_id)
        database.add_url_mapping(short_id, video_id)
        vid_cmd = f"/yt_{short_id}"
        aud_cmd = f"/ytm_{short_id}"
    else:
        vid_cmd = f"/yt_{video_id}"
        aud_cmd = f"/ytm_{video_id}"
    
    video_url = _make_yt_url(video_id)

    video_btn = f"[ 📼 {target_height}p ({video_size_str}) {vid_cmd} ]" if can_video else f"[ 📼 Too long (> {MAX_DURATION_VIDEO // 60}m) ]"
    audio_btn = f"[ 💿 {audio_fmt} ({audio_size_str}) {aud_cmd} ]" if can_audio else f"[ 💿 Too long (> {MAX_DURATION_AUDIO // 60}m) ]"

    is_audio_only = bool(AUDIO_ONLY_URL_RE.search(video_url))

    if is_audio_only:
        lines = [
            f"🎵 Audio: \"{title}\" ({dur_str})",
            "",
            f"🔗 {video_url}",
            "",
            f"{audio_btn}"
        ]
    else:
        lines = [
            f"📺 Video: \"{title}\" ({dur_str})",
            "",
            f"🔗 {video_url}",
            "",
            f"{video_btn}",
            "",
            f"{audio_btn}"
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

@dc_cli.on_init
def on_init(bot, args):
    global dc_bot_instance, dc_accid
    bot.logger.info("Initializing YT Bot...")
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


@dc_cli.on_start
def on_start(bot, _args):
    global dc_bot_instance, dc_accid
    dc_bot_instance = bot
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        dc_accid = accounts[0]
        try:
            bot.rpc.set_config(dc_accid, "download_limit", "1")
            bot.rpc.set_config(dc_accid, "delete_device_after", "3600")
            bot.logger.info("Successfully set auto-download limit to 1 byte and delete_device_after to 1 hour to optimize storage.")
        except Exception as e:
            bot.logger.error(f"Failed to set storage optimization settings in on_start: {e}")
            
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
