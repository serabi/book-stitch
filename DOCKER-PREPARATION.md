# Docker Distribution Preparation Audit

Audit of PageKeeper's Docker setup in preparation for public distribution, covering ARM/Pi compatibility, transcription guidance, and resource consumption.

---

## 1. Raspberry Pi (ARM64) Compatibility

**Compatible with two changes.**

### Blockers

| Issue | Location | Fix |
|-------|----------|-----|
| `platform: linux/amd64` hardcoded | `docker-compose.yml:7` | Remove line (example file is already clean) |
| `LD_LIBRARY_PATH` references NVIDIA/CUDA paths | `Dockerfile:7` | Harmless on ARM (paths won't exist), but should be conditional on `INSTALL_GPU` |

### Dependency ARM64 Status

| Package | ARM64? | Notes |
|---------|--------|-------|
| `python:3.13-slim` base image | Yes | Official multi-arch (amd64 + arm64) |
| `ffmpeg` / `libavcodec-extra` | Yes | Available in Debian ARM64 repos |
| `flask`, `sqlalchemy`, `alembic`, `requests`, `beautifulsoup4` | Yes | Pure Python |
| `faster-whisper` (-> ctranslate2) | **Risky** | ARM64 wheels may not exist; compilation is slow/fragile |
| `lxml` | Yes* | May need `libxml2-dev` + `libxslt1-dev` build deps on ARM |
| `nh3` | Yes* | Rust-based; needs Rust toolchain or pre-built wheel |
| `rapidfuzz` | Yes* | Cython; ARM64 wheels available on PyPI for most versions |
| `deepgram-sdk` | Yes | Pure HTTP client |

*\* = may need build tools added to Dockerfile for ARM builds*

### Bottom Line

The core app (Flask, sync, database, UI) runs fine on ARM64. The only real pain point is `faster-whisper` / `ctranslate2` for local transcription. Everything else either has ARM wheels or compiles with minimal extra build deps.

---

## 2. Transcription on Raspberry Pi

**Recommend external whisper.cpp server or Deepgram API key for Pi users.**

### Current Transcription Architecture (3 providers)

| Provider | Config | Pi-Friendly? |
|----------|--------|-------------|
| **Local Whisper** (default) | `TRANSCRIPTION_PROVIDER=local`, uses `faster-whisper` | No - ctranslate2 ARM builds are fragile; even "tiny" model takes ~15-20 min/hr of audio on Pi |
| **Whisper.cpp** (external) | `TRANSCRIPTION_PROVIDER=whispercpp` + `WHISPER_CPP_URL` | Yes - PageKeeper just makes HTTP calls |
| **Deepgram** (cloud API) | `TRANSCRIPTION_PROVIDER=deepgram` + `DEEPGRAM_API_KEY` | Yes - pure HTTP, no local compute |

### Recommendations for Pi Users

1. **Best option**: External whisper.cpp server on a more powerful machine (self-hosted, no API cost)
2. **Easiest option**: Deepgram API key (cloud, pay-per-use)
3. **Possible but painful**: Local whisper with "tiny" model (slow, may fail to build ctranslate2)

### Planned Change: Split `faster-whisper` into Optional Dependency

Currently `faster-whisper==1.2.1` is always installed even if the user picks Deepgram or whisper.cpp.

**Approach**: Split into `requirements.txt` (core) and `requirements-whisper.txt` (local whisper) with a Dockerfile build arg. This lets Pi/ARM users skip ctranslate2 entirely when using an external transcription provider.

---

## 3. Current Resource Consumption

### Base Container (idle / sync-only)

| Resource | Usage |
|----------|-------|
| **RAM** | ~80-150 MB (Flask + SQLite + sync threads) |
| **CPU** | Minimal - periodic sync via `schedule`, no persistent workers |
| **Disk (image)** | ~800 MB (python:3.13-slim + ffmpeg + pip packages) |
| **Disk (data)** | Varies - SQLite DB is small; audio/epub caches grow with library |
| **Network** | Light - API calls to ABS, Hardcover, KOReader, etc. |

### During Transcription (heaviest workload)

| Resource | Usage |
|----------|-------|
| **RAM** | +500 MB to +8 GB depending on Whisper model (tiny ~150MB, large ~6GB+) |
| **CPU** | 4 threads pinned (configurable via faster-whisper, `cpu_threads=4`) |
| **Disk I/O** | FFmpeg writes temp WAV files; chunks up to 45 min each |
| **GPU (optional)** | CUDA if available (+~800MB for NVIDIA libs in image) |

### Threading Model

- **No Celery/Redis** - lightweight Python threading
- `BackgroundJobService`: 1 daemon thread for transcription jobs
- `SyncManager`: ThreadPoolExecutor with workers = number of active sync clients
- `BookloreClient`: ThreadPoolExecutor with max 10 workers for batch fetches
- `ABSSocketListener`: 1 worker thread for suggestion queue

### Database

- **SQLite** (`/data/pagekeeper.db`) - no external DB needed
- NullPool (no connection pooling, appropriate for SQLite)
- Alembic migrations run on startup

### Caching

- In-memory LRU transcript cache (capacity: 3)
- Filesystem: `/data/audio_cache/`, `/data/epub_cache/`
- No Redis or external cache service

---

## Recommended Changes for Public Distribution

### Must-do

1. **Remove `platform: linux/amd64`** from dev `docker-compose.yml` (example file is already clean)
2. **Split `faster-whisper` into optional dependency** - separate `requirements-whisper.txt` + Dockerfile build arg
3. **Document transcription provider recommendations** for Pi/ARM users in README

### Nice-to-have

4. **Add resource limit suggestions** to `docker-compose.example.yml`:
   ```yaml
   deploy:
     resources:
       limits:
         memory: 512M  # bump to 2G+ if using local whisper
   ```
5. **Clean up `LD_LIBRARY_PATH`** in Dockerfile - only set when `INSTALL_GPU=true`

### Files to Modify

- `Dockerfile` - conditional LD_LIBRARY_PATH, optional whisper deps via build arg
- `docker-compose.yml` - remove `platform: linux/amd64`
- `docker-compose.example.yml` - add resource limit suggestions
- `requirements.txt` - split out `faster-whisper` to `requirements-whisper.txt`
- `src/utils/transcription_providers.py` - graceful ImportError handling for faster-whisper
- README - document ARM/Pi guidance and transcription provider recommendations

### Verification Steps

- Build on ARM64 (or `docker buildx build --platform linux/arm64`) and confirm all pip packages install
- Run container on ARM64 with `TRANSCRIPTION_PROVIDER=whispercpp` and verify transcription works via HTTP
- Run container with no transcription to verify core sync/UI works on ARM with minimal resources
- Check `docker stats` for baseline memory/CPU usage

---

## 4. Storyteller Integration Review

### Overview

The Storyteller integration is well-structured: a clean API client (`src/api/storyteller_api.py`, 333 lines), a sync client implementing the `SyncClient` interface (`src/sync_clients/storyteller_sync_client.py`, 153 lines), and a native word-timeline alignment path that bypasses Whisper entirely.

### How It Works

1. **Auth**: POST to `/api/token` with username/password, gets JWT (cached 30s, auto-refreshed on 401)
2. **Sync**: Reads/writes progress via `/api/v2/books/{uuid}/positions` with rich locator data (href, fragment, CFI, chapter progress)
3. **Linking**: Strict UUID-only — books must be explicitly linked via UI search modal. No filename fallback.
4. **Alignment**: Can read Storyteller's `wordTimeline` transcription files directly from the filesystem, skipping Whisper transcription entirely

### What's Good

- **Clean separation**: API client, sync client, and alignment are all independent. Storyteller is fully optional (`STORYTELLER_ENABLED=false`).
- **Rich position data**: Sends href, fragment, CSS selector, CFI, chapter progress — not just a percentage. This means Storyteller can resume to the exact paragraph.
- **Graceful error handling**: 409 timestamp conflicts treated as success (prevents retry loops). Connection failures logged but don't crash sync.
- **Native alignment priority**: `background_job_service.py` checks for Storyteller `wordTimeline` first (Priority 1), then SMIL, then Whisper. This is the fastest path and uses zero extra compute.
- **Path traversal defense**: `get_word_timeline_chapters()` validates resolved paths stay within the assets root.
- **Bidirectional sync**: Participates in both audiobook and ebook sync modes, can win leader election.

### Issues Found

#### 1. Unused Volume Mounts in Dev Compose

`docker-compose.yml` mounts two volumes that **no code references**:

```yaml
- /Volumes/data/media/downloads/storyteller-ingest:/linker_books:rw   # UNUSED
- /Volumes/data/media/storyteller:/readalouds:ro                      # UNUSED
```

Grepping `src/` for `readaloud`, `linker_book`, and `/import` returns zero matches. These appear to be artifacts from the old Forge pipeline (removed per CHANGELOG). They're not in `docker-compose.example.yml` (good), but should be cleaned from the dev compose.

#### 2. `docker-compose.example.yml` Has No Storyteller Guidance

The example compose file has zero mention of Storyteller. Users who want Storyteller native alignment need to know to add:

```yaml
volumes:
  - /path/to/storyteller/processing:/storyteller-data:ro
```

And set `STORYTELLER_ASSETS_DIR=/storyteller-data` in the Settings UI. This is documented in `README.md` but not in the example compose file where users will actually configure things.

#### 3. `search_books()` Fetches Entire Library Client-Side

`storyteller_api.py:223-257` — `search_books()` calls `GET /api/v2/books` (fetching ALL books), then filters in Python. This is noted in `TODO.md` already. For small libraries this is fine, but for users with large Storyteller libraries it could be slow. Storyteller doesn't expose a server-side search endpoint, so this may be unavoidable.

#### 4. `get_all_positions_bulk()` Makes N+1 API Calls

`storyteller_api.py:133-148` — Fetches positions by calling `get_position_details()` for each book individually. This is an N+1 query pattern — 1 call for the book list + 1 call per book for positions. If Storyteller has a bulk positions endpoint, this could be optimized. Otherwise, for large libraries the sync cycle will include many sequential HTTP calls.

#### 5. Word-Timeline Directory Resolution Uses Book Title

`storyteller_api.py:290-298` — The transcription directory name is `{title}{suffix}` (e.g., `Boxcar Children [BMt8MyCX]`). This works because Storyteller uses the same convention, but it's fragile if:
- A user renames a book in Storyteller after processing
- Title contains filesystem-unfriendly characters

Verified against actual Storyteller data: the naming convention matches (`processing/assets/{title}/transcriptions/00006-00001.json`). The suffix format `[BMt8MyCX]` is used for duplicate titles.

#### 6. No `STORYTELLER_ASSETS_DIR` in Example Compose Environment

The `docker-compose.example.yml` doesn't include `STORYTELLER_ASSETS_DIR` in its environment block. Since settings are configured via the web UI this is technically fine, but a commented example would help discoverability.

### Storyteller on Raspberry Pi

Storyteller itself is the heavier service (it runs its own Whisper transcription to generate the `wordTimeline` data). But from **PageKeeper's perspective**, the Storyteller integration is very Pi-friendly:

- **API calls**: Pure HTTP (Bearer token auth, JSON payloads) — negligible resources
- **Native alignment**: Reads pre-computed JSON files from Storyteller's filesystem — no transcription needed
- **Key insight**: If a Pi user runs Storyteller on a separate, more powerful machine and mounts its `processing/` directory (e.g., via NFS), PageKeeper on the Pi gets word-level alignment for free without any local transcription

This makes Storyteller the **ideal pairing for Pi users** — it shifts all the heavy compute (Whisper transcription) to the Storyteller host, and PageKeeper just reads the results.

### Recommended Changes

| Priority | Change | File |
|----------|--------|------|
| Clean up | Remove unused `/linker_books` and `/readalouds` mounts | `docker-compose.yml` |
| Documentation | Add commented Storyteller volume mount to example compose | `docker-compose.example.yml` |
| Documentation | Highlight Storyteller native alignment as recommended Pi path | README |
| Nice-to-have | Note N+1 positions fetch as future optimization | `TODO.md` (already noted for search) |
