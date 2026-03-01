# Book Stitch — TODO

## Code Cleanup
- [ ] Remove last 5 emojis from codebase:
  - `src/blueprints/books.py` lines 518, 545, 552 — flash messages use ❌/✅
  - `src/sync_manager.py` line 1245 — `📊` in status line logged via logger.info
  - `src/api/booklore_client.py` line 771 — `📚` in Booklore shelf API payload

## ABS Integration — Remove "Required" Assumptions
- [ ] Rewrite `QUICKSTART.md` — currently assumes ABS is mandatory
  - ABS_SERVER, ABS_KEY, ABS_LIBRARY_ID marked as `# REQUIRED` in the compose template
  - Step 1 is "Get Your API Keys" — assumes ABS is the starting point
  - References "Book Linker" (removed with Forge) and port 8080 (should be 4477)
  - Uses emojis throughout
  - Should reflect that ABS is optional and users can start with ebook-only or any combination
- [ ] Review settings UI — ABS is tab 1 with no indication it's optional
  - Other integrations have enable/disable toggles; ABS does not
  - Consider adding an `ABS_ENABLED` toggle or at minimum labeling it as optional

## Frontend
- [ ] Continue frontend improvements (UI/UX polish, responsiveness, design consistency)

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
- [ ] Clean up dead code in `src/api/storyteller_api.py`
  - `find_book_by_title()`, `get_progress_by_filename()`, `update_progress_by_filename()`,
    `get_progress()`, `get_progress_with_fragment()` are all legacy filename-based methods
  - Sync client now uses UUID-based methods exclusively — these are unused
- [ ] Remove legacy link migration logic in `src/blueprints/dashboard.py` (lines 155-158)
  - Detects books with Storyteller state but no UUID, shows re-link prompt
  - `storyteller_legacy_link` flag carried through to `templates/index.html`
- [ ] Rethink match UI for Storyteller
  - `templates/match.html` and `templates/batch_match.html` have a dedicated "Storyteller (Preferred)" column as step 2
  - With Forge removed, Storyteller isn't the automatic ebook pipeline — the UI hierarchy should reflect that
  - Consider making Storyteller linking optional/secondary rather than a required step in the flow
- [ ] Address N+1 in `get_all_positions_bulk()` — fetches each book's position individually in a loop
- [ ] `search_books()` fetches the entire Storyteller library then filters client-side — no server-side search
- [ ] No guidance for users on how to get books into Storyteller now that Forge is gone

## Hardcover Integration
- [ ] Improve Hardcover integration
  - Current state: write-only progress sync, basic auto-matching by ISBN/title
  - Better edition matching (ASIN, ISBN-13, manual override)
  - Richer metadata sync (cover art, series info)
  - Read status tracking (want to read, currently reading, finished)
