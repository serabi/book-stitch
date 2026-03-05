# Copilot Instructions

## Project Overview

Book Sync is a self-hosted, Docker-based sync engine that keeps audiobook and ebook reading positions in sync across multiple platforms (Audiobookshelf, KOReader via KoSync, Storyteller, Booklore, and Hardcover). It works by transcribing audiobook audio and fuzzy-matching the transcript against EPUB text to build an alignment map, then converting positions between formats and pushing updates to all connected clients.

The application is a Python/Flask web server with a web dashboard on port 4477, a background sync engine, and an optional split KoSync API port.

## Repository Structure

```
src/
  api/            # REST API blueprints
  blueprints/     # Flask route handlers (web UI, KoSync protocol)
  db/             # Database models and service layer (SQLAlchemy + SQLite via Alembic)
  services/       # Core business logic (alignment, transcription, sync management)
  sync_clients/   # Per-platform client wrappers (ABS, Storyteller, Booklore, Hardcover, CWA)
  sync_manager.py # Central orchestrator: listens for events, drives sync cycles
  web_server.py   # Flask app factory and startup
tests/            # Pytest test suite
templates/        # Jinja2 HTML templates
static/           # CSS, JS, icons
alembic/          # Database migration scripts
```

## Testing

Always run tests via `./run-tests.sh` — never bare `pytest`. The test suite requires Docker for `epubcfi` and `ffmpeg` dependencies that are not available locally.

```bash
./run-tests.sh                              # full suite
./run-tests.sh tests/test_sync_manager.py   # single file
./run-tests.sh -k "test_name" -v            # filtered + verbose
```

If the `book-sync` container is running, tests execute there via `docker exec` (fastest). Otherwise the script falls back to `docker compose -f docker-compose.test.yml run --rm test`.

## Code Style

- Python 3.11+, formatted and linted with **Ruff** (`ruff check` and `ruff format`)
- Line length: 120 characters (E501 is ignored for log/debug strings)
- SQLAlchemy filter patterns use `column == None` and `column == True` style (E711/E712 ignored)
- Dependency-injector patterns use function calls in default arguments (B008 ignored)
- Import style: `known-first-party = ["src"]`

Run the linter with:
```bash
ruff check src/ tests/
ruff format src/ tests/
```

## Security Notes

- `KOSYNC_KEY` is intentionally revealed to the user via the SHOW button on the settings page (fetch-on-demand, never embedded in HTML). **It must never appear in log output** — always sanitize before logging.
- The admin dashboard (port 4477) must stay on the LAN; it has no authentication layer.
- Only the KoSync port (`KOSYNC_PORT`) should be exposed to the internet — it has rate limiting, input validation, and MD5-hashed authentication.
- Admin/management endpoints require credentials when accessed from public IPs.
- Do not expose the `/api/*` endpoints through a public reverse proxy.

## Commit Rules

- Do not include `Co-Authored-By` lines or any other AI attribution in commit messages.

## Key Patterns

- **Write-tracker**: After pushing a position to a client, the sync engine ignores the echo that comes back to prevent feedback loops.
- **Alignment map**: Built once per audiobook/EPUB pair, then cached. Transcription uses Whisper (local), Deepgram (cloud), or Whisper.cpp (external).
- **Three sync layers**: Instant (Socket.IO + KoSync webhooks), per-client polling, and scheduled full sweep.
- **Database migrations**: Managed with Alembic. Run `alembic upgrade head` after pulling changes that include new migration files.
