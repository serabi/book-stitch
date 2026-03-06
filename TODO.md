# PageKeeper — TODO

## Frontend
- [ ] Continue frontend improvements (UI/UX polish, responsiveness, design consistency)
- [ ] Add a "Trigger Sync" button to the UI
  - Currently the only way to force a full sync cycle is restarting the container
  - Per-book sync exists (`/api/sync-now/<abs_id>`) but no full-cycle trigger
  - Add a button (settings page or dashboard) that calls a new `/api/sync-all` endpoint
  - Endpoint should call `manager.sync_cycle()` in a background thread

## Local Reading Tracker ([#16](https://github.com/serabi/pagekeeper/issues/16))
- [x] Data model (Phase 1) — `started_at`, `finished_at`, `rating`, `read_count` on Book; `ReadingJournal` and `ReadingGoal` models; Alembic migration; DatabaseService CRUD
- [ ] Reading Tab UI (Phase 2) — `reading_bp` blueprint, navbar link, stats bar, status filter pills, book grid, book detail view, journal timeline, API endpoints, JS/CSS

## Reading Stats & Goals ([#18](https://github.com/serabi/pagekeeper/issues/18))
- [ ] Stats computation: books finished/year, per-month breakdown, average rating, currently reading count
- [ ] Yearly reading goal: set target, track progress
- [ ] Stats bar on reading tab (goal progress, monthly chart, stat cards)
- [ ] API endpoints for stats and goals
- Depends on #16. Design doc: `future-plans/2026-03 - reading tracker plan.md` (Phase 5)

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

## Hardcover Bidirectional Sync ([#17](https://github.com/serabi/pagekeeper/issues/17))
- [ ] Backend improvements (Phase 3): cached columns on HardcoverDetails, rate limiter, cached `_ensure_user_book()`/`_ensure_read_id()`, smarter status transitions, `get_service_state()`, `fetch_bulk_state()`, edition-aware sync, client poller
- [ ] Local ↔ Hardcover data sync (Phase 4): push/pull status changes, date sync, optional journal mirroring, status badges on dashboard
- Depends on #16. Design doc: `future-plans/2026-03 - reading tracker plan.md` (Phases 3-4)

## BookFusion Integration — Phase 2: Journal Highlights
- [ ] Link BookFusion highlights to PageKeeper `Book` records
  - Match by title/filename between `BookfusionHighlight.book_title` and `Book` records
  - Display matched highlights on the book's journal page alongside reading notes
  - Ties into the journaling feature — highlights become part of the reading story
- [ ] Add BookFusion highlight sync to the dashboard
  - Show last sync time and number of highlights synced
  - Add a "Sync Now" button to force a sync
- [ ] Add BookFusion icon to header
- [ ] See if there's a way to pull the BookFusion book list via the highlights sync API

## Decouple Data Model from ABS ID ([#20](https://github.com/serabi/pagekeeper/issues/20))
- [ ] Introduce source-agnostic primary key (internal UUID) for `Book` model
- [ ] Make `abs_id` an optional field rather than the required anchor
- [ ] Allow books to be anchored to any service (Storyteller, Booklore, ABS)
- [ ] Update `PendingSuggestion` to support non-ABS source IDs
- [ ] Update sync logic to handle books without ABS associations

## Create PWA ([#22](https://github.com/serabi/pagekeeper/issues/22))
- [ ] Look into making the frontend a PWA for better mobile compatibility

## Smart Match Suggestions
- [ ] Auto-suggest likely book pairings when linking
  - Pre-compute candidate matches across all services by title/author similarity:
    - ABS audiobook ↔ ebook file
    - ABS audiobook ↔ Storyteller book
    - Ebook file ↔ Storyteller book
  - Surface suggestions in the match UI to reduce manual searching
  - Purely internal matching intelligence — does not create or push anything to external services
