# Changelog

All notable changes to this project will be documented in this file.

## [1.6.5] - 2026-06-17

### Added
- **Startup Cookie Verification & Dynamic TLD Rewriting**:
  - The bot now automatically and asynchronously verifies Yandex Music cookies in `data/cookies.txt` on startup across all domains present in the cookies (`yandex.ru`, `yandex.by`, `yandex.kz`, `yandex.uz`, `yandex.com`).
  - Automatically identifies which regional Yandex domain is successfully authenticated and sets it as the active Yandex domain.
  - Dynamically rewrites all incoming Yandex Music URLs to match the active authenticated domain before requesting `yt-dlp` info or downloading, ensuring that regional login cookies are correctly sent and authorized.
- **Yandex-Specific Proxy Configuration**:
  - Added support for the `YANDEX_PROXY` environment variable. When configured, only Yandex Music requests and startup cookie checks are routed through this proxy, while other media sources (like YouTube) continue to download directly (or via the global `PROXY` fallback), preventing unnecessary slow-downs or data costs.

### Fixed
- **User-Friendly Error Formatting**:
  - Downgraded failed `yt-dlp` info fetch and download logs from `ERROR` to `WARNING`.
  - Handled the known `yt-dlp` TypeError crash on failed/blocked Yandex Music requests (`argument of type 'bool' is not iterable`) and replaced it with a clean, descriptive message explaining potential reasons (subscription requirements, region blocks, captcha challenge).

## [1.6.4] - 2026-06-16

### Added
- **Automatic Transport Failover:** Added a robust event-driven transport failover mechanism. The bot now listens to the core's `MSG_FAILED` event. When a message fails to deliver, it automatically rotates to the next configured backup transport, updates `configured_addr`, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread. The failover process is limited to a maximum of 10 attempts per message to prevent infinite loops, and the administrator is alerted only on the first failure.

### Fixed
- **E2E Failover Loop & Key Fallback**:
  - Added fallback support for both `chat_id` and `chatId` keys in message snapshots to prevent `chat 'Unknown' (ID: None)` errors.
  - Downgraded permanent E2E and resend logs to `WARNING`.
  - Removed administrative failover alert messages completely, relying entirely on structured logging to prevent any potential loop risks.


## [1.6.3] - 2026-06-15

### Added
- **Video/Audio Trimming by Timestamp:** Added support for automatically trimming downloads of video and audio tracks based on timestamp parameters (e.g. `?t=51`, `?t=1m20s`, or `&start=80`) present in YouTube (and resolved Yandex Video preview) URLs. The bot will seek and download only the specified range to save server bandwidth and keep files under the 30MB limit.
- **Dynamic Size and Duration Estimation:** Updated the link preview/info box and message captions to calculate and display the remaining duration and scaled size of the trimmed media, rather than the full media length.

### Fixed
- **File Fallback Search with URL IDs:** Fixed fallback directory search logic in `_download_video` when `video_id` is a full URL, preventing "file not found" errors after successful downloads of trimmed clips.

## [1.6.2] - 2026-06-15

### Added
- **Audio-Only Platform Improvements:** Audio-only services (SoundCloud and Yandex Music) now present only the audio download option (`/ytm`), hiding the video button. Any explicit video download command (`/yt`) on these domains is automatically and gracefully routed to audio extraction.
- **Native Audio Downloads (No Transcoding):** Audio tracks under 10 minutes are downloaded and saved in their original native formats without transcoding (preferring native `opus` for YouTube, native `m4a/aac` for SoundCloud and YouTube fallback, and native `mp3` for Yandex Music).

## [1.6.1] - 2026-06-10

### Added
- **Yandex Video Preview Resolution:** Support for automatically resolving Yandex Video Preview links (`yandex.ru/video/preview/...`) to their underlying source video links (e.g. Rutube or YouTube) and downloading them, while keeping original query timestamp parameters (`?t=...`).

## [1.6.0] - 2026-06-05

### Added
- **DPI Bypass Hack:** Integrated a patched `deltachat-rpc-server` binary into the Docker setup to bypass SSL DPI connection blocks when communicating with chatmail.
- **Resilient Sending Mode:** Added `/resilient` admin command to configure resilient mode (accepts `on`/`off`/`1`/`0`/`true`/`false`, or no arguments to query current status). When enabled, each outgoing message is sent through all configured mail relays using resending mechanism in a non-blocking background thread to bypass chatmail blocking issues without causing UI delays, while ensuring deduplication into a single message bubble on the recipient client.

## [1.5.2] - 2026-05-22

### Fixed
- **Command Underscore Separator Stripping:** Fixed a bug in `/yt` and `/ytm` where clicking a generated dynamic link like `/yt_lgW2xTos3hQ` would leave a leading underscore in the extracted video ID, causing yt-dlp to fail with a "Video unavailable" error.

## [1.5.1] - 2026-05-22

### Changed
- Standardized the welcome greeting to return the exact same detailed output as the `/help` command instead of a custom welcome prefix message.

## [1.5.0] - 2026-05-19

### Added

- Added complete set of in-chat transport management commands matching `tgbridge`:
  - `/addtransport <payload>` to dynamically add backup mail relays via chatmail URI or credentials.
  - `/setprimary <addr>` to switch the primary active mail relay (`configured_addr`).
- Upgraded `/transports` command to show connectivity status, primary/backup labels, message counts (sent/received), and last sent/received timestamps.
- Upgraded `/rmtransport <addr>` command with full validation checks and last-transport protection.

## [1.4.1] - 2026-05-18

### Added
- **Yandex Music Support:** Support for downloading tracks and albums from Yandex Music (`music.yandex.ru`, `.com`, `.by`, `.kz`).

## [1.4.0] - 2026-05-12

### Added
- **Age-Restricted & VEVO Support:** Optional `data/cookies.txt` authentication for downloading age-restricted and VEVO-locked videos.
- **Deno JS Runtime:** Added Deno + `yt-dlp[default]` (yt-dlp-ejs) to the Docker image to solve YouTube's n-challenge and fix "Requested format is not available" errors.
- **Dynamic Resolution:** Videos over 10 minutes are automatically downloaded at 360p to stay within the file size limit. Short videos use 480p.
- **Resolution Fallback:** If a video exceeds the 30 MB limit at 480p, the bot automatically retries at 360p.
- **Improved Error Reporting:** Downloads that are silently filtered by yt-dlp now report the actual reason (size/duration/restriction) from stderr.

### Changed
- **File size limit lowered to 30 MB** (from 50 MB) to ensure reliable email delivery after Base64 encoding overhead.
- **Short YouTube links:** Captions now use `youtu.be/ID` format instead of `www.youtube.com/watch?v=ID`.
- Updated help text and info messages to reflect the 30 MB limit and dynamic resolution.
- `_find_file_in_dir` now returns the largest matching file and supports filename prefix filtering for more reliable file detection after download.

## [1.3.0] - 2026-05-05

6: 
7: ### Added
8: - **Multi-Service Support:** Support for PeerTube, Rutube, Vimeo, VK, Twitter, Reddit, Instagram, TikTok, and more.
9: - **Thumbnail Previews:** Info messages now include a video thumbnail preview.
10: - **Generic URL Handling:** The `/yt` and `/ytm` commands now accept any valid URL.
11: 
12: ### Changed
13: - **Video Optimization:** Configured `yt-dlp` to prefer pre-muxed MP4 formats to avoid unnecessary server-side re-encoding.
14: - **Audio Optimization:** Disabled re-encoding for short audio tracks to preserve original quality and save CPU.
15: - **Cache Improvements:** URL-based downloads are now cached using MD5 hashes for safe filesystem storage.
16: 
17: ### Fixed
18: - **Event Shadowing:** Implemented handler-specific debouncing to prevent `on_new_message` from accidentally silencing command handlers.
19: - **Double-Triggering:** Eliminated redundant "Please wait" messages caused by concurrent event processing.
20: - **Path Traversal Safety:** Sanitized cache filenames for URL-based downloads.
21: 
22: ## [1.2.0] - 2026-05-05

### Added
- **Dynamic Opus Strategy:** Switched from MP3 to Opus. High quality (128k stereo) for <= 10m, space-saving (64k mono) for long audio.
- **Disk Monitoring:** Automatic download blocking at 10% free space and admin warnings at 20%.
- **Improved UX:** Added estimated file sizes with `~` prefix in link detection messages.
- **Enhanced Anti-Spam:** Debounced warning messages and fixed duplicate sends caused by client-side double-taps.

### Changed
- Increased maximum audio duration limit to 60 minutes.
- Updated `/stats` command to display real-time disk usage info.

### Fixed
- Memory leak in download lock management.
- Zombie `yt-dlp` processes when download timeouts occur.
- Bug where administrative messages were sometimes processed by multiple threads.

## [1.1.0] - 2026-05-05

### Added
- Smart caching system: files stored in `data/cache` for 24 hours.
- Automatic cache cleanup (2 GB total size limit).
- Download deduplication: concurrent requests for the same video wait for a single download.
- Anti-spam: 10-minute cooldown for the same video in a specific chat.

### Changed
- Increased maximum video duration from 10 to 30 minutes.
- Improved reaction-based progress tracking.

## [1.0.0] - 2026-05-05

### Added
- Initial release of Delta Chat YouTube Bot.
- Support for video downloads in MP4 (480p, H.264).
- Support for audio downloads in MP3 (128kbps).
- YouTube link auto-detection with quick-download buttons.
- Reaction-based progress tracking (⏳, ⌛, ☑️, ❌).
- Rate limiting (1 req/min) and global download queue (max 5 concurrent).
- Video duration limit of 10 minutes.
- Admin system based on email and cryptographic fingerprints.
- Download statistics command `/stats`.
- Automatic cleanup of temporary files.
- Docker and Docker Compose deployment support.
