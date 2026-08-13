# Delta Chat YouTube Bot

A simple Delta Chat bot that downloads YouTube videos and audio via `yt-dlp`. Designed to stay within email delivery limits (50 MB) and ensure maximum compatibility across all platforms.

## Features

- **Multi-Service Support:** Downloads from YouTube, **Yandex Music, PeerTube, Rutube, Dzen, OK.ru, Coub, Pinterest, SoundCloud, Imgur, Facebook, Vimeo, VK, vkvideo.ru, Twitter, Reddit, Instagram, TikTok, Twitch, Bilibili** and more via `yt-dlp`.
- **Automatic Transport Failover:** Automatically detects message delivery failures via raw core events, switches the primary active transport to a backup relay in round-robin fashion, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread (up to a maximum of 10 attempts per message) to prevent infinite retry loops and CPU spikes.
- **Yandex Preview Resolution:** Automatically resolves Yandex Video Preview links (`yandex.ru/video/preview/...`) to their underlying source video links (e.g. Rutube, YouTube, etc.) and processes them, preserving original timestamp parameters (`?t=...`).
- **Video/Audio Trimming:** Automatically trims downloaded video and audio tracks based on start time parameters (e.g., `?t=51`, `?t=1m20s`, or `&start=80`) present in the URL, downloading and sending only the requested section.
- **Video Downloads & Automatic Chunking (`/yt`):** Downloads video in MP4 with automatic multi-resolution fallback. Videos longer than 10 minutes are automatically offered in 10-minute chunks (`/yt_ID_0_600`) to guarantee delivery within 50 MB size limits, with clickable `▶️ Next chunk` links included in each delivered file's caption.
- **Audio Downloads (`/ytm`):** Extracts audio as high-quality Opus with embedded metadata tags (Title, Artist, Album, Cover Art, embedded plain-text `LYRICS`) and the original video URL embedded in the file's description/comment metadata tag. Optimized to skip re-encoding for short clips to preserve original quality.
- **Subtitles & Lyrics Extraction:** Automatically downloads official or auto-generated YouTube subtitles, embeds clean plain-text lyrics into the audio file, and generates synchronized `.lrc` karaoke files.
- **Navidrome / Subsonic Integration (`/ytms`):** Admin command to save downloaded tagged audio directly into a Navidrome music directory along with synchronized `.lrc` companion files for karaoke lyrics display, and immediately trigger a Subsonic REST API library scan (`startScan.view`).
- **YouTube Music Optimization:** Automatically hides the video download button (`/yt`) for `music.youtube.com` links to treat them as audio-only. Automatically retries downloads and metadata fetches in cookie-less guest mode if cookie-based attempts fail (e.g., due to PO Token blocks or expired cookies).
- **Auto-Detection:** Automatically detects links in chat and provides download options with **video thumbnails** and estimated file sizes.
- **Fast Commands:** Use `/yt_VIDEOID` or `/ytm_VIDEOID` (for YouTube) or `/yt URL` (generic) for quick downloads.
- **Visual Progress:** Uses message reactions to show status:
  - ⏳ : Downloading started.
  - ⌛ : Downloaded, sending to chat.
  - ☑️ : Sent successfully.
  - ❌ : Error occurred.
  - ℹ️ : Already sent recently (Anti-spam).
- **Smart Limits & Security:**
  - Maximum video duration: 30 minutes.
  - Maximum audio duration: 60 minutes.
  - Maximum file size: **50 MB** (safe for email delivery after Base64 encoding).
  - Rate limiting: 1 request per minute (admin exempt).
  - Global download queue: Max 5 concurrent downloads.
  - Handler-specific debouncing: Prevents race conditions and duplicate sends.
  - Smart Caching: Files are kept for 24 hours (max 2 GB) using safe MD5 hashes for URLs.
  - Anti-Spam: Prevents sending the same video to the same chat more than once every 10 minutes.
  - Disk Monitoring: Blocks downloads if server disk space is below 10%; warns admin at 20%.

## Commands

- `/yt <url>` - Download video from URL.
- `/yt_<video_id>` - Download video by ID.
- `/ytm <url>` - Download audio from URL.
- `/ytm_<video_id>` - Download audio by ID.
- `/ytms <url>` - Save audio to Navidrome library via Subsonic API (Admin only).
- `/ytms_<video_id>` - Save audio to Navidrome by ID (Admin only).
- `/stats` - View bot usage statistics.
- `/help` - Show help message.
- `/initadmin` - Claim bot ownership (first time setup).
- `/donate` - Support the project.
- `/transports` - Show configured mail relays & stats (Admin only).
- `/addtransport` - Add a backup mail relay (Admin only).
- `/rmtransport <addr>` - Remove a mail relay (Admin only).
- `/setprimary <addr>` - Switch the primary mail relay (Admin only).
- `/resilient` - Toggle resilient sending mode across all relays (Admin only).

### Target-Specific Commands in Group Chats

In group chats where multiple bots are present, you can address this bot specifically to prevent other bots from responding. Append the `@yt` suffix to any command, for example:
- `/help@yt`
- `/stats@yt`

## Deployment

### Prerequisites

- Docker and Docker Compose
- A Delta Chat account for the bot

### Setup

1. Clone this repository.
2. Build the container:

   ```bash
   docker compose build
   ```

3. Initialize the Delta Chat account:

   ```bash
   docker compose run --rm yt_bot python bot.py init bot-email@chatmail-example.com your_password
   ```

4. Start the bot:

   ```bash
   docker compose up -d
   ```

5. Check the logs to get the QR code or link to add the bot:

   ```bash
   docker compose logs -f
   ```

6. Add the bot in Delta Chat and send `/initadmin` to claim ownership.

## Cookies, OAuth Token & Proxy (Age-Restricted & Yandex Music Content)

Some contents (such as age-restricted/VEVO videos, or Yandex Music tracks) require authentication or a premium subscription.

> [!IMPORTANT]
> **Yandex Music API Changes:** Yandex has permanently deprecated web-based session endpoints for downloading tracks. As a result, standard browser cookies are no longer sufficient to download tracks from Yandex Music. You **must** configure a Yandex OAuth token (`YANDEX_TOKEN`) instead.

### 1. Set Up Yandex Music OAuth Token
To authenticate Yandex Music downloads:
1. Run the interactive token generator tool inside Docker:
   ```bash
   docker compose run --rm yt_bot python get_token.py
   ```
2. Open the URL printed by the script in your web browser (make sure you are logged in to Yandex with a Plus subscription).
3. Enter the code shown in the terminal.
4. Copy the generated token and save it to your `.env` file:
   ```env
   YANDEX_TOKEN=your_oauth_token_here
   ```

### 2. Set Up YouTube Cookies
Export cookies from your browser (using the **"Get cookies.txt LOCALLY"** browser extension in **Netscape** format) while logged into YouTube. Save this file to the bot's data directory to download age-restricted or VEVO videos:
```bash
cp cookies.txt ~/deltachat_yt/data/
```
The bot will load these cookies automatically on startup.

### 3. Verify Yandex Music Status
You can verify if the bot successfully logs in and has an active Yandex Plus subscription using the included diagnostic script:
- Run on host: `python3 check_yandex.py`
- Run inside Docker: `docker compose run --rm yt_bot python check_yandex.py`

### 4. Proxy Configuration (Bypass Yandex Geoblocking)
Since Yandex Music is geoblocked outside Russia/CIS (returning "This page is no longer available" or CAPTCHAs to datacenter/foreign IPs), you will need a proxy to download Yandex Music tracks from foreign servers.

You can configure proxies in a `.env` file in the project directory:
```env
# Global proxy for all downloads (YouTube, SoundCloud, etc.)
PROXY=socks5://user:password@ip:port

# Yandex-specific proxy (Only routes Yandex Music requests through this proxy, keeping YouTube downloads fast and direct)
YANDEX_PROXY=http://user:password@ru_proxy_ip:port

# Backup proxy (Used as a fallback for YouTube/other downloads if primary connection/cookies fail)
BACKUP_PROXY=http://user:password@backup_proxy_ip:port
```

## Navidrome / Subsonic Integration (`/ytms`)

The bot can save downloaded audio tracks (with all embedded tags and cover art) directly into your Navidrome music library and automatically trigger an instant library scan via the Subsonic REST API.

### 1. Configuration (`.env`)

Add your Navidrome connection details to `.env`. You can use either your plain password **or** a precomputed Subsonic MD5 token and salt (so your actual password is never stored):

**Option A: Using password**
```env
# Navidrome / Subsonic Server URL
NAVIDROME_URL=https://music.example.com

# Navidrome User & Password
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=your_navidrome_password

# Target Music Folder inside the container (default: /music)
NAVIDROME_MUSIC_DIR=/music
```

**Option B: Using precomputed token + salt (no plaintext password)**
```env
# Navidrome / Subsonic Server URL
NAVIDROME_URL=https://music.example.com

# Navidrome User & Token
NAVIDROME_USER=admin
NAVIDROME_TOKEN=your_precomputed_md5_token
NAVIDROME_SALT=your_custom_salt

# Target Music Folder inside the container (default: /music)
NAVIDROME_MUSIC_DIR=/music
```

> [!TIP]
> **Generate Token & Salt:**
> To calculate `NAVIDROME_TOKEN` from your password without saving the password:
> ```bash
> python3 -c "import hashlib, secrets; salt = secrets.token_hex(8); pwd = input('Enter Navidrome password: '); token = hashlib.md5((pwd + salt).encode()).hexdigest(); print(f'NAVIDROME_SALT={salt}\nNAVIDROME_TOKEN={token}')"
> ```

### 2. Music Directory Mount (`docker-compose.override.yml`)

To mount your host's Navidrome music directory into the bot container without editing the tracked `docker-compose.yml`, copy the example override file:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

Edit `docker-compose.override.yml` to specify your music path:

```yaml
services:
  yt_bot:
    volumes:
      - /path/to/navidrome/music:/music
```

Then restart the container:

```bash
docker compose up -d
```

## Admin Management

You can also manage the administrator via the CLI:

```bash
docker compose exec yt_bot python set_admin.py --email your@email.com
```

## Support

If you find this bot useful, consider supporting the developer:

- [Ko-fi](https://ko-fi.com/gluek)
- [Tribute](https://web.tribute.tg/d/IWb)
