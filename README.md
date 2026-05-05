# Delta Chat YouTube Bot

A simple Delta Chat bot that downloads YouTube videos and audio via `yt-dlp`. Designed to stay within Delta Chat's 50MB file limit and ensure maximum compatibility across all platforms.

## Features

- **Video Downloads (`/yt`):** Downloads video in MP4 (H.264 + AAC) at 480p. Optimized for inline playback on Android, iOS, and Desktop.
- **Audio Downloads (`/ytm`):** Extracts audio as MP3 (128kbps).
- **Fast Commands:** Use `/yt_VIDEOID` or `/ytm_VIDEOID` for quick downloads.
- **Auto-Detection:** Automatically detects YouTube links in chat and provides download options.
- **Visual Progress:** Uses message reactions to show status:
  - ⏳ : Downloading started.
  - ⌛ : Downloaded, sending to chat.
  - ☑️ : Sent successfully.
  - ❌ : Error occurred.
- **Smart Limits:**
  - Maximum video duration: 10 minutes.
  - Maximum file size: 50 MB.
  - Rate limiting: 1 request per minute (admin exempt).
  - Global download queue: Max 5 concurrent downloads.

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

## Admin Management

You can also manage the administrator via the CLI:

```bash
docker compose exec yt_bot python set_admin.py --email your@email.com
```

## Support

If you find this bot useful, consider supporting the developer:

- [Ko-fi](https://ko-fi.com/gluek)
- [Tribute](https://web.tribute.tg/d/IWb)
