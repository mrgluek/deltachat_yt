# Changelog

All notable changes to this project will be documented in this file.

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
