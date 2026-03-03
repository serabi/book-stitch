# Book Stitch — TODO

## Code Cleanup
- [x] Remove last 5 emojis from codebase
- [x] Clean up dead code in Storyteller API client

## Developer Experience
- [x] Docker-first test runner (`run-tests.sh`, `docker-compose.test.yml`)
  - Routes all tests through Docker for `epubcfi` and `ffmpeg` dependencies
  - Claude Code hook blocks bare `pytest` and redirects to `./run-tests.sh`
  - Updated CLAUDE.md and README with testing conventions

## ABS Integration — Remove "Required" Assumptions

### QUICKSTART.md
- [x] Remove `# REQUIRED` from ABS env vars in compose template
- [x] Rewrite Step 1 — replaced ABS-first flow with "Choose Your Services" approach
- [x] Remove "Book Linker" reference
- [x] Fix port references to 4477
- [x] Remove emojis throughout
- [x] Add ebook-only and audio-only setup paths

### Settings UI
- [x] Add `ABS_ENABLED` toggle to settings (matches Storyteller/Booklore pattern)

### Backend / Sync Engine
- [x] Fix `abs_sync_client.py` `is_configured()` — delegates to `ABSClient.is_configured()`
- [x] Add `ABS_ENABLED` to `config_loader.py` `ALL_SETTINGS` and `DEFAULT_CONFIG`
- [x] Add `ABS_ENABLED` to `settings_bp.py` `bool_keys` list
- [x] `sync_manager.py` `_setup_sync_clients` — resolved by `is_configured()` fix
- [x] `ABSClient.is_configured()` checks `ABS_ENABLED` env var
- [x] `ABSEbookSyncClient.is_configured()` checks ABS availability before `SYNC_ABS_EBOOK`
- [x] Extracted `ABSService` wrapper with `is_available()` guards
- [x] Created `abs_bp` blueprint for ABS-specific routes (libraries, cover proxy)
- [x] Migrated all direct `abs_client` calls in blueprints to use `abs_service`
- [x] Guarded Hardcover automatch and resolve when ABS disabled
- [x] Fix `sync_manager.py` cross-format normalization (line ~280): `if not has_abs or not ebook_clients: return None` assumes ABS is always present — ebook-only books should still normalize between ebook clients

## Frontend
- [ ] Continue frontend improvements (UI/UX polish, responsiveness, design consistency)
- [ ] Add a "Trigger Sync" button to the UI
  - Currently the only way to force a full sync cycle is restarting the container
  - Per-book sync exists (`/api/sync-now/<abs_id>`) but no full-cycle trigger
  - Add a button (settings page or dashboard) that calls a new `/api/sync-all` endpoint
  - Endpoint should call `manager.sync_cycle()` in a background thread

## Book Status: Paused / DNF
- [ ] Add ability to mark a book as Paused or DNF (Did Not Finish)
  - New status values alongside existing `active`, `pending`, etc.
  - Paused/DNF books should be excluded from automatic sync cycles
  - Add UI controls in the card action panel to set/clear these statuses
  - Consider a filtered view or section to separate paused/DNF books from active ones

## Reading History
- [ ] Add reading history feature
  - New database model to track position changes over time per book
  - No existing schema, routes, or templates — needs to be built from scratch
  - Could record each sync event with before/after positions and timestamps

## Statistics
- [ ] Add statistics page
  - Sync counts, reading pace, library overview, per-book activity
  - No existing schema, routes, or templates — needs to be built from scratch
  - Could aggregate from the existing `State` table and sync logs

## Storyteller Integration
- [x] Clean up dead code in `src/api/storyteller_api.py`
  - Removed `find_book_by_title()`, `get_progress_by_filename()`, `update_progress_by_filename()`,
    `get_progress()`, `get_progress_with_fragment()` and supporting `_filename_to_book_cache`
- [ ] Remove legacy link migration logic in `src/blueprints/dashboard.py` (lines 155-158)
  - Detects books with Storyteller state but no UUID, shows re-link prompt
  - `storyteller_legacy_link` flag carried through to `templates/index.html`
- [ ] Rethink match UI for Storyteller
  - `templates/match.html` and `templates/batch_match.html` have Storyteller as step 2 (no longer labeled "Preferred")
  - Consider making Storyteller linking optional/secondary rather than a required step in the flow
- [ ] Address N+1 in `get_all_positions_bulk()` — fetches each book's position individually in a loop
- [ ] `search_books()` fetches the entire Storyteller library then filters client-side — no server-side search
- [ ] No guidance for users on how to get books into Storyteller now that Forge is gone

## Dual Booklore — Architectural Improvements
- [ ] Persist Booklore source on Book records to avoid cross-instance drift
  - `find_in_booklore()` resolves by filename each time; if the same filename exists on both servers, later re-lookups (update hash, shelf ops) can bind to the wrong instance
  - Add a `booklore_source` column to `Book` and pass `source_tag` through match/import flows
- [ ] Pin GitHub Actions SHAs in `.github/workflows/lint.yml`
  - `actions/checkout@v4` and `astral-sh/ruff-action@v3` use floating tags
  - Add explicit `permissions: contents: read` block
- [ ] Fix ambiguous anchor matching in `ebook_utils.py` (line ~950)
  - `bs4_chapter_text.find(clean_anchor)` always picks the first occurrence; if `clean_anchor` repeats, can resolve to wrong position
  - Consider choosing the occurrence closest to `target_offset`

## Hardcover Integration
- [ ] Improve Hardcover integration
  - Current state: write-only progress sync, basic auto-matching by ISBN/title
  - Better edition matching (ASIN, ISBN-13, manual override)
  - Richer metadata sync (cover art, series info)
  - Read status tracking (want to read, currently reading, finished)

## Smart Match Suggestions
- [ ] Auto-suggest likely book pairings when linking
  - Pre-compute candidate matches across all services by title/author similarity:
    - ABS audiobook ↔ ebook file
    - ABS audiobook ↔ Storyteller book
    - Ebook file ↔ Storyteller book
  - Surface suggestions in the match UI to reduce manual searching
  - Purely internal matching intelligence — does not create or push anything to external services
