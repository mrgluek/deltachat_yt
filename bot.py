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

# Semaphore for yt-dlp concurrency
_download_semaphore = asyncio.Semaphore(5)

# Thread-safe refcounted locks for per-video synchronization
class RefCountLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.refs = 0

_global_lock_mgr = threading.Lock()
_download_locks: dict[str, RefCountLock] = {}

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


def _make_yt_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _extract_video_id(text: str) -> str | None:
    """Extract YouTube video ID from URL or raw ID."""
    text = text.strip()
    m = YT_URL_RE.search(text)
    if m:
        return m.group(1)
    if YT_ID_RE.match(text):
        return text
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
    if contact:
        get_val = getattr(contact, 'get', lambda k: getattr(contact, k, None))
        for attr in ['fingerprint', 'key_fingerprint', 'public_key']:
            val = get_val(attr)
            if val:
                matches = re.findall(r'[0-9a-fA-F]{32,64}', str(val).replace(' ', '').replace(':', ''))
                if matches:
                    return matches[0].upper()
    try:
        fp = bot.rpc.get_contact_config(accid, contact_id, "fp")
        if fp:
            return fp.upper().replace(' ', '')
    except Exception:
        pass
    for args in [(accid, contact_id), (contact_id,)]:
        try:
            enc_info = bot.rpc.get_contact_encryption_info(*args)
            if enc_info:
                cleaned = "".join(enc_info.split()).replace(':', '')
                matches = re.findall(r'[0-9a-fA-F]{32,64}', cleaned)
                if matches:
                    return ",".join(matches).upper()
        except Exception:
            continue
    return None


def _is_dc_admin(bot, accid, contact_id):
    """
    Checks if contact_id is the admin.
    Priority: Fingerprint (if set) > Email.
    """
    try:
        admin_fp = database.get_admin_fingerprint()
        admin_email = database.get_config("admin_dc_email")

        # If no admin is configured at all, no one is admin
        if not admin_fp and not admin_email:
            return False

        contact = None
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            pass

        if not contact:
            return False

        # 1. If fingerprint is set in DB, we prefer checking it
        if admin_fp:
            c_fp = _get_contact_fingerprint(bot, accid, contact_id, contact=contact)
            if c_fp:
                if admin_fp.upper() in c_fp.upper().split(','):
                    return True
                # If fingerprints exist but don't match, we don't trust the email!
                return False
            # If we expect a fingerprint but contact doesn't have one yet,
            # we fall through to email check ONLY if email is also set.

        # 2. Check email
        if admin_email and contact.address:
            if admin_email.strip().lower() == contact.address.strip().lower():
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
        try:
            addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
            if addr:
                database.increment_transport_sent(addr)
        except Exception:
            pass
        return msg_id
    except Exception as e:
        logger.error(f"Failed to send msg to chat {chat_id}: {e}")
        raise


def _react(bot, accid, msg_id, emoji: str):
    """Set a reaction on a message."""
    try:
        # emoji_list expects a list of strings
        bot.rpc.send_reaction(accid, msg_id, [emoji] if emoji else [])
    except Exception as e:
        logger.debug(f"Failed to set reaction on msg {msg_id}: {e}")


def _get_cache_path(video_id: str, download_type: str) -> str:
    ext = "mp4" if download_type == "video" else "opus"
    return os.path.join(CACHE_DIR, f"{video_id}.{ext}")


# ── yt-dlp wrappers ──

def _find_file_in_dir(directory: str, extensions: list[str]) -> str | None:
    """Find the first file in directory matching any of the given extensions."""
    if not os.path.isdir(directory):
        return None
    for f in os.listdir(directory):
        fpath = os.path.join(directory, f)
        if os.path.isfile(fpath) and any(f.lower().endswith(ext) for ext in extensions):
            return fpath
    return None


async def _fetch_video_info(video_id: str) -> dict | None:
    """Fetch video metadata without downloading."""
    cmd = [
        "yt-dlp", "--no-playlist", "--dump-json", "--no-warnings",
        _make_yt_url(video_id)
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and stdout:
            return json.loads(stdout)
    except Exception as e:
        logger.error(f"Failed to fetch info for {video_id}: {e}")
    return None


async def _download_video(video_id: str, output_dir: str) -> tuple[str | None, dict | None, str | None]:
    """Download video. Returns (filepath, info_dict, error_string)."""
    out_template = os.path.join(output_dir, "%(title).50s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--match-filter", f"duration<={MAX_DURATION_VIDEO}",
        "-S", "vcodec:h264,res:480,acodec:aac,+size",
        "--max-filesize", "50M",
        "--merge-output-format", "mp4",
        "--no-warnings",
        "--print-json",
        "-o", out_template,
        _make_yt_url(video_id)
    ]
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
                return None, None, "📦 Video exceeds 50 MB size limit"
            return None, None, f"yt-dlp error: {err[:200]}"

        if not stdout:
            logger.warning(f"yt-dlp video returned no stdout for {video_id} (likely filtered out by duration)")
            return None, None, f"⏱ Video is longer than {MAX_DURATION_VIDEO // 60} minutes"

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
                filepath = _find_file_in_dir(output_dir, ['.mp4', '.mkv', '.webm'])
        if filepath and os.path.exists(filepath):
            size = os.path.getsize(filepath)
            if size > 50 * 1024 * 1024:
                os.remove(filepath)
                return None, info, "📦 Downloaded file exceeds 50 MB"
            return filepath, info, None
        return None, info, "Download completed but file not found"
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except:
            pass
        return None, None, "⏱ Download timed out (5 min limit)"
    except Exception as e:
        return None, None, f"Error: {e}"


async def _download_audio(video_id: str, output_dir: str, duration: int) -> tuple[str | None, dict | None, str | None]:
    """Download audio. Returns (filepath, info_dict, error_string)."""
    fmt = "opus"
    if duration <= 600:
        pp_args = ["--postprocessor-args", "ffmpeg:-ac 2 -ar 48000 -b:a 128k"]
    else:
        pp_args = ["--postprocessor-args", "ffmpeg:-ac 1 -ar 24000 -b:a 64k"]

    out_template = os.path.join(output_dir, f"{video_id}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--match-filter", f"duration<={MAX_DURATION_AUDIO}",
        "-x",
        "--audio-format", fmt,
    ] + pp_args + [
        "--no-warnings",
        "--print-json",
        "-o", out_template,
        _make_yt_url(video_id)
    ]
    try:
        async with _download_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            err = stderr.decode(errors='replace').strip()
            if "duration" in err.lower() or "filter" in err.lower():
                return None, None, f"⏱ Audio is longer than {MAX_DURATION_AUDIO // 60} minutes"
            return None, None, f"yt-dlp error: {err[:200]}"

        info = {}
        if stdout:
            info = json.loads(stdout.decode(errors='replace').strip())

        filepath = None
        expected_path = os.path.join(output_dir, f"{video_id}.opus")
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
                filepath = _find_file_in_dir(output_dir, ['.opus', '.mp3', '.m4a', '.webm'])

        if filepath and os.path.exists(filepath):
            size = os.path.getsize(filepath)
            if size > 50 * 1024 * 1024:
                os.remove(filepath)
                return None, info, "📦 Audio file exceeds 50 MB"
            return filepath, info, None
        
        logger.error(f"Audio file not found for {video_id}. Dir contents: {os.listdir(output_dir)}")
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


async def _do_download(bot, accid, msg, video_id: str, download_type: str):
    """Actual download + send logic."""
    chat_id = msg.chat_id
    req_msg_id = msg.id
    
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
    cache_path = _get_cache_path(video_id, download_type)
    if os.path.exists(cache_path):
        os.utime(cache_path, None)
        await _send_from_cache(bot, accid, msg, video_id, download_type, cache_path)
        return

    # 3. Fetch info to know duration for audio strategy
    info = await _fetch_video_info(video_id)
    if not info:
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, "❌ Could not fetch video info")
        return
    
    duration = int(info.get("duration", 0))

    # 4. Wait for lock if already downloading same ID
    with get_download_lock(video_id + download_type):
        cache_path = _get_cache_path(video_id, download_type)
        if os.path.exists(cache_path):
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
                if download_type == "video":
                    filepath, info, error = await _download_video(video_id, tmpdir)
                else:
                    filepath, info, error = await _download_audio(video_id, tmpdir, duration)

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
                shutil.move(filepath, cache_path)
                
                await _send_from_cache(bot, accid, msg, video_id, download_type, cache_path, info)
                return

            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ {last_error or 'Download failed after retry'}")


async def _send_from_cache(bot, accid, msg, video_id, download_type, filepath, info=None):
    """Send a file from the cache to the chat."""
    chat_id = msg.chat_id
    req_msg_id = msg.id
    
    _react(bot, accid, req_msg_id, "⌛")

    if not info:
        info = await _fetch_video_info(video_id)

    title = (info or {}).get("title", video_id)
    duration = (info or {}).get("duration", 0)
    filesize = os.path.getsize(filepath)
    dur_str = _format_duration(int(duration)) if duration else "?"
    size_str = _format_size(filesize)

    ext = os.path.splitext(filepath)[1].lower().replace(".", "").upper()
    if download_type == "video":
        caption = f"📺 {title} ({dur_str}, {size_str}, {ext})\n\n🔗 https://youtu.be/{video_id}"
    else:
        caption = f"🎵 {title} ({dur_str}, {size_str}, {ext})\n\n🔗 https://youtu.be/{video_id}"

    _send(bot, accid, chat_id, caption, file=filepath)

    _react(bot, accid, req_msg_id, "☑️")

    database.add_download(chat_id, msg.from_id, video_id, title, int(duration or 0), download_type, filesize)


def _handle_download_command(bot, accid, event, download_type: str, payload: str):
    """Common handler for /yt and /ytm commands."""
    msg = event.msg
    video_id = _extract_video_id(payload)
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

@dc_cli.on(events.NewMessage(command="/yt"))
def yt_command(bot, accid, event):
    if accid != dc_accid:
        return
    _handle_download_command(bot, accid, event, "video", event.payload.strip())


@dc_cli.on(events.NewMessage(command="/ytm"))
def ytm_command(bot, accid, event):
    if accid != dc_accid:
        return
    _handle_download_command(bot, accid, event, "audio", event.payload.strip())


@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    help_text = _get_help_text(bot, accid, msg.from_id)
    _send(bot, accid, msg.chat_id, help_text)


@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    s = database.get_stats()
    videos = s["by_type"].get("video", 0)
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
    logger.info(f"Stats requested by {addr} (id={event.msg.from_id}), is_admin={is_admin}")

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
        f"/yt <url> — Download video (MP4 H.264+AAC 480p, ≤50MB)\n"
        f"/yt_<video_id> — Download video by ID\n"
        f"/ytm <url> — Download audio (Opus 128kbps stereo < 10 min, 64kbps mono >= 10 min, ≤50MB)\n"
        f"/ytm_<video_id> — Download audio by ID\n"
        f"/stats — Download statistics\n"
        f"/donate — Support development ❤️\n"
        f"/help — This message\n\n"
        f"💡 _You can also just paste a YouTube link and I'll show you download options._\n\n"
        f"⏱ Max duration: video {MAX_DURATION_VIDEO // 60}m, audio {MAX_DURATION_AUDIO // 60}m | Max file: 50 MB\n"
    )

    admin_email = database.get_config("admin_dc_email")
    if not admin_email:
        help_text += "\n/initadmin — Claim bot ownership\n"
    elif _is_dc_admin(bot, accid, from_id):
        help_text += f"\n👑 **Admin:** `{admin_email}`\n"

    return help_text


# ── YouTube link auto-detection and /yt_ID, /ytm_ID handlers ──

@dc_cli.on(events.NewMessage)
def on_new_message(bot, accid, event):
    msg = event.msg
    
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

    # 1. Handle /yt_VIDEOID and /ytm_VIDEOID commands
    m = re.match(r'^/yt_([a-zA-Z0-9_-]{11})$', text)
    if m:
        video_id = m.group(1)
        if _is_rate_limited(bot, accid, msg.from_id):
            _send(bot, accid, msg.chat_id, f"⏱ Please wait {RATE_LIMIT_SECONDS}s between downloads.")
            return
        t = threading.Thread(target=_run_download, args=(bot, accid, msg, video_id, "video"), daemon=True)
        t.start()
        return

    m = re.match(r'^/ytm_([a-zA-Z0-9_-]{11})$', text)
    if m:
        video_id = m.group(1)
        if _is_rate_limited(bot, accid, msg.from_id):
            _send(bot, accid, msg.chat_id, f"⏱ Please wait {RATE_LIMIT_SECONDS}s between downloads.")
            return
        t = threading.Thread(target=_run_download, args=(bot, accid, msg, video_id, "audio"), daemon=True)
        t.start()
        return

    # 2. Auto-detect YouTube links and respond with info
    if text.startswith('/'):
        return  # Don't process other commands

    yt_match = YT_URL_RE.search(text)
    if yt_match:
        video_id = yt_match.group(1)
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
            if not bot.rpc.get_contact_config(accid, msg.from_id, "greeted"):
                help_text = _get_help_text(bot, accid, msg.from_id)
                _send(bot, accid, msg.chat_id, f"👋 Welcome to YT Bot!\n\n{help_text}")
                bot.rpc.set_contact_config(accid, msg.from_id, "greeted", "1")
    except Exception as e:
        logger.error(f"Greeting check error: {e}")


def _handle_link_info(bot, accid, msg, video_id: str):
    """Fetch video info and reply with download commands."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        info = loop.run_until_complete(_fetch_video_info(video_id))
    finally:
        loop.close()

    if not info:
        return  # Silently ignore if we can't fetch info

    title = info.get("title", "Unknown")
    duration = info.get("duration", 0)
    dur_str = _format_duration(int(duration)) if duration else "?"

    audio_fmt = "Opus"
    
    # Size estimation
    video_size_str = "?? MB"
    audio_size_str = "?? MB"
    if duration:
        # Audio estimation: Opus 128k for short, 64k for long
        bitrate = 128 if duration <= 600 else 64
        audio_mb = (duration * bitrate) / 8192
        audio_size_str = f"~{audio_mb:.1f} MB"
        
        # Video 480p estimation
        video_mb = 0
        # Try to find a real 480p format size in the metadata
        for f in info.get('formats', []):
            if f.get('height') == 480 and f.get('vcodec') != 'none':
                fs = f.get('filesize') or f.get('filesize_approx')
                if fs:
                    video_mb = fs / 1048576
                    break
        
        # Fallback if no format size found: use ~500kbps (0.06 MB/s)
        if not video_mb:
            video_mb = duration * 0.06
            
        video_size_str = f"~{min(video_mb, 50.0):.1f} MB"

    can_video = duration <= MAX_DURATION_VIDEO
    can_audio = duration <= MAX_DURATION_AUDIO

    lines = [f"📺 YouTube: \"{title}\" ({dur_str})", ""]
    
    if can_video:
        lines.append(f"Download video 480p ({video_size_str}): /yt_{video_id}")
    else:
        lines.append(f"⚠️ Video too long (> {MAX_DURATION_VIDEO // 60}m)")
        
    if can_audio:
        lines.append(f"Download audio {audio_fmt} ({audio_size_str}): /ytm_{video_id}")
    else:
        lines.append(f"⚠️ Audio too long (> {MAX_DURATION_AUDIO // 60}m)")

    _send(bot, accid, msg.chat_id, "\n".join(lines))


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

        except Exception as e:
            logger.error(f"Error in cache cleaner: {e}")
            
        await asyncio.sleep(3600)  # Run once an hour


def _run_background_loop():
    """Run the async background loop for cache cleaning."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(_cache_cleaner_loop())
    loop.run_forever()


# ── Bot lifecycle ──

@dc_cli.on_init
def on_init(bot, args):
    global dc_bot_instance, dc_accid
    bot.logger.info("Initializing YT Bot...")
    dc_bot_instance = bot
    
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
        bot.rpc.set_config(accid, "delete_device_after", "604800")
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
            import io
            try:
                import qrcode
            except ImportError:
                qrcode = None

            qrdata = bot.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            print("\n" + "=" * 50)
            print("To add this bot, scan the QR code or copy the link:\n")
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
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    dc_cli.start()
