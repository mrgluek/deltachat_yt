# Delta Chat YouTube Bot

A simple Delta Chat bot that downloads YouTube videos and audio via `yt-dlp`. Designed to stay within email delivery limits (30 MB) and ensure maximum compatibility across all platforms.

## Features

- **Multi-Service Support:** Downloads from YouTube, **PeerTube, Rutube, Dzen, OK.ru, Coub, Pinterest, SoundCloud, Imgur, Facebook, Vimeo, VK, vkvideo.ru, Twitter, Reddit, Instagram, TikTok, Twitch, Bilibili** and more via `yt-dlp`.
- **Video Downloads (`/yt`):** Downloads video in MP4 (H.264 + AAC) at **360p or 480p** (automatically uses 360p for videos over 10 minutes to stay within size limits).
- **Audio Downloads (`/ytm`):** Extracts audio as high-quality Opus. Optimized to skip re-encoding for short clips to preserve original quality.
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
  - Maximum file size: **30 MB** (safe for email delivery after Base64 encoding).
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
- `/stats` - View bot usage statistics.
- `/help` - Show help message.
- `/initadmin` - Claim bot ownership (first time setup).
- `/donate` - Support the project.

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

## Cookies (Age-Restricted & VEVO Content)

To download age-restricted or VEVO-locked videos, place a valid YouTube `cookies.txt` (Netscape format) in the `data/` directory:

```bash
cp cookies.txt ~/deltachat_yt/data/
```

Export cookies using the **"Get cookies.txt LOCALLY"** browser extension. Use a dedicated Google account — not your primary one. The bot will detect and use the file automatically on every request.

## Admin Management

You can also manage the administrator via the CLI:

```bash
docker compose exec yt_bot python set_admin.py --email your@email.com
```

## Support

If you find this bot useful, consider supporting the developer:

- [Ko-fi](https://ko-fi.com/gluek)
- [Tribute](https://web.tribute.tg/d/IWb)
