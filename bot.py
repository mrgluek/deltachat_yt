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

from deltachat2 import events, MsgData
from deltabot_cli import BotCli

import database

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("yt_bot")

dc_cli = BotCli("ytbot")

# Global references
dc_bot_instance = None
dc_accid = None

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

# Global download semaphore (max 5 concurrent)
_download_semaphore = asyncio.Semaphore(5)
# Locks per video_id to prevent duplicate downloads
_download_locks = collections.defaultdict(asyncio.Lock)
_download_loop = None

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
    try:
        contact = None
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            pass
        admin_fp = database.get_admin_fingerprint()
        if admin_fp:
            c_fp = _get_contact_fingerprint(bot, accid, contact_id, contact=contact)
            if c_fp:
                if admin_fp.upper() in c_fp.upper().split(','):
                    return True
                return False
        if contact:
            admin_email = database.get_config("admin_dc_email")
            if admin_email and admin_email.lower() == contact.address.lower():
                return True
    except Exception as e:
        logger.error(f"Admin check error: {e}")
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
    if download_type == "video":
        return os.path.join(CACHE_DIR, f"{video_id}.mp4")
    # For audio, check both possible extensions
    mp3_path = os.path.join(CACHE_DIR, f"{video_id}.mp3")
    if os.path.exists(mp3_path):
        return mp3_path
    return os.path.join(CACHE_DIR, f"{video_id}.opus")


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
        # yt-dlp may report a pre-merge filename; try alternatives
        if not filepath or not os.path.exists(filepath):
            if filepath:
                base = os.path.splitext(filepath)[0]
                for ext in ['.mp4', '.mkv', '.webm']:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        filepath = candidate
                        break
            # Last resort: find any video file in the output dir
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
        return None, None, "⏱ Download timed out (5 min limit)"
    except Exception as e:
        return None, None, f"Error: {e}"


async def _download_audio(video_id: str, output_dir: str, duration: int) -> tuple[str | None, dict | None, str | None]:
    """Download audio. Returns (filepath, info_dict, error_string)."""
    # Hybrid strategy: MP3 128k for short, Opus 64k for long
    if duration <= 1800:
        fmt = "mp3"
        pp_args = ["--postprocessor-args", "ExtractAudio:-b:a 128k"]
    else:
        fmt = "opus"
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
            logger.error(f"yt-dlp audio failed (rc={proc.returncode}) for {video_id}: {err[:500]}")
            if "duration" in err.lower() or "filter" in err.lower():
                return None, None, f"⏱ Audio is longer than {MAX_DURATION_AUDIO // 60} minutes"
            return None, None, f"yt-dlp error: {err[:200]}"

        # Log raw output for debugging
        stderr_text = stderr.decode(errors='replace').strip() if stderr else ""
        if stderr_text:
            logger.info(f"yt-dlp audio stderr for {video_id}: {stderr_text[:500]}")

        info = {}
        if stdout:
            stdout_text = stdout.decode(errors='replace').strip()
            logger.info(f"yt-dlp audio stdout length: {len(stdout_text)}, _filename in output: {'_filename' in stdout_text}")
            try:
                info = json.loads(stdout_text)
                json_fn = info.get("_filename") or info.get("filename")
                logger.info(f"yt-dlp reported _filename: {json_fn}")
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse yt-dlp JSON: {e}. First 200 chars: {stdout_text[:200]}")
        else:
            logger.warning(f"yt-dlp audio returned no stdout for {video_id} (likely filtered out by duration)")
            return None, None, f"⏱ Audio is longer than {MAX_DURATION_AUDIO // 60} minutes"

        # After -x conversion, search by video_id and format
        filepath = None
        expected_path = os.path.join(output_dir, f"{video_id}.{fmt}")
        if os.path.exists(expected_path):
            filepath = expected_path
        else:
            # Fallback: try JSON filename with different extensions
            json_path = info.get("_filename") or info.get("filename")
            if json_path:
                base = os.path.splitext(json_path)[0]
                for ext in ['.mp3', '.opus', '.m4a', '.webm']:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        filepath = candidate
                        break
            # Last resort: scan directory
            if not filepath:
                filepath = _find_file_in_dir(output_dir, ['.mp3', '.opus', '.m4a', '.webm'])

        if filepath and os.path.exists(filepath):
            logger.info(f"Audio file found: {filepath} ({os.path.getsize(filepath)} bytes)")
            size = os.path.getsize(filepath)
            if size > 50 * 1024 * 1024:
                os.remove(filepath)
                return None, info, "📦 Audio file exceeds 50 MB"
            return filepath, info, None

        # Debug: log what's actually in the directory
        try:
            dir_contents = os.listdir(output_dir) if os.path.isdir(output_dir) else []
            logger.error(f"Audio file not found for {video_id}. Dir contents: {dir_contents}")
        except Exception:
            pass
        return None, info, "Download completed but file not found"
    except asyncio.TimeoutError:
        return None, None, "⏱ Download timed out (5 min limit)"
    except Exception as e:
        return None, None, f"Error: {e}"


def _run_download(bot, accid, msg, video_id: str, download_type: str):
    """Run download in a background thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_download(bot, accid, msg, video_id, download_type))
    finally:
        loop.close()


async def _do_download(bot, accid, msg, video_id: str, download_type: str):
    """Actual download + send logic."""
    chat_id = msg.chat_id
    req_msg_id = msg.id
    
    # 1. Anti-spam check (per chat)
    last_sent = database.get_last_download(chat_id, video_id, download_type)
    if time.time() - last_sent < ANTI_SPAM_SECONDS:
        _react(bot, accid, req_msg_id, "ℹ️")
        _send(bot, accid, chat_id, "ℹ️ This video was already sent to this chat recently. Scroll up! 👆")
        return

    # 2. Check cache first
    cache_path = _get_cache_path(video_id, download_type)
    if os.path.exists(cache_path):
        # Update file timestamp to keep it in cache
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
    async with _download_locks[video_id + download_type]:
        # Check cache again after getting lock
        cache_path = _get_cache_path(video_id, download_type)
        if os.path.exists(cache_path):
            await _send_from_cache(bot, accid, msg, video_id, download_type, cache_path, info)
            return

        # ⏳ React: downloading
        _react(bot, accid, req_msg_id, "⏳")

        last_error = None
        for attempt in range(2):  # Try up to 2 times
            tmpdir = tempfile.mkdtemp(prefix="ytbot_")
            try:
                if download_type == "video":
                    filepath, info, error = await _download_video(video_id, tmpdir)
                else:
                    filepath, info, error = await _download_audio(video_id, tmpdir, duration)

                if error:
                    last_error = error
                    # Retry only for "file not found" errors
                    if "file not found" in error.lower() and attempt == 0:
                        logger.warning(f"Attempt {attempt+1} failed for {video_id}: {error}, retrying...")
                        continue
                    _react(bot, accid, req_msg_id, "❌")
                    _send(bot, accid, chat_id, f"❌ {error}")
                    return

                if not filepath or not os.path.exists(filepath):
                    last_error = "Download failed: file not found"
                    if attempt == 0:
                        logger.warning(f"Attempt {attempt+1}: file not found for {video_id}, retrying...")
                        continue
                    _react(bot, accid, req_msg_id, "❌")
                    _send(bot, accid, chat_id, f"❌ {last_error}")
                    return

                # Move to cache
                os.makedirs(CACHE_DIR, exist_ok=True)
                shutil.move(filepath, cache_path)
                
                await _send_from_cache(bot, accid, msg, video_id, download_type, cache_path, info)
                return  # Success!

            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        # All attempts failed
        _react(bot, accid, req_msg_id, "❌")
        _send(bot, accid, chat_id, f"❌ {last_error or 'Download failed after retry'}")

        # Cleanup lock
        if video_id + download_type in _download_locks:
            del _download_locks[video_id + download_type]


async def _send_from_cache(bot, accid, msg, video_id, download_type, filepath, info=None):
    """Send a file from the cache to the chat."""
    chat_id = msg.chat_id
    req_msg_id = msg.id
    
    # ⌛ React: sending
    _react(bot, accid, req_msg_id, "⌛")

    if not info:
        # If we don't have info (it was a cache hit), try to get it quickly
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

    # ☑️ React: done
    _react(bot, accid, req_msg_id, "☑️")

    # Record in DB
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

    # Start download in background thread
    t = threading.Thread(target=_run_download, args=(bot, accid, msg, video_id, download_type), daemon=True)
    t.start()


# ── Delta Chat command handlers ──

@dc_cli.on(events.NewMessage(command="/yt"))
def yt_command(bot, accid, event):
    """Download YouTube video."""
    _handle_download_command(bot, accid, event, "video", event.payload.strip())


@dc_cli.on(events.NewMessage(command="/ytm"))
def ytm_command(bot, accid, event):
    """Download YouTube audio."""
    _handle_download_command(bot, accid, event, "audio", event.payload.strip())


@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    help_text = _get_help_text(bot, accid, msg.from_id)
    _send(bot, accid, msg.chat_id, help_text)


@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    msg = event.msg
    s = database.get_stats()
    videos = s["by_type"].get("video", 0)
    audios = s["by_type"].get("audio", 0)
    reply = (
        f"📊 **YT Bot Statistics**\n\n"
        f"Total downloads: {s['total']} ({videos} video, {audios} audio)\n"
        f"Last 24h: {s['last_24h']}\n"
        f"Total data: {_format_size(s['total_size'])}"
    )
    _send(bot, accid, msg.chat_id, reply)


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
        f"/yt <url> — Download video (MP4 480p, ≤50MB)\n"
        f"/yt_<video_id> — Download video by ID\n"
        f"/ytm <url> — Download audio (MP3 128kbps)\n"
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
    if msg.is_info:
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

    audio_fmt = "MP3" if duration <= 1800 else "Opus"
    
    can_video = duration <= MAX_DURATION_VIDEO
    can_audio = duration <= MAX_DURATION_AUDIO

    lines = [f"📺 YouTube: \"{title}\" ({dur_str})", ""]
    
    if can_video:
        lines.append(f"Download video 480p: /yt_{video_id}")
    else:
        lines.append(f"⚠️ Video too long (> {MAX_DURATION_VIDEO // 60}m)")
        
    if can_audio:
        lines.append(f"Download audio {audio_fmt}: /ytm_{video_id}")
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
        dc_accid = accid
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
