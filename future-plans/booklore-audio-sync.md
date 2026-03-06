# Booklore Audiobook Progress Sync

**Date:** 2026-03-03
**Status:** Future planning — not yet implemented

## Context

Booklore recently added native audiobook support with a full player, streaming API, and progress tracking. PageKeeper already integrates with Booklore for **ebook** progress, but the audiobook half is unimplemented — `BookloreSyncClient.get_supported_sync_types()` returns `{'audiobook', 'ebook'}` but only the ebook path has code behind it.

This plan adds Booklore audiobook progress sync so that listening in Booklore's audiobook player stays in sync with ABS, KOReader, Storyteller, etc. — using the same alignment-map cross-format pipeline that already works for ABS audiobooks.

## Booklore Audiobook API

Discovered from Booklore source code (Spring Boot + Angular):

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/audiobook/{bookId}/info` | Metadata: duration, chapters, per-track durations, codec |
| `GET /api/v1/audiobook/{bookId}/stream` | Single-file audio streaming (HTTP Range support) |
| `GET /api/v1/audiobook/{bookId}/track/{trackIndex}/stream` | Per-track streaming for folder-based audiobooks |
| `GET /api/v1/audiobook/{bookId}/cover` | Embedded cover art |
| `GET /api/v1/books/{bookId}` | Book detail including `audiobookProgress` / `fileProgress` |
| `POST /api/v1/books/progress` | Save playback position |
| `GET /api/v1/bookmarks/book/{bookId}` | User-placed bookmarks (separate from auto-save progress) |
| `POST /api/v1/bookmarks` | Create bookmark |

**Progress format:**
- `positionMs` — milliseconds into current track
- `trackIndex` — 0-based track index (null for single-file audiobooks like .m4b)
- `percentage` — overall progress as 0-100

**Two progress API variants:**
```json
// Legacy (always supported, no bookFileId needed):
{"bookId": 42, "audiobookProgress": {"positionMs": 1234567, "trackIndex": 2, "percentage": 35.4}}

// New (requires bookFileId):
{"bookId": 42, "fileProgress": {"bookFileId": 7, "positionData": "1234567", "positionHref": "2", "progressPercent": 35.4}}
```

**Auth:** Streaming endpoints use `?token=JWT` query parameter (HTML5 `<audio>` can't send headers). All other endpoints use `Authorization: Bearer {jwt}`.

---

## V1 — Booklore Audiobook Progress Sync

### Files to Modify

#### 1. `src/api/booklore_client.py` — Add audiobook API methods

**New instance vars** in `__init__`:
- `_audio_metadata_cache = {}` — maps `book_id → {total_duration, tracks: [float], track_count, is_single_file}`
- `_audio_cache_timestamps = {}` — per-book cache timestamps, 6-hour TTL

**New methods:**

- **`get_audiobook_info(book_id)`** — `GET /api/v1/audiobook/{bookId}/info`, parses track durations into seconds (handles ms vs seconds heuristic: values > 10000 treated as ms), caches result

- **`_ms_track_to_absolute_seconds(tracks, track_index, position_ms)`** — converts `(trackIndex, positionMs)` → absolute seconds from start of book. Sum completed tracks + within-track offset. Single-file: `positionMs / 1000.0`

- **`_absolute_seconds_to_ms_track(tracks, absolute_seconds)`** → `(track_index | None, position_ms)` — reverse conversion. Iterates tracks summing durations until the target is found. Clamps at end of last track.

- **`get_audiobook_progress(book_id)`** — `GET /api/v1/books/{bookId}`, parses `audiobookProgress` or `fileProgress` fields, converts to absolute seconds using `get_audiobook_info()` track data. Returns `{absolute_seconds, percentage, position_ms, track_index}` or `None`

- **`update_audiobook_progress(book_id, absolute_seconds, percentage)`** — converts seconds → `(trackIndex, positionMs)` using `_absolute_seconds_to_ms_track()`, posts `POST /api/v1/books/progress` with legacy `audiobookProgress` variant (avoids needing `bookFileId`)

#### 2. `src/sync_clients/booklore_sync_client.py` — Route audiobook vs. ebook

**`__init__` changes:**
- Add `alignment_service` parameter (needed for audiobook text extraction)
- Add `delta_audio_thresh` from `SYNC_DELTA_ABS_SECONDS` (default 60s)

**Refactored methods** — each routes on `book.sync_mode`:

- **`get_service_state()`** → `_get_ebook_service_state()` (existing logic extracted) or `_get_audiobook_service_state()` (new: fetches Booklore audiobook progress, returns `ServiceState` with `current={'pct': ..., 'ts': abs_seconds}`)

- **`get_text_from_current_state()`** → ebook path unchanged; audiobook path uses `alignment_service.get_char_for_time(abs_id, timestamp)` → extracts ~200 chars of EPUB text around that offset (same pattern as `ABSSyncClient`)

- **`update_progress()`** → ebook path unchanged; audiobook path converts `percentage * book.duration` → absolute seconds → `booklore_client.update_audiobook_progress()`

**New private methods:**

- **`_find_booklore_audiobook(book)`** — matches a PageKeeper book to a Booklore book record:
  - Strategy A: `find_book_by_filename(book.ebook_filename)` — works when Booklore has both epub and audiobook under same book ID
  - Strategy B (fallback): `search_books(book.abs_title)` — accept only unambiguous single match; filter for audio file types if multiple results

#### 3. `src/utils/di_container.py` — Wire alignment_service

```python
booklore_sync_client = providers.Singleton(
    BookloreSyncClient, booklore_client, ebook_parser,
    client_name="BookLore", alignment_service=alignment_service  # ADD
)
```

#### 4. `src/sync_manager.py:1158` — Include Booklore in audio-only clients

Current:
```python
audio_only_clients = {'ABS', 'Hardcover'}
```

Updated:
```python
audio_only_clients = {'ABS', 'Hardcover', 'BookLore'}
```

Without this, Booklore is excluded from sync for books without a `kosync_doc_id` (audio-only mode).

### Data Flow

**Booklore leads** (user listened in Booklore player):
```
ClientPoller → BookloreSyncClient.get_service_state()
  → BookloreClient.get_audiobook_progress(book_id)
    → GET /api/v1/books/{bookId} → parse positionMs + trackIndex
    → get_audiobook_info() → [track durations]
    → _ms_track_to_absolute_seconds() → absolute_seconds
  → ServiceState{ts: abs_seconds, pct: abs_seconds/duration}
  → delta > 60s → sync triggered
  → get_text_from_current_state() → alignment_service → text snippet
  → SyncManager pushes to ABS, KoSync, Storyteller, etc.
```

**ABS leads** (user listened in ABS, pushing to Booklore):
```
ABS socket event → SyncManager → ABS is leader
  → BookloreSyncClient.update_progress()
    → percentage * duration → absolute_seconds
    → _find_booklore_audiobook() → book_id
    → BookloreClient.update_audiobook_progress()
      → _absolute_seconds_to_ms_track() → (trackIndex, positionMs)
      → POST /api/v1/books/progress
```

### Key Design Decisions

1. **Route on `book.sync_mode` within existing client** — not a new `BookloreAudiobookSyncClient`. The framework expects one client per service, and `get_supported_sync_types()` already declares both types.

2. **Legacy `audiobookProgress` API variant** — avoids needing `bookFileId` (extra API call). Works on all Booklore versions.

3. **`State.timestamp` stores absolute seconds** — same convention as ABS. The state save code at `sync_manager.py:1376` already reads `state_data.get('ts')`.

4. **Track duration cache in `BookloreClient`** — API-level metadata belongs in the API client, not the sync client. Consistent with existing pattern.

5. **No new config vars required** — reuses `SYNC_DELTA_ABS_SECONDS` for audiobook delta threshold.

### Testing

Run via `./run-tests.sh` (Docker required).

**`tests/test_booklore_audiobook.py`** — Unit tests for `BookloreClient` audiobook methods:
- Track conversion: single-file, multi-track, edge cases (clamp at end)
- `get_audiobook_info`: ms vs seconds heuristic, caching behavior
- `get_audiobook_progress`: both API variants, no-progress case
- `update_audiobook_progress`: multi-track and single-file payloads

**`tests/test_booklore_sync_client_audiobook.py`** — Unit tests for audiobook routing:
- `get_service_state` routes correctly on `sync_mode`
- `get_text_from_current_state` uses alignment service in audiobook mode
- `update_progress` converts percentage → seconds → track position
- `_find_booklore_audiobook`: filename match, title fallback, ambiguous rejection

All tests mock `_make_request` — no HTTP calls. Follow existing `test_booklore_unittest.py` patterns.

---

## Cross-Edition Sync: Booklore EPUB ↔ Booklore Audiobook

### Scenario A: ABS + Booklore (covered by V1)

If the audiobook exists in **both ABS and Booklore**, cross-format sync works automatically once V1 is implemented:

1. ABS provides audio → Whisper transcription → alignment map (existing pipeline)
2. Booklore ebook progress is synced via existing ebook integration
3. Booklore audiobook progress is synced via V1 additions
4. The alignment map bridges the two formats:
   - Listen in Booklore player → audiobook position changes
   - V1 detects delta → alignment map converts timestamp → ebook text position
   - Pushes to Booklore ebook reader + KOReader + Storyteller

**No additional work needed beyond V1.**

### Scenario B: Booklore Standalone — No ABS Required (V3)

For audiobooks that exist **only in Booklore** (no ABS copy), the alignment pipeline currently fails because it hardcodes ABS as the audio source at three points:

```python
# sync_manager.py _run_background_job():
item_details = self.abs_client.get_item_details(abs_id)      # Line 773
chapters = item_details.get('media', {}).get('chapters', [])  # Line 817
audio_files = self.abs_client.get_audio_files(abs_id)         # Line 837
```

Everything downstream (transcription, alignment, storage) is already audio-source-agnostic. The `process_audio()` transcriber just wants `[{"stream_url": "...", "ext": "..."}]`.

#### What V3 requires

**1. `src/api/booklore_client.py` — New `get_audio_files(book_id)` method**

Returns the same shape as `ABSClient.get_audio_files()`:
```
Single-file:  [{"stream_url": "{base}/api/v1/audiobook/{bookId}/stream?token={jwt}", "ext": "m4b"}]
Multi-track:  [{"stream_url": "{base}/api/v1/audiobook/{bookId}/track/{i}/stream?token={jwt}", "ext": "mp3"} for each track]
```

**2. `src/sync_manager.py` — Abstract audio source in `_run_background_job()`**

Route based on `abs_id` prefix:
- `"li_*"` → ABS path (current behavior, unchanged)
- `"booklore-*"` → Booklore path:
  - `booklore_client.get_audiobook_info(bl_id)` for chapters + duration
  - `booklore_client.get_audio_files(bl_id)` for stream URLs
  - EPUB from existing fallback chain (already supports Booklore download)

**3. Synthetic `abs_id` for Booklore-only audiobooks**

Use `"booklore-{booklore_book_id}"` as the primary key. The `abs_id` column is just a unique string — no schema change needed.

**4. Book registration — new Booklore audiobook path**

Extension of `POST /match` or new endpoint:
- User picks a Booklore audiobook (audio-type books from cache)
- User picks an ebook (Booklore epub, or other source)
- Creates `Book` with `abs_id="booklore-{id}"`, `ebook_filename`, `status='pending'`, `duration` from audiobook info

**5. Skip ABS sync client for Booklore-only books**

The existing `sync_mode` filter at `sync_manager.py:1356` already skips ABS for ebook-only books. Add similar logic: if `abs_id` starts with `"booklore-"`, skip the ABS sync client.

#### Scope assessment

V3 touches more files and introduces an audio source abstraction in `_run_background_job()`. The core refactor is small (the 3 hardcoded ABS lines), but the registration flow and UI need design. Recommend implementing after V1 is proven stable.

---

## V2 — Incremental Enhancements

Between V1 and V3, these quality-of-life improvements:

- **`booklore_book_id` column** on `books` table (Alembic migration) — O(1) lookup after first match, avoids repeated title search
- **`fileProgress` write variant** — uses `bookFileId` for newer Booklore versions
- **Manual book linking UI** — dropdown on book detail page to explicitly link Booklore book ID to PageKeeper entry
- **Bulk audiobook progress prefetch** — enrich `_refresh_book_cache()` with audiobook progress data to avoid per-book API calls

---

## Phase Summary

| Phase | Scope | Key Benefit |
|-------|-------|-------------|
| **V1** | 4 files modified, 2 test files | Booklore audiobook sync + Scenario A cross-format sync |
| **V2** | DB migration + UI enhancements | Reliability + performance |
| **V3** | Audio source abstraction + registration flow | Scenario B: Booklore standalone (no ABS required) |
