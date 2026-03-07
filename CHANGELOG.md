# Changelog

<!-- markdownlint-disable MD024 -->

All notable changes to PageKeeper will be documented in this file.

## [0.1.2] - 2026-03-07

### Added

- **BookFusion integration** — Upload books from Booklore to BookFusion, sync highlights via the Obsidian plugin API, and save highlights to reading journals. Dual API key support with test-before-save.
- **Reading tracker** — Reading detail page with journal entries, manual progress input, ratings, read counts, and yearly reading goals.
- **Suggestions system** — Dedicated `/suggestions` page with card grid, cover images, match candidates, filter/search, and Dismiss/Link/Never Ask actions. Socket-driven discovery queues suggestions automatically when unmapped audiobooks are detected. Reverse suggestions surface audiobook matches for ebook-only books.
- **Batch matching** — Queue-based batch matching workflow with validation and prefilled linking.
- **Storyteller native alignment** — Allows you to build alignment maps directly from Storyteller's word-level timing data, bypassing Whisper transcription
- **Cover management** — Cover picker modal, custom cover URL support, Hardcover cover search.
- **Processing dashboard UX** — Dedicated processing section with progress bars, retry counts, status accents, and live polling.
- **Reading date sync** — Pull `started_at`/`finished_at` from Hardcover and ABS. Auto-complete books at 100% progress, with re-read guards in place. 
- **Diagnostic test buttons** — One-click connectivity/auth verification for all services in Settings.
- **Dashboard status filter** — Filter book grid by status or sync mode.
- **Hot-reload settings** — Settings changes apply in-process without container restart.

### Changed

- **Backend decomposition** — Extracted SuggestionService, BackgroundJobService, ProgressResetService, and database repositories from monolithic files.
- **Renamed to PageKeeper** — Project renamed from Book Sync across all branding, Docker config, and documentation.
- **Completed book actions** — Streamlined kebab menu: "Sync status to all services" and "Clear Progress" only.

### Fixed

- **Security: exception logging sanitized** — Error handlers no longer log raw exception text or tracebacks at ERROR level, preventing KOSYNC_KEY leakage.
- **Security: path traversal in EPUB resolver** — Filename validation and containment checks on filesystem glob results.
- **Security: API token moved to header** — Cover proxy uses `Authorization: Bearer` instead of query parameter.
- **Security: secrets removed from settings HTML** — Password fields show placeholder instead of emitting stored values.
- **Security: path traversal at HTTP boundaries (CWE-22)** — Sanitize `ebook_filename` inputs and validate resolved paths before file operations.
- **Security: pin h11>=0.16.0** — Fix HTTP Request Smuggling (CVE-2025-43859). Upgrade Docker base to python:3.13-slim.
- **Sync: fallback state save key mismatch** — Leader snapshot now uses correct `'ts'` key and fresh `last_updated`, matching the normal sync path.
- **Sync: Booklore link/unlink stale fields** — Clearing or changing ebook association now updates `kosync_doc_id` and `original_ebook_filename`.
- **Sync: deferred clear progress** — Lock timeout no longer discards pending clear, allowing deferred external reset on next cycle.
- **Sync: clear progress re-sync bounce** — 0% states saved after clearing to prevent stale external progress from bouncing back.
- **UX: custom cover override** — User-set custom covers now correctly override auto-discovered ABS covers.
- **UX: batch queue Ready status** — Storyteller-only items without ebook no longer marked Ready (they can't be processed).
- **Booklore UNIQUE constraint** — Composite `(filename, source)` constraint replaces stale single-column constraint.
- **Suggestion sort tie-breaker** — Booklore matches now correctly prioritized instead of deprioritized.
- **KOSync stale shadow documents** — Sibling resolution skips documents not updated in 30 days.
- **Alignment map validation** — Malformed entries skipped with warning instead of crashing.

---

## [0.1.0] - 2026-03-01

### Initial Release

PageKeeper is a self-hosted sync engine that links audiobook listening positions to matching spots in ebooks. It transcribes a segment of the audiobook audio, fuzzy-matches it against the EPUB text, and builds an alignment map. Once built, converting between a timestamp and a page position is instant.

Forked from [abs-kosync-bridge](https://github.com/JadeTech-Solutions/abs-kosync-bridge) and rebuilt with a new architecture.

#### Supported Platforms

| Platform | Type | Function |
|----------|------|----------|
| [Audiobookshelf](https://www.audiobookshelf.org/) | Audiobook server | Main audiobook source; reads/writes progress |
| [KOReader](https://koreader.rocks/) (via KoSync) | E-reader protocol | Ebook reader on Kobo, Boox, Kindle; syncs EPUB position |
| [Storyteller](https://smoores.gitlab.io/storyteller/) | Audiobook companion | Synced audiobook + EPUB app |
| [Booklore](https://github.com/booklore) | Ebook library | Ebook manager; provides EPUB files and tracks reading progress |
| [Hardcover](https://hardcover.app/) | Book tracking | Write-only; logs reading progress |
| [Calibre-Web (CWA)](https://github.com/janeczku/calibre-web) | OPDS ebook source | Alternative source for fetching EPUBs |

All integrations are optional.

#### Core Features

- **Multi-platform sync** — Keeps audiobook and ebook positions in sync across all configured platforms.
- **Three-tier sync engine** — Instant sync (ABS Socket.IO + KoSync PUT), per-client polling (Storyteller, Booklore), and scheduled full sweep.
- **Audio-to-text alignment** — Whisper (local CPU/GPU), Deepgram (cloud), or Whisper.cpp. Alignment map cached in DB for instant subsequent syncs.
- **Universal book import** — Audio-only, ebook-only, or linked (audio + ebook).
- **Web dashboard** — Book grid with covers, per-service progress, search/filter, and quick actions.
- **Settings UI** — All configuration via web interface. Multi-library ABS picker, per-service toggles, sync tuning.
- **Split-port security** — KoSync API on a separate port from the admin dashboard.
- **Write suppression** — Centralized write tracker prevents feedback loops across clients.

#### Architecture (vs. upstream)

- Flask Blueprints, CSS design system with custom properties, dependency injection container.
- Removed: Forge pipeline, upstream CI/CD, emoji logging, dead code.

---

## Environment Variables Reference

<!-- markdownlint-disable MD060 -->

> [!NOTE]
> All settings below can be configured via the **Web UI** at `/settings`. Environment variables are only used for initial bootstrapping on first launch.

### Audiobookshelf

| Variable | Default | Description |
|----------|---------|-------------|
| `ABS_SERVER` | -- | Audiobookshelf server URL |
| `ABS_KEY` | -- | ABS API token |
| `ABS_LIBRARY_ID` | -- | ABS library ID to sync from |
| `ABS_COLLECTION_NAME` | `Synced with KOReader` | ABS collection to auto-add synced books to |
| `ABS_PROGRESS_OFFSET_SECONDS` | `0` | Rewind progress sent to ABS by this many seconds |
| `ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID` | `false` | Limit ebook searches to the configured ABS library |

### KOSync

| Variable | Default | Description |
|----------|---------|-------------|
| `KOSYNC_ENABLED` | `false` | Enable KOSync integration |
| `KOSYNC_SERVER` | -- | Target KOSync server URL |
| `KOSYNC_USER` | -- | KOSync username |
| `KOSYNC_KEY` | -- | KOSync password |
| `KOSYNC_HASH_METHOD` | `content` | Hash method: `content` (accurate) or `filename` (fast) |
| `KOSYNC_USE_PERCENTAGE_FROM_SERVER` | `false` | Use raw % from server instead of text-based matching |

### Storyteller

| Variable | Default | Description |
|----------|---------|-------------|
| `STORYTELLER_ENABLED` | `false` | Enable Storyteller integration |
| `STORYTELLER_API_URL` | -- | Storyteller server URL |
| `STORYTELLER_USER` | -- | Storyteller username |
| `STORYTELLER_PASSWORD` | -- | Storyteller password |

### Booklore

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKLORE_ENABLED` | `false` | Enable Booklore integration |
| `BOOKLORE_SERVER` | -- | Booklore server URL |
| `BOOKLORE_USER` | -- | Booklore username |
| `BOOKLORE_PASSWORD` | -- | Booklore password |
| `BOOKLORE_SHELF_NAME` | `Kobo` | Booklore shelf to auto-add synced books to |
| `BOOKLORE_LIBRARY_ID` | -- | Restrict sync to a specific Booklore library ID |

### CWA (Calibre-Web Automated)

| Variable | Default | Description |
|----------|---------|-------------|
| `CWA_ENABLED` | `false` | Enable CWA/OPDS integration |
| `CWA_SERVER` | -- | Calibre-Web server URL |
| `CWA_USERNAME` | -- | Calibre-Web username |
| `CWA_PASSWORD` | -- | Calibre-Web password |

### Hardcover.app

| Variable | Default | Description |
|----------|---------|-------------|
| `HARDCOVER_ENABLED` | `false` | Enable Hardcover.app integration |
| `HARDCOVER_TOKEN` | -- | API token from hardcover.app/account/api |

### BookFusion

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKFUSION_ENABLED` | `false` | Enable BookFusion integration |
| `BOOKFUSION_CALIBRE_API_KEY` | -- | BookFusion Calibre plugin API key (uploads) |
| `BOOKFUSION_OBSIDIAN_API_KEY` | -- | BookFusion Obsidian plugin API key (highlights) |

### Telegram Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_ENABLED` | `false` | Enable Telegram notifications |
| `TELEGRAM_BOT_TOKEN` | -- | Telegram bot token |
| `TELEGRAM_CHAT_ID` | -- | Telegram chat ID |
| `TELEGRAM_LOG_LEVEL` | `ERROR` | Minimum log level to forward |

### Sync Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_PERIOD_MINS` | `5` | Background sync interval in minutes |
| `SYNC_DELTA_ABS_SECONDS` | `60` | Min ABS progress change (seconds) to trigger update |
| `SYNC_DELTA_KOSYNC_PERCENT` | `0.5` | Min KOSync progress change (%) to trigger update |
| `SYNC_DELTA_KOSYNC_WORDS` | `400` | Min word-count change to trigger KOSync update |
| `SYNC_DELTA_BETWEEN_CLIENTS_PERCENT` | `0.5` | Min difference between clients (%) to trigger propagation |
| `FUZZY_MATCH_THRESHOLD` | `80` | Text matching confidence threshold (0-100) |
| `SYNC_ABS_EBOOK` | `false` | Also sync progress to ABS ebook item |
| `XPATH_FALLBACK_TO_PREVIOUS_SEGMENT` | `false` | Fall back to previous XPath segment on lookup failure |
| `SUGGESTIONS_ENABLED` | `false` | Enable auto-discovery suggestions |
| `ABS_SOCKET_ENABLED` | `true` | Enable real-time ABS Socket.IO listener |
| `ABS_SOCKET_DEBOUNCE_SECONDS` | `30` | Debounce after last ABS playback event before sync |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_PROVIDER` | `local` | Provider: `local` (faster-whisper), `deepgram`, or `whisper_cpp` |
| `WHISPER_MODEL` | `tiny` | Whisper model size |
| `WHISPER_DEVICE` | `auto` | Device: `auto`, `cpu`, or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `auto` | Precision: `int8`, `float16`, `float32` |
| `WHISPER_CPP_URL` | -- | URL to whisper.cpp server |
| `DEEPGRAM_API_KEY` | -- | Deepgram API key |
| `DEEPGRAM_MODEL` | `nova-2` | Deepgram model tier |

### System

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `America/New_York` | Container timezone |
| `LOG_LEVEL` | `INFO` | Application log level |
| `DATA_DIR` | `/data` | Persistent data directory |
| `BOOKS_DIR` | `/books` | Local ebook library path |
| `AUDIOBOOKS_DIR` | `/audiobooks` | Local audiobook files path |
| `STORYTELLER_LIBRARY_DIR` | `/storyteller_library` | Storyteller library directory |
| `EBOOK_CACHE_SIZE` | `3` | LRU cache size for parsed ebooks |
| `JOB_MAX_RETRIES` | `5` | Max transcription job retry attempts |
| `JOB_RETRY_DELAY_MINS` | `15` | Minutes between job retries |
