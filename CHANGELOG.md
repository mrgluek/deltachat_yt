# Changelog

All notable changes to this project will be documented in this file.

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
