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
- [x] ~~Improve System Paths in Settings -> General~~ — Removed; paths and timezone are now Docker-only env vars
- [ ] Add a "Trigger Sync" button to the UI
  - Currently the only way to force a full sync cycle is restarting the container
  - Per-book sync exists (`/api/sync-now/<abs_id>`) but no full-cycle trigger
  - Add a button (settings page or dashboard) that calls a new `/api/sync-all` endpoint
  - Endpoint should call `manager.sync_cycle()` in a background thread

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

## KOSync Server Security Hardening

The KOSync server (`src/api/kosync_server.py`) is not safe to expose publicly.
Split-port mode correctly isolates admin endpoints, but the sync protocol
itself has issues. Fixes are ordered by severity.

### Phase 1 — Critical (must fix before any public exposure)

- [ ] **`/users/login` leaks password** (line 170)
  - Returns `KOSYNC_KEY` in plaintext in the JSON response body
  - Fix: return a session token or the MD5 hash, never the raw password
  - Also review `/users/create` (line 156) — currently a stub that always returns 201

- [ ] **Admin endpoints have no authentication** (lines 751+)
  - `api_get_kosync_documents`, `api_link_kosync_document`, `api_unlink_kosync_document`,
    `api_delete_kosync_document` have no `@kosync_auth_required` decorator
  - "LAN only" is an assumption, not an enforcement
  - Fix: add auth decorator to all admin endpoints, or gate them behind a separate
    admin token/session check

- [ ] **Auth accepts raw password in header** (line 107)
  - `key == expected_password or key == expected_hash` — accepting the plaintext password
    alongside the MD5 hash means intercepted traffic gives full access
  - Fix: only accept the MD5 hash (this is what KOReader actually sends)
  - Verify KOReader's behavior first — if it sends raw password on some code paths,
    this can't be removed without breaking compatibility

### Phase 2 — Moderate (should fix for production use)

- [ ] **No rate limiting on auth endpoints**
  - `/users/auth`, `/users/login`, `/syncs/progress` can all be brute-forced
  - Fix: add per-IP rate limiting (e.g. flask-limiter or simple in-memory counter)
  - Consider lockout after N failed attempts from the same IP

- [ ] **Auto-discovery on untrusted input** (lines 226-442)
  - Sending arbitrary hashes to PUT triggers filesystem scans (`_try_find_epub_by_hash`)
    and spawns background threads with no concurrency limit
  - Fix: cap concurrent auto-discovery threads (e.g. max 3)
  - Fix: rate-limit new hash registrations per IP
  - Consider making `AUTO_CREATE_EBOOK_MAPPING` default to `false` when split-port is enabled

- [ ] **No input validation on PUT body** (line 250+)
  - `percentage`, `progress`, `device`, `device_id` aren't type-checked or range-validated
  - Fix: validate percentage is a float 0.0–1.0, document hash is hex, etc.
  - Reject malformed payloads early

### Phase 3 — Hardening (defense in depth)

- [ ] **MD5 password hashing** (`src/utils/kosync_headers.py` line 15)
  - MD5 is cryptographically broken — rainbow tables, collision attacks
  - This is the KOSync protocol spec, so changing it breaks KOReader compatibility
  - Mitigation: document that strong passwords are required since the hash is weak
  - Long-term: consider optional HMAC-based auth as an alternative for non-KOReader clients

- [ ] **No HTTPS enforcement**
  - Flask dev server has no TLS; credentials travel in plain HTTP headers
  - Fix: document that a reverse proxy with TLS is required for public exposure
  - Consider adding a startup warning if split-port is enabled without a proxy detected

- [ ] **CORS / CSRF protection**
  - No CORS headers on sync endpoints — browser-based CSRF possible if server is reachable
  - Fix: add restrictive CORS policy to sync blueprint (deny cross-origin by default)

## Dual Booklore — Architectural Improvements
- [ ] Persist Booklore source on Book records to avoid cross-instance drift
  - `find_in_booklore()` resolves by filename each time; if the same filename exists on both servers, later re-lookups (update hash, shelf ops) can bind to the wrong instance
  - Add a `booklore_source` column to `Book` and pass `source_tag` through match/import flows
- [x] Use list-per-filename for `booklore_by_filename` in dashboard enrichment
- [ ] Pin GitHub Actions SHAs in `.github/workflows/lint.yml`
  - `actions/checkout@v4` and `astral-sh/ruff-action@v3` use floating tags
  - Add explicit `permissions: contents: read` block
- [ ] Fix ambiguous anchor matching in `ebook_utils.py` (line ~950)
  - `bs4_chapter_text.find(clean_anchor)` always picks the first occurrence; if `clean_anchor` repeats, can resolve to wrong position
  - Consider choosing the occurrence closest to `target_offset`
- [x] Clean up type annotations in `hardcover_client.py`

## Hardcover Integration
- [ ] Improve Hardcover integration
  - Current state: write-only progress sync, basic auto-matching by ISBN/title
  - Better edition matching (ASIN, ISBN-13, manual override)
  - Richer metadata sync (cover art, series info)
  - Read status tracking (want to read, currently reading, finished)
