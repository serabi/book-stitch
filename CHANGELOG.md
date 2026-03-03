# Changelog

<!-- markdownlint-disable MD024 -->

All notable changes to Book Stitch will be documented in this file.

## [1.0.2] - 2026-03-02

### Changed

- **Hot-reload settings without server restart** — Saving settings now applies changes in-process instead of restarting the container. Handles LOG_LEVEL reconfiguration, SYNC_PERIOD_MINS rescheduling, and ABS Socket.IO listener start/stop/restart.
- **KoSync password Show/Hide toggle** — The KoSync password field now has a Show/Hide button that fetches the saved value on demand, so you can see the password you need to retype in KOReader without exposing it in the page source.
- **Save Settings button right-aligned** — The floating Save bar now aligns the button to the right.

### Fixed

- **Settings tab resets to General on save** — The active tab is now preserved across saves via a hidden form field.
- **Navbar icons persisting for disabled services** — Service icons are now gated on their `*_ENABLED` flags.

### Removed

- **Shelfmark integration** — Removed the Shelfmark iframe view, navbar icon, settings UI, and all associated configuration (`SHELFMARK_URL`, `SHELFMARK_ENABLED`).

---

## [1.0.1] - 2026-03-02

### Fixed

- **Security: API token no longer sent as URL query parameter** — The cover proxy now passes the ABS token via `Authorization: Bearer` header instead of `?token=` in the URL, preventing it from leaking into logs, browser history, and referrer headers.
- **Security: Secrets removed from settings HTML** — Password and API token fields no longer emit their stored value into the DOM. Fields show an `(unchanged)` placeholder when a value is set, and the backend preserves existing secrets when the field is submitted empty.
- **Docker test entrypoint** — Fixed `sh -c` argument handling in `docker-compose.test.yml` that leaked the shell name (`sh`) as a stray pytest argument when passing flags via `run-tests.sh`.
- **Sync engine: `client_pct` null safety** — `client_pct` is now sanitized after reading from client state, preventing `TypeError` when the value is explicitly `None` (e.g., from a JSON `null`).
- **Navbar icon flash on page reload** — Added intrinsic `width`/`height` attributes to the app icon to prevent it from rendering at full native size before CSS loads.
- **Test assertion** — Removed stale emoji prefix from sync unit test assertion to match current log format.

---

## [1.0.0] - 2026-03-01

### Initial Release

Book Stitch is a self-hosted sync engine that links audiobook listening positions to matching spots in ebooks. It transcribes a segment of the audiobook audio, fuzzy-matches it against the EPUB text, and builds an alignment map. Once built, converting between a timestamp and a page position is instant.

Forked from [abs-kosync-bridge](https://github.com/JadeTech-Solutions/abs-kosync-bridge) and rebuilt with a new architecture, simplified feature set, and fresh identity.

### Supported Platforms

| Platform | Type | Function |
|----------|------|----------|
| [Audiobookshelf](https://www.audiobookshelf.org/) | Audiobook server | Main audiobook source; reads/writes progress in seconds |
| [KOReader](https://koreader.rocks/) (via KoSync) | E-reader protocol | Ebook reader on Kobo, Boox, Kindle; syncs EPUB position via XPath |
| [Storyteller](https://smoores.gitlab.io/storyteller/) | Audiobook companion | Synced audiobook + EPUB app; REST API v2 |
| [Booklore](https://github.com/booklore) | Ebook library | Ebook manager; provides EPUB files and tracks reading progress |
| [Hardcover](https://hardcover.app/) | Book tracking | Write-only; logs reading progress for stats and tracking |
| [Calibre-Web (CWA)](https://github.com/janeczku/calibre-web) | OPDS ebook source | Alternative source for fetching EPUBs via OPDS |

All integrations are optional. Use as few or as many as you want.

### Core Features

- **Multi-platform sync** — Keeps audiobook and ebook positions in sync across all configured platforms. Progress changes on one platform are automatically converted and pushed to every other connected client.
- **Three-tier sync engine:**
  - **Instant sync** — Listens to ABS Socket.IO playback events and KoSync PUT updates in real time. Syncs within ~30 seconds of a user action.
  - **Per-client polling** — Lightweight independent polling for Storyteller and Booklore at configurable intervals. Only triggers sync when position actually changes.
  - **Scheduled full sync** — Background sweep every N minutes that catches anything the other layers missed.
- **Audio-to-text alignment** — Transcribes audiobook segments using Whisper (local CPU/GPU), Deepgram (cloud), or Whisper.cpp (external server). Fuzzy-matches against EPUB text via N-gram anchoring. Alignment map is cached in the database — subsequent syncs are instant.
- **Universal book import** — Import books as audio-only, ebook-only, or linked (audio + ebook). Not every book needs both formats.
- **Web dashboard** — Book grid with cover art, per-service progress, out-of-sync warnings, search/filter, and quick actions (sync now, mark complete, edit mapping, delete).
- **Settings UI** — All configuration managed from the web interface. Multi-library ABS picker, per-service toggles, sync tuning. Settings persist in the database; environment variables are only needed for initial bootstrapping.
- **Split-port security** — Run the KoSync API on a separate port from the admin dashboard. Expose the sync endpoint to the internet while keeping the dashboard on your LAN.
- **Write suppression** — Centralized write tracker prevents feedback loops across all clients. If Book Stitch just pushed a position to a service, the echo that comes back is silently dropped.
- **Auto-suggestions** — Discovers unmapped books with activity and fuzzy-matches them to potential ebook counterparts for user approval.
- **Batch matching** — Link multiple books at once from a queue interface.
- **Telegram notifications** — Forward log events to a Telegram chat at a configurable severity threshold.
- **Thread-safe EPUB caching** — LRU cache with locking for concurrent access from the sync daemon, background jobs, and web requests.
- **Alembic migrations** — Database schema managed via Alembic. Upgrades run automatically on startup.

### Changes from Upstream

#### Architecture

- **Flask Blueprints** — Split the monolithic `web_server.py` into modular blueprints (`dashboard`, `books`, `settings`, `api`, `logs`, `covers`). Each route group is self-contained.
- **CSS design system** — Extracted inline styles into modular stylesheets with CSS custom properties (`variables.css`, `base.css`, `layout.css`, `components.css`, `dashboard.css`, `settings.css`, `logs.css`, `match.css`).
- **Dependency injection** — All services wired through a DI container for loose coupling and testability.

#### Added

- **Universal book import** — Audio-only and ebook-only import modes alongside the original linked mode.
- **Settings overhaul** — Full UI-driven configuration with multi-library ABS picker, dynamic settings that take effect without restart.

#### Removed

- **Forge pipeline** — Removed the Auto-Forge audiobook processing pipeline. Storyteller linking is now direct via the REST API.
- **Upstream CI/CD** — Removed all upstream GitHub Actions workflows, deploy scripts, and debug tooling.
- **Emoji logging** — Removed emoji prefixes from all log statements in favor of plain text.
- **Dead code** — Cleaned up unused imports, dev markers, and unreachable code paths.

---

## Environment Variables Reference

<!-- markdownlint-disable MD060 -->

> [!NOTE]
> All settings below can be configured via the **Web UI** at `/settings`. Environment variables are only used for initial bootstrapping on first launch.

### Audiobookshelf

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_SERVER` | — | Audiobookshelf server URL |
| `ABS_KEY` | — | ABS API token |
| `ABS_LIBRARY_ID` | — | ABS library ID to sync from |
| `ABS_COLLECTION_NAME` | `Synced with KOReader` | Name of the ABS collection to auto-add synced books to |
| `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Rewind progress sent to ABS by this many seconds |
| `ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID` | `false` | Limit ebook searches to the configured ABS library only |

### KOSync

| Variable | Default | Description |
|----------|---------|-------------|
| `KOSYNC_ENABLED` | `false` | Enable KOSync integration |
| `KOSYNC_SERVER` | — | Target KOSync server URL |
| `KOSYNC_USER` | — | KOSync username |
| `KOSYNC_KEY` | — | KOSync password |
| `KOSYNC_HASH_METHOD` | `content` | Hash method: `content` (accurate) or `filename` (fast) |
| `KOSYNC_USE_PERCENTAGE_FROM_SERVER` | `false` | Use raw % from server instead of text-based matching |

### Storyteller

| Variable | Default | Description |
|----------|---------|-------------|
| `STORYTELLER_ENABLED` | `false` | Enable Storyteller integration |
| `STORYTELLER_API_URL` | — | Storyteller server URL (e.g., `http://host.docker.internal:8001`) |
| `STORYTELLER_USER` | — | Storyteller username |
| `STORYTELLER_PASSWORD` | — | Storyteller password |

### Booklore

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKLORE_ENABLED` | `false` | Enable Booklore integration |
| `BOOKLORE_SERVER` | — | Booklore server URL |
| `BOOKLORE_USER` | — | Booklore username |
| `BOOKLORE_PASSWORD` | — | Booklore password |
| `BOOKLORE_SHELF_NAME` | `Kobo` | Name of the Booklore shelf to auto-add synced books to |
| `BOOKLORE_LIBRARY_ID` | — | Restrict sync to a specific Booklore library ID |

### CWA (Calibre-Web Automated)

| Variable | Default | Description |
|----------|---------|-------------|
| `CWA_ENABLED` | `false` | Enable CWA/OPDS integration |
| `CWA_SERVER` | — | Calibre-Web server URL |
| `CWA_USERNAME` | — | Calibre-Web username |
| `CWA_PASSWORD` | — | Calibre-Web password |

### Hardcover.app

| Variable | Default | Description |
|----------|---------|-------------|
| `HARDCOVER_ENABLED` | `false` | Enable Hardcover.app integration |
| `HARDCOVER_TOKEN` | — | API token from hardcover.app/account/api |

### Telegram Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_ENABLED` | `false` | Enable Telegram notifications |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID to send messages to |
| `TELEGRAM_LOG_LEVEL` | `ERROR` | Minimum log level to forward (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`) |

### Shelfmark

| Variable | Default | Description |
|----------|---------|-------------|
| `SHELFMARK_URL` | — | URL to your Shelfmark instance (enables nav icon when set) |

### Sync Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_PERIOD_MINS` | `5` | Background sync interval in minutes |
| `SYNC_DELTA_ABS_SECONDS` | `60` | Min ABS progress change (seconds) to trigger an update |
| `SYNC_DELTA_KOSYNC_PERCENT` | `0.5` | Min KOSync progress change (%) to trigger an update |
| `SYNC_DELTA_KOSYNC_WORDS` | `400` | Min word-count change to trigger a KOSync update |
| `SYNC_DELTA_BETWEEN_CLIENTS_PERCENT` | `0.5` | Min difference between clients (%) to trigger propagation |
| `FUZZY_MATCH_THRESHOLD` | `80` | Text matching confidence threshold (0–100) |
| `SYNC_ABS_EBOOK` | `false` | Also sync progress to the ABS ebook item |
| `XPATH_FALLBACK_TO_PREVIOUS_SEGMENT` | `false` | Fall back to previous XPath segment on lookup failure |
| `SUGGESTIONS_ENABLED` | `false` | Enable auto-discovery suggestions |
| `ABS_SOCKET_ENABLED` | `true` | Enable real-time ABS Socket.IO listener for instant sync on playback events |
| `ABS_SOCKET_DEBOUNCE_SECONDS` | `30` | Seconds to wait after last ABS playback event before triggering sync |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_PROVIDER` | `local` | Provider: `local` (faster-whisper), `deepgram`, or `whisper_cpp` |
| `WHISPER_MODEL` | `tiny` | Whisper model size (`tiny`, `base`, `small`, `medium`, `large`) |
| `WHISPER_DEVICE` | `auto` | Device: `auto`, `cpu`, or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `auto` | Precision: `int8`, `float16`, `float32` |
| `WHISPER_CPP_URL` | — | URL to whisper.cpp server endpoint |
| `DEEPGRAM_API_KEY` | — | Deepgram API key |
| `DEEPGRAM_MODEL` | `nova-2` | Deepgram model tier |

### System

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `America/New_York` | Container timezone |
| `LOG_LEVEL` | `INFO` | Application log level |
| `DATA_DIR` | `/data` | Path to persistent data directory |
| `BOOKS_DIR` | `/books` | Path to local ebook library |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Path to local audiobook files |
| `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Path to Storyteller library directory |
| `EBOOK_CACHE_SIZE` | `3` | LRU cache size for parsed ebooks |
| `JOB_MAX_RETRIES` | `5` | Max transcription job retry attempts |
| `JOB_RETRY_DELAY_MINS` | `15` | Minutes to wait between job retries |
