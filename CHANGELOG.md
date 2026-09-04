# Changelog

All notable changes to this project will be documented in this file.

## [1.6.51] - 2026-09-04

### Improved
- **Instagram Photo Post Fast-Exit & Reaction Cleanup:**
  - Added fast-failure detection for Instagram photo posts when `yt-dlp` returns `There is no video in this post`, immediately aborting unnecessary retry cycles across backup proxy and cookie configurations.
  - Automatically clears the bot reaction (`🤖`) on photo posts without sending error messages, enabling clean coexistence with WebPreview bot in shared chats.
  - Added short domain `instagr.am/` support to `SUPPORTED_URL_RE`.

## [1.6.50] - 2026-08-26

### Fixed
- **Prevent Double Trimming of Video Sections:**
  - Removed redundant local `ffmpeg` re-trimming on video sections downloaded via `yt-dlp --download-sections`, which previously caused seeking past EOF on the already-sliced output file and produced empty 175 KB remnant files.

## [1.6.49] - 2026-08-25

### Fixed
- **Reliable Thumbnail Downloading and Caching:**
  - Added `_download_thumbnail` with proper browser `User-Agent` and automatic directory creation (`data/thumbnails/`).
  - Added automatic re-fetching of missing thumbnail images when serving cached video metadata.

## [1.6.48] - 2026-08-25

### Fixed
- **Preserve Pristine Base Audio Container for Slicing:**
  - Avoided Mutagen metadata and lyrics tagging on the raw base audio file when downloading for slicing (`for_slicing=True`). Mutagen atom rewrites on YouTube raw M4A containers corrupt chunk offsets (`stco`/`co64`), causing FFmpeg transcode and stream-copy failures (`Conversion failed!`).

## [1.6.47] - 2026-08-25

### Fixed
- **Purge Corrupt Base File on Trimming Failure:**
  - Re-introduced auto-deletion of corrupt `cached_base` on trimming failure so that fallback configurations can re-download a fresh clean base audio stream instead of retrying against broken cache.

## [1.6.46] - 2026-08-25

### Fixed
- **Video Section Slicing with Progressive Streams:**
  - Prioritized format `18` (360p progressive MP4) and `android,web` player client when downloading video sections (`--download-sections`), preventing FFmpeg 403 HTTP Range rejection errors on adaptive separate streams.

## [1.6.45] - 2026-08-25

### Fixed
- **Accurate Audio Seeking in FFmpeg:**
  - Shifted `-ss` seek position parameter after `-i` input flag in both stream-copy and transcoding commands to avoid container demuxer timestamp errors across different FFmpeg distributions.

## [1.6.44] - 2026-08-25

### Fixed
- **FFmpeg Trimming on Chapterless Files:**
  - Removed obsolete `-map_chapters -1` flag which caused FFmpeg 7.1+ to fail with exit code 234 (`EINVAL`) when slicing files without embedded chapter tracks.
  - Simplified and cleaned error logging to prevent raw multi-line FFmpeg build banners in chat and logs.

## [1.6.43] - 2026-08-25

### Fixed
- **Purge Corrupt Cached Base Audio on Slicing Failure:**
  - Added auto-deletion of base audio files in `data/cache/` when ffmpeg fails to slice/transcode from them.
  - Enhanced error logging with ffmpeg stderr outputs to easily diagnose slicing issues and ensure fallback configurations re-download fresh uncorrupted base streams.

## [1.6.42] - 2026-08-25

### Fixed
- **0 KB Corrupt Cache Auto-Purge and Validation:**
  - Added strict non-empty (`size > 0`) checks across cache finders (`_find_cached_file`), audio download collectors (`_find_file_in_dir`), and Navidrome saver (`_save_to_navidrome`).
  - Corrupt or 0-byte cached files on disk are automatically deleted and bypassed so tracks are re-sliced cleanly from valid sources instead of serving empty files.

## [1.6.41] - 2026-08-25

### Added
- **Tracklist Extraction from YouTube Comments & Linked Comments (`lc=`):**
  - Added automatic tracklist parsing from pinned, top, and linked YouTube comments (`_extract_chapters_from_comments`) when a video lacks embedded chapters or description timestamps.
  - Preserved `lc=` (linked comment) query parameter in YouTube URLs so linked comment tracklists are automatically detected, parsed, and converted into sliced track buttons (`/ytms_...`, `/ytm_...`, `/yt_...`).

## [1.6.40] - 2026-08-25

### Fixed
- **Base Audio Slicing Cache Retention:**
  - Removed 50 MB file size limit deletion when downloading base mix audio for slicing (`for_slicing=True`).
  - Fixes loop where long DJ mixes (>50 MB) were downloaded, tagged, and then deleted by the email attachment size checker before slicing could occur.

## [1.6.39] - 2026-08-25

### Fixed
- **Chapter Track Slicing & Chapter Strip (`-map_chapters -1`):**
  - Fixed `start_time = 0` evaluation check in ffmpeg trim commands (`if start_time is not None:`) so track 1 is properly trimmed from 0 seconds instead of skipping the `-ss` argument.
  - Added `-map_chapters -1` to ffmpeg audio and video trimming commands so sliced tracks do not retain the 20+ chapter markers of the entire mix.
  - Added automatic audio transcoding fallback (`-c:a libopus / aac`) if stream copy trimming encounters codec container mismatches.
  - Prevented fallthrough to downloading the full 1-hour audio mix when section slicing is requested.

## [1.6.38] - 2026-08-21

### Fixed
- **Defined `out_template` and `safe_id` in `_download_video`:**
  - Fixed `NameError: name 'out_template' is not defined` during video downloads (e.g. Rutube or YouTube video links).

## [1.6.37] - 2026-08-20

### Fixed
- **Album Organization and Lyrics Slicing for Chapters:**
  - Fixed Album tag resolution for sliced chapters to consistently use the video/mix album title instead of falling back to the chapter track title.
  - Added timestamp-adjusted synchronized `.lrc` lyrics slicing (`_slice_lrc`) for chapters, ensuring every track slice in Navidrome gets its corresponding `.lrc` companion file and embedded lyrics.

## [1.6.36] - 2026-08-20

### Improved
- **Original Stereo Audio Stream Preservation for Slicing:**
  - Base audio downloaded for track slicing is now always preserved in its native original stereo stream format (`.m4a` AAC stereo or `.opus` Opus stereo) without mono compression downsampling.
  - Slices inherit the untouched original YouTube audio stream quality bit-for-bit.

## [1.6.35] - 2026-08-20

### Fixed
- **Optimized Full Base Audio Caching for Album / Mix Slicing:**
  - Full audio is cached locally upon first track request, allowing all subsequent tracks to slice instantly in 0.03 seconds via local ffmpeg.
  - Fixes ffmpeg Range seeking issues (code 251 / 152 / Invalid argument) caused by YouTube CDN stream interruptions on remote sections.
  - Automatically handles extended album/mix durations up to 120 minutes during slicing without triggering standard duration filters.

## [1.6.34] - 2026-08-20

### Fixed
- **Seamless Local Trim Fallback for Slices:**
  - Added automatic fallback to local ffmpeg slicing from cached/downloaded base audio or video if remote HTTP range slicing fails (e.g. ffmpeg 152 / 403 stream drops).
  - Ensures 100% reliable track and chapter slicing across all tracks in long mixes.

## [1.6.33] - 2026-08-20

### Fixed
- **Mobile Client Fallback for Chapter/Section Downloads:**
  - Configured `yt-dlp` to automatically use `player_client=android,ios,web` when downloading section slices (`start_time` / `end_time`).
  - Added detection of ffmpeg external downloader exit codes (`ERROR: ffmpeg exited with code...`) to automatically trigger mobile player client retries, preventing 403 Forbidden errors when ffmpeg accesses YouTube media URLs directly.

## [1.6.32] - 2026-08-20

### Added
- **Track & Chapter-Based Slicing:**
  - Automatically parses YouTube chapters from video metadata and description tracklists (supporting standard timestamps, bracketed timestamps, and inline markdown links).
  - Previews now offer Track 1 (with exact chapter boundaries and track title) instead of arbitrary 10-minute blocks when chapters are present.
  - Video and audio downloads embed track title, album, artist, track number, and total track count into audio tags (ID3, Vorbis, MP4, FLAC).
  - Fast partial downloads with `yt-dlp --download-sections "*start-end"` via HTTP range requests, downloading only the requested track bytes without full video stream downloads or transcoding.
- **Navidrome (`/ytms`) Chapter Integration:**
  - Saving chapter slices via `/ytms` organizes files with the track's title (e.g. `<Artist>/<Album>/<Track Title>.<ext>`) with full metadata tags.
  - Slices from long videos/mixes (> 60 min) are allowed based on the track's effective duration.
- **1-Tap Next Track Navigation:**
  - After downloading or saving a track, the bot automatically offers the command to download or save the next track in the playlist.

## [1.6.31] - 2026-08-19

### Fixed
- **Broadened Format Selection & Mobile Muxed Audio Fallback:**
  - Updated audio format selector to `ba[acodec=opus]/ba[ext=m4a]/ba/b/best` and video selector to `bv*+ba/b/best` so `yt-dlp` seamlessly extracts audio from muxed video streams (`b` / `best`) when standalone audio (`ba`) is not offered by mobile YouTube clients.
  - Added automatic mobile player client retry upon encountering `Requested format is not available` or `HTTP 403 Forbidden`.

## [1.6.30] - 2026-08-18

### Fixed
- **Conditional Mobile Player Client on 403 Forbidden:**
  - Standardized `yt-dlp` to run with normal default client configurations for all standard videos and playlists to prevent regressions.
  - Added automatic targeted fallback to `player_client=android,ios,web` specifically upon encountering `HTTP Error 403: Forbidden`, seamlessly bypassing VEVO/SABR restrictions on music without affecting normal videos.

## [1.6.29] - 2026-08-18

### Fixed
- **Multi-Client YouTube Player Extraction (`player_client=android,ios,web`):**
  - Added `--extractor-args "youtube:player_client=android,ios,web"` across info extraction, video, and audio downloads to bypass strict SABR Web PO-Token / HTTP 403 Forbidden blocks on music and VEVO tracks during guest/proxy downloads.

## [1.6.28] - 2026-08-18

### Fixed
- **CLI Options for `yt-dlp` Error Toleration:**
  - Replaced incorrect `--compat-options no-abort-on-error` with standalone flags `--no-abort-on-error` and `--ignore-errors` in `_download_video` and `_download_audio`.

## [1.6.27] - 2026-08-18

### Fixed
- **Subtitles Rate Limiting & Non-Fatal Download Toleration:**
  - Narrowed subtitle language filter from `all,-live_chat` to `en.*,ru.*,orig,-live_chat` in audio downloads to prevent YouTube HTTP 429 Too Many Requests rate-limiting across 100+ languages.
  - Added `--compat-options no-abort-on-error` to both audio and video downloads to ensure that subtitle fetching or other non-fatal warnings do not abort the media download.

## [1.6.26] - 2026-08-18

### Fixed
- **Automatic In-Place `cookies.txt` Sanitization (`_sanitize_cookies_file`):**
  - Added in-place sanitization for `data/cookies.txt` before invoking `yt-dlp` or loading cookies, ensuring that missing `# Netscape HTTP Cookie File` headers, malformed domain flags, and multi-space whitespace variations are automatically normalized on disk so `yt-dlp` never crashes with `CookieLoadError` / `invalid Netscape format`.

## [1.6.25] - 2026-08-18

### Fixed
- **Robust Netscape Cookie Parsing (`_load_cookiejar`):**
  - Added custom `_load_cookiejar` helper that normalizes `domain_specified` flags and handles `#HttpOnly_` prefixes to prevent Python `http.cookiejar.MozillaCookieJar` standard library `AssertionError` (`assert domain_specified == initial_dot`) on modern browser cookie exports.

## [1.6.24] - 2026-08-18

### Added
- **YouTube & Yandex Music Account Status Diagnostics in `/stats`:**
  - Added real-time account status checks for **YouTube** (`▶️ **YouTube:**`) and **Yandex Music** (`🎵 **Yandex Music:**`) in the admin section of `/stats` right above Navidrome.
  - Verifies YouTube session authentication against `data/cookies.txt`, detecting active login sessions, user/channel handles, expired cookies, and YouTube bot-detection challenges ("The page needs to be reloaded").
  - Verifies Yandex Music status via `YANDEX_TOKEN` or `data/cookies.txt`, checking subscription status and account display names.
- **Startup Account Diagnostics:**
  - Automatically verifies and logs YouTube and Yandex Music account status during startup.
- **Improved Error Messages:**
  - Added user-friendly formatting in `_clean_error` for YouTube "The page needs to be reloaded" (expired/flagged cookies) and HTTP 403 Forbidden errors.

## [1.6.23] - 2026-08-14

### Fixed
- **Yandex Music & Audio Metadata Tagging:** Added `_tag_audio_file` using `mutagen` to embed ID3v2.3 tags (`TIT2` Title, `TPE1` Artist, `TPE2` Album Artist, `TALB` Album, `TDRC` Year, `APIC` Cover Art, `USLT` Lyrics, and URL comments) into MP3 downloads from Yandex Music and ensure full metadata tagging across all formats.
- **Yandex Music Album & Lyrics Extraction:** Updated `_fetch_yandex_metadata` to extract album title (`track.albums`), release year, and track lyrics from Yandex Music API.

## [1.6.22] - 2026-08-14

### Added
- **Subtitles & Lyrics Extraction:** Automatically downloads official or auto-generated YouTube subtitles (`--write-subs`, `--write-auto-subs`, `--convert-subs lrc`).
- **Audio Metadata Lyrics Embedding:** Parses clean plain-text lyrics and embeds them into audio metadata tags (`LYRICS` / `UNSYNCEDLYRICS` Vorbis comments for Opus/FLAC, `USLT` for MP3, `©lyr` for MP4/M4A) via `mutagen`.
- **Synchronized Karaoke `.lrc` for Navidrome:** Automatically converts subtitles to synchronized `.lrc` format and saves `<Track Title>.lrc` alongside the audio file in the Navidrome library directory on `/ytms`.
- **Navidrome Diagnostics & Connectivity Status:** Checks and displays Navidrome connection status in startup logs, `/help` (admin), and `/stats` (admin).
- **Network Timeouts & Failover in `update.sh`:** Added connection timeouts, low-speed checks, and automatic fallback to Forgejo mirror.

## [1.6.21] - 2026-08-13

### Added
- **Navidrome / Subsonic Integration (`/ytms`):** Added administrator command `/ytms <url>` and `/ytms_<id>` to save downloaded tagged audio files directly into the Navidrome music directory organized as `<Artist>/<Album>/<Track Title>.<ext>`.
- **Subsonic REST API Library Scan:** Automatically triggers Navidrome to scan its music library immediately after saving new files via `/rest/startScan.view` using token+salt MD5 authentication.
- **Link Preview Navidrome Button:** Adds a 1-tap `💾 /ytms_<id>` button to link preview messages for bot administrators.
- **Docker Compose Override Example:** Added `docker-compose.override.yml.example` to easily mount host music directories into the container.

## [1.6.20] - 2026-08-13

### Added
- **Audio File Metadata & Tagging:** Configured `yt-dlp` to automatically embed tags (Title, Artist, Album, Cover Art) into audio files (`--embed-metadata` and `--embed-thumbnail`).
- **Original Video Link Embedding:** Embeds original video webpage URL into audio file metadata tags (`description` and `comment` fields via `--parse-metadata`), preserving original link in Delta Chat captions.

## [1.6.19] - 2026-07-27

### Changed
- **Increased File Size Limit to 50 MB:** Raised maximum file size limit from 30 MB to 50 MB (`MAX_FILESIZE_MB = 50`) to provide extra headroom for high-bitrate 10-minute video chunks and prevent size limit aborts due to encoding variance.

## [1.6.18] - 2026-07-27

### Added
- **Automatic 10-Minute Video Chunking (`/yt_ID_0_600`):** Videos longer than 10 minutes are automatically offered in 10-minute segments (00:00–10:00, 10:00–20:00, etc.) using clean, 100% clickable Delta Chat command links (`/yt_ID_START_END`).
- **Sequential Next Chunk Delivery Links:** Delivered video chunk captions automatically include a clickable `▶️ Next chunk (start-end): /yt_ID_START_END` command link to fetch subsequent parts seamlessly until the end of the video.

## [1.6.17] - 2026-07-26

### Fixed
- **Fix `Requested format is not available` Error:** Removed hardcoded `player_client=android,web` extractor argument that caused YouTube's SABR experiment to hide adaptive DASH streams and fail on videos like `ssmQkRkXE84`.
- **Flexible Format Selection & Multi-Resolution Fallback:** Updated format selector to `bv[height<={max_height}]+ba/b[height<={max_height}]/b` with `--merge-output-format mp4` and implemented automatic step-down resolution fallback (`480p` ➔ `360p` ➔ `240p` ➔ `144p`) when file sizes exceed limits or formats are filtered out.
- **Informative Size Limit Feedback:** Suggests downloading the audio version (`/ytm_<id>`) if even 144p resolution exceeds 30 MB.

## [1.6.16] - 2026-07-08

### Fixed
- **Fix Incomplete/Partial Download Delivery:** Prevented the bot from matching and delivering unfinished `.part`, `.ytdl`, or `.temp` files.
- **Robust Size Limit Detection:** Correctly detects when `yt-dlp` aborts downloads due to the `--max-filesize` limit (even when returning exit code 0 under `--print-json`) and responds with a descriptive size limit warning instead of sending a partial file or failing with "file not found".

## [1.6.15] - 2026-07-06

### Fixed
- **Fix Dependency Conflict/NameError:** Pinned `deltabot-cli==8.1.2` and `deltachat2[full]<1.0.0` in `requirements.txt` to resolve dependency conflicts and avoid the `ChatType` NameError/ImportError bugs introduced in newer, incompatible versions of `deltachat2`.

## [1.6.14] - 2026-07-03

### Fixed
- **Zombie Process Reaping:** Enabled `init: true` in Docker Compose to automatically reap zombie processes (like those from `yt-dlp` or RPC calls) in the bot container, preventing PID limit exhaustion.

## [1.6.13] - 2026-06-25

### Changed
- **Compact Preview Format:** Simplified the layout of video/audio information and command buttons, combining the title and the URL into a single Markdown link, and aligning download command options side-by-side.

## [1.6.12] - 2026-06-25

### Changed
- **Bidirectional Suffix Matching:** Suffix matching is now bidirectional (e.g. `@y` or `@ytbot` will match YT bot).
- **Smart Group Chat Command Filtering:** The bot now automatically ignores unaddressed general `/help` and `/stats` commands in group chats if other bots are present in the chat.

## [1.6.11] - 2026-06-25

### Added
- **Target-Specific Command Suffixes:** Added support for addressing this bot specifically in group chats using the `/command@yt` suffix.

## [1.6.10] - 2026-06-24

### Fixed
- **Fallback Support for Link Detection / Previews**:
  - Refactored all metadata fetching calls (`_send_from_cache` and `_handle_link_info`) to use the new `_fetch_video_info_with_fallback` helper function.
  - This ensures that YouTube link auto-detection/preview generation attempts all proxy/cookie fallback configurations (including `BACKUP_PROXY`) instead of failing on the first configuration and outputting country block errors to the chat.

## [1.6.9] - 2026-06-24

### Fixed
- **Detailed Country/Region Block Error Messages**:
  - Enhanced error cleaning logic to parse and extract specific regional availability information from the `yt-dlp` output (e.g. "This video is available in Russian Federation.") and append it to the country block error message sent to the user.

## [1.6.8] - 2026-06-24

### Added
- **Backup Proxy Fallback Support**:
  - Introduced the `BACKUP_PROXY` environment variable for routing download and metadata fetch requests if the default configurations fail.
  - Generalised the download and metadata fetch retry loop into a dynamic multi-stage configuration sequence (default proxy + cookies, default proxy guest mode, backup proxy guest mode, backup proxy + cookies).

## [1.6.7] - 2026-06-24

### Added
- **YouTube Music & Guest Fallback Enhancements**:
  - Added `music.youtube.com` to `AUDIO_ONLY_URL_RE` so the bot hides the video download option (`/yt`) and only offers the audio download option (`/ytm`) for YouTube Music links.
  - Implemented a self-healing automatic fallback to cookie-less (guest) downloading for both metadata fetching and audio/video downloading. If the first download attempt using configured cookies fails (e.g. due to expired cookies or PO Token blocks triggering `403 Forbidden`), the bot will dynamically retry downloading without cookies.
  - Fixed a silent failure in `_download_audio` by validating the `yt-dlp` process return code, ensuring errors are logged and handled correctly rather than failing with a generic "file not found" message.

## [1.6.6] - 2026-06-17

### Added
- **Native Yandex Music Downloader Integration**:
  - Replaced the deprecated and broken web-based Yandex Music extractor in `yt-dlp` (which returned 404 / invalid session errors) with a native Python downloader utilizing the `yandex-music` library.
  - Implemented token-based authentication via the `YANDEX_TOKEN` environment variable.
  - Added the `get_token.py` interactive CLI tool to generate Yandex OAuth tokens via Yandex Device Auth, automatically routed through the proxy to prevent geoblocking.
  - Added OAuth token validation and Yandex Plus subscription status checking during startup and inside the `check_yandex.py` verification script.

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
