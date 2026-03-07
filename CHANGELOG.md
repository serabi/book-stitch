# Changelog

<!-- markdownlint-disable MD024 -->

All notable changes to PageKeeper will be documented in this file.

## [0.1.8] - 2026-03-07

### Added

- **Reading detail and journal UI expansion** — Added a dedicated reading detail experience with richer journal workflows, manual progress entry improvements, and new reading-focused templates, styles, and client-side interactions.
- **Batch matching and suggestion improvements** — Expanded the pairing workflow with queue-based batch matching, suggestion page UX refinements, stronger validation, and better prefilled linking flows.
- **Cover management and metadata polish** — Added cover picker support, cover image management routes, and improved metadata enrichment across dashboard and reading views.

### Changed

- **Navigation, layout, and settings UX** — Refined navbar structure, responsive layouts, settings service URL configuration, and shared component styling for better consistency across desktop and mobile views.
- **Backend service decomposition** — Continued breaking large sync and database responsibilities into focused repositories, blueprints, and services to reduce coupling and improve maintainability.
- **BookFusion and reading workflows** — Improved BookFusion linking, hidden-status handling, highlight flows, and reading-page interactions across the app.

### Fixed

- **Lint and safety cleanup** — Resolved Ruff issues, strengthened template/context escaping, eliminated remaining unsafe DOM insertion patterns, and tightened input validation around proxy and matching flows.
- **Progress and date handling reliability** — Improved reading date sync, progress reset behavior, background job recovery, and error handling around external client updates.

## [0.1.7] - 2026-03-05

### Added

- **BookFusion integration** — New BookFusion page (`/bookfusion`) for uploading books and syncing highlights. Upload books from Booklore to BookFusion via the Calibre plugin API (3-step init → S3 → finalize flow). Sync highlights from BookFusion via the Obsidian plugin API with incremental cursor-based pagination.
- **Dual API key support** — BookFusion uses separate API keys for uploads (Calibre API) and highlights (Obsidian API). Each key has its own settings field and independent test button. The highlights field auto-extracts the token when pasting an `obsidian://` connect link.
- **Test before save** — BookFusion test buttons send the unsaved key value directly, so you can verify credentials before saving settings.
- **Highlight viewer** — Highlights are displayed grouped by book, sorted by date, with parsed quote text (stripped of markdown, wiki-links, and Obsidian block IDs). Chapter and date shown beneath each highlight.
- **Save highlights to journal** — Each book group in the highlights view has a "Save to Journal" button with a book matcher dropdown (auto-selects matching PageKeeper books by title). Saves all highlights as reading journal `note` entries.

## [0.1.6] - 2026-03-05

### Added

- **Diagnostic test buttons** — Each service section on the Settings page now has a "Test" button that verifies connectivity and authentication in one click. Covers Audiobookshelf, Storyteller, Booklore, CWA, Hardcover, and Telegram. Returns human-readable error messages (e.g., "Authentication failed — check your username and password") instead of raw HTTP status codes.
- **Storyteller native alignment** — Alignment maps can now be built directly from Storyteller's word-level timing data (`wordTimeline`), bypassing Whisper transcription entirely. Mount Storyteller's processing directory and set the Assets Directory in Settings. New alignment priority chain: Storyteller native → SMIL → Whisper.
- **Socket-driven suggestion discovery** — When an unmapped audiobook is detected via ABS Socket.IO events, PageKeeper automatically queues a suggestion search in the background. Thread-safe with lock + in-flight set to prevent duplicate work.
- **Reverse suggestions** — Books with reading progress in Storyteller or Booklore now trigger searches for matching audiobooks in Audiobookshelf, surfacing pairing candidates in both directions.
- **Storyteller as suggestion source** — Storyteller is now searched alongside Booklore and CWA when discovering ebook matches for audiobook suggestions.
- **Pairing suggestions page** — Dedicated `/suggestions` page with card grid, cover images, match candidates with source labels, filter/search, and Dismiss/Link/Never Ask actions.
- **Suggestions nav link** — Added "Suggestions" to the navigation bar.
- **Processing books dashboard UX** — Dedicated "Processing" section on the dashboard with status-specific card rendering: striped progress bars for active jobs, pulsing dot for queued books, retry count for failed jobs. CSS status accents (cyan/amber/red left borders), contextual footer text, and sanitized menu actions for pending/processing/failed states. Live polling via `/api/processing-status` refreshes every 5 seconds and reloads the page on status transitions.
- **Reading date sync from external sources** — `started_at` and `finished_at` dates are now pulled from Hardcover (`user_book_reads`) and Audiobookshelf (`mediaProgress.startedAt`/`finishedAt`) instead of defaulting to today's date. A one-time backfill runs on dashboard load for books missing dates. Books found to be finished externally are automatically marked as completed.
- **Sync Reading Dates button** — New "Sync Reading Dates" tool in Settings → Tools tab. Triggers an on-demand pull of reading dates from Hardcover and ABS for all books missing them, with a summary of updated/completed/error counts.
- **Auto-complete at 100% progress** — Active books reaching ≥ 99% sync progress are automatically marked as completed and push 100% to all services. Includes re-read guard: skips auto-complete if local progress is 1–95% with an external `finished_at` date set.
- **Sync read status to Booklore** — Mark Complete pushes `READ` status to Booklore (auto-sets `dateFinished`). Resume pushes `READING` status. Supports dual Booklore instances.
- **Sync status to all services (completed books)** — Completed books now have a "Sync status to all services" menu action that pushes 100% completion to all configured services and reloads the page.

### Changed

- **Suggestions Link button** — Clicking "Link" on a suggestion now pre-fills the match page with the book's title and pre-selects the audiobook, instead of opening an empty match page.
- **Suggestions empty state** — Shows context-aware messages: explains that no candidates have been found yet when suggestions are enabled, or prompts the user to enable them in Settings (with a direct link) when disabled.
- **Completed book menu cleanup** — Completed books now show only "Sync status to all services" and "Clear Progress" in the kebab menu. Removed redundant "Mark Complete" and "Sync Now" actions.
- **Renamed to PageKeeper** — Project renamed from Book Sync to PageKeeper across all branding, templates, Docker config, and documentation. GitHub repo moved to `serabi/pagekeeper`. localStorage keys migrated with backward-compatible fallback so existing users keep their preferences.

### Fixed

- **Hardcover test button** — Hardcover's GraphQL API returns `me` as a list; the test now handles both list and dict response shapes.
- **KOSync stale shadow documents** — Sibling document resolution now skips documents not updated in the last 30 days, preventing stale shadow entries from overriding current progress.
- **Alignment map validation** — Loading alignment maps from the database now validates each point for required `char` (int) and `ts` (float) keys, skipping malformed entries with a warning instead of crashing.
- **Completed books missing from Finished section** — Books with `status == 'completed'` now appear in the Finished section regardless of their `unified_progress` value.
- **Clear progress re-sync bounce** — After clearing progress, 0% state records are now saved to the database. Previously states were only deleted, causing the next sync cycle to re-sync stale external progress back.
- **Clear progress blocking UI** — Clear progress now runs in a background thread to avoid blocking the UI for ~37 seconds waiting on the sync lock.
- **Booklore UNIQUE constraint with multiple sources** — Migration force-recreates `booklore_books` table with composite `(filename, source)` unique constraint, replacing the stale single-column constraint that failed when the same filename existed across multiple Booklore instances.
- **Sync Now silent failure for completed books** — Fixed broken import in `reading_date_service.py` and routed completed books to push 100% completion directly instead of going through `sync_cycle` (which skips non-active books).

---

## [0.1.5] - 2026-03-04

### Added

- **Reading tracker data model** — New `started_at`, `finished_at`, `rating`, and `read_count` fields on books; `ReadingJournal` and `ReadingGoal` tables for tracking reading history and yearly goals.

### Fixed

- **Idempotent mark-complete** — Repeated calls to `/api/mark-complete` on an already-completed book no longer inflate `read_count`. The increment is now guarded by a status check.
- **Input validation for reading fields** — `rating` (0–5), `read_count` (≥ 1), and `target_books` (≥ 0) are now validated before persistence, preventing invalid data from reaching the database.

---

## [0.1.4] - 2026-03-03

### Added

- **Dashboard status filter** — Filter the book grid by status (Currently Reading, Finished, Paused, DNF) or sync mode (Audiobook, Ebook-only) via the "Show:" dropdown. Sections with no matching cards are automatically hidden.

---

## [0.1.3] - 2026-03-02

### Security

- **Path traversal fix (CWE-22)** — Sanitize `ebook_filename` at all HTTP input boundaries and validate resolved paths before file deletion/access. Prevents arbitrary file deletion via crafted filenames flowing through database lookups into `Path().unlink()`.
- **Pin h11>=0.16.0** — Fix critical HTTP Request Smuggling vulnerability (CVE-2025-43859, CVSS 9.3) in transitive dependency via `python-socketio`.
- **Upgrade Docker base image to python:3.13-slim** — Resolves `util-linux` (CVE-2026-3184) and `zlib` low-severity vulnerabilities in the previous Debian base.

---

## [0.1.2] - 2026-03-02

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

## [0.1.1] - 2026-03-02

### Fixed

- **Security: API token no longer sent as URL query parameter** — The cover proxy now passes the ABS token via `Authorization: Bearer` header instead of `?token=` in the URL, preventing it from leaking into logs, browser history, and referrer headers.
- **Security: Secrets removed from settings HTML** — Password and API token fields no longer emit their stored value into the DOM. Fields show an `(unchanged)` placeholder when a value is set, and the backend preserves existing secrets when the field is submitted empty.
- **Docker test entrypoint** — Fixed `sh -c` argument handling in `docker-compose.test.yml` that leaked the shell name (`sh`) as a stray pytest argument when passing flags via `run-tests.sh`.
- **Sync engine: `client_pct` null safety** — `client_pct` is now sanitized after reading from client state, preventing `TypeError` when the value is explicitly `None` (e.g., from a JSON `null`).
- **Navbar icon flash on page reload** — Added intrinsic `width`/`height` attributes to the app icon to prevent it from rendering at full native size before CSS loads.
- **Test assertion** — Removed stale emoji prefix from sync unit test assertion to match current log format.

---

## [0.1.0] - 2026-03-01

### Initial Release

PageKeeper is a self-hosted sync engine that links audiobook listening positions to matching spots in ebooks. It transcribes a segment of the audiobook audio, fuzzy-matches it against the EPUB text, and builds an alignment map. Once built, converting between a timestamp and a page position is instant.

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
- **Write suppression** — Centralized write tracker prevents feedback loops across all clients. If PageKeeper just pushed a position to a service, the echo that comes back is silently dropped.
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
