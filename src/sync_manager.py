import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import schedule

from src.api.storyteller_api import StorytellerAPIClient
from src.db.models import State
from src.services.alignment_service import AlignmentService
from src.services.background_job_service import BackgroundJobService
from src.services.library_service import LibraryService
from src.services.migration_service import MigrationService
from src.services.progress_reset_service import ProgressResetService
from src.services.suggestion_service import SuggestionService
from src.sync_clients.sync_client_interface import (
    LocatorResult,
    SyncClient,
    SyncResult,
    UpdateProgressRequest,
)
from src.utils.epub_resolver import get_local_epub

# Logging utilities (placed at top to ensure availability during sync)
from src.utils.logging_utils import sanitize_log_data

# Silence noisy third-party loggers
for noisy in ('urllib3', 'requests', 'schedule', 'chardet', 'multipart', 'faster_whisper'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Only call basicConfig if logging hasn't been configured already (by memory_logger)
root_logger = logging.getLogger()
if not hasattr(root_logger, '_configured') or not root_logger._configured:
    logging.basicConfig(
        level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    )
logger = logging.getLogger(__name__)


class SyncManager:
    def __init__(self,
                 abs_client=None,
                 booklore_client=None,
                 hardcover_client=None,
                 transcriber=None,
                 ebook_parser=None,
                 database_service=None,
                 storyteller_client: StorytellerAPIClient=None,
                 sync_clients: dict[str, SyncClient]=None,
                 alignment_service: AlignmentService = None,
                 library_service: LibraryService = None,
                 migration_service: MigrationService = None,
                 suggestion_service: SuggestionService = None,
                 background_job_service: BackgroundJobService = None,
                 epub_cache_dir=None,
                 data_dir=None,
                 books_dir=None):

        logger.info("=== Sync Manager Starting ===")
        # Use dependency injection
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self._booklore_clients = [booklore_client] if booklore_client else []
        self.hardcover_client = hardcover_client
        self.transcriber = transcriber
        self.ebook_parser = ebook_parser
        self.database_service = database_service
        self.storyteller_client = storyteller_client

        self.alignment_service = alignment_service
        self.library_service = library_service
        self.migration_service = migration_service

        self.data_dir = data_dir
        self.books_dir = books_dir

        try:
            val = float(os.getenv("SYNC_DELTA_BETWEEN_CLIENTS_PERCENT", 1))
        except (ValueError, TypeError):
            logger.warning("Invalid SYNC_DELTA_BETWEEN_CLIENTS_PERCENT value, defaulting to 1")
            val = 1.0
        self.sync_delta_between_clients = val / 100.0
        self.delta_chars_thresh = 2000  # ~400 words
        self.epub_cache_dir = epub_cache_dir or (self.data_dir / "epub_cache" if self.data_dir else Path("/data/epub_cache"))

        # Extracted services — auto-construct if not injected (backward compat for tests)
        if suggestion_service is not None:
            self.suggestion_service = suggestion_service
        else:
            self.suggestion_service = SuggestionService(
                database_service=database_service,
                abs_client=abs_client,
                booklore_clients=self._booklore_clients,
                storyteller_client=storyteller_client,
                library_service=library_service,
                books_dir=books_dir,
                ebook_parser=ebook_parser,
            )

        if background_job_service is not None:
            self.background_job_service = background_job_service
        else:
            self.background_job_service = BackgroundJobService(
                database_service=database_service,
                abs_client=abs_client,
                booklore_clients=self._booklore_clients,
                ebook_parser=ebook_parser,
                transcriber=transcriber,
                alignment_service=alignment_service,
                library_service=library_service,
                storyteller_client=storyteller_client,
                epub_cache_dir=self.epub_cache_dir,
                data_dir=data_dir,
                books_dir=books_dir,
            )

        self._sync_lock = threading.Lock()
        self._pending_clears: set[str] = set()  # abs_ids awaiting clear during sync
        self._pending_clears_lock = threading.Lock()
        self._last_library_sync = 0

        self._setup_sync_clients(sync_clients)

        # ProgressResetService created internally (shares lock references with sync cycle)
        self.progress_reset_service = ProgressResetService(
            database_service=database_service,
            alignment_service=alignment_service,
            sync_clients=self.sync_clients,
            sync_lock=self._sync_lock,
            pending_clears=self._pending_clears,
            pending_clears_lock=self._pending_clears_lock,
        )

        self.startup_checks()
        self.background_job_service.cleanup_stale_jobs()


    def _setup_sync_clients(self, clients: dict[str, SyncClient]):
        self.sync_clients = {}
        for name, client in clients.items():
            if client.is_configured():
                self.sync_clients[name] = client
                logger.info(f"Sync client enabled: '{name}'")
            else:
                logger.debug(f"Sync client disabled/unconfigured: '{name}'")

    def startup_checks(self):
        # Check configured sync clients
        for client_name, client in (self.sync_clients or {}).items():
            try:
                client.check_connection()
                logger.info(f"'{client_name}' connection verified")
            except Exception as first_err:
                time.sleep(2)
                try:
                    client.check_connection()
                    logger.info(f"'{client_name}' connection verified (retry)")
                except Exception as e:
                    logger.warning(f"'{client_name}' connection failed after retry: {e} (first attempt: {first_err})")

        # Check CWA Integration Status
        if self.library_service and self.library_service.cwa_client:
            cwa = self.library_service.cwa_client
            if cwa.is_configured():
                # check_connection() logs its own Success/Fail messages and verifies Authentication
                if cwa.check_connection():
                    # If connected, ensure search template is cached
                    template = cwa._get_search_template()
                    if template:
                        logger.info(f"   CWA search template: {template}")
            else:
                logger.debug("CWA not configured (disabled or missing server URL)")
        else:
            logger.debug("CWA not available (library_service or cwa_client missing)")

        # Check ABS ebook search capability
        if self.abs_client and self.abs_client.is_configured():
            try:
                # Just verify methods exist (don't actually search during startup)
                if hasattr(self.abs_client, 'get_ebook_files') and hasattr(self.abs_client, 'search_ebooks'):
                    logger.info("ABS ebook methods available (get_ebook_files, search_ebooks)")
                else:
                    logger.warning("ABS ebook methods missing - ebook search may not work")
            except Exception as e:
                logger.warning(f"ABS ebook check failed: {e}")

        # Run one-time migration
        if self.migration_service:
            logger.info("Checking for legacy data to migrate...")
            self.migration_service.migrate_legacy_data()

        # Cleanup orphaned cache files
        # DISABLED: Current logic is too aggressive (deletes original_ebook_filename for linked books).
        # We rely on delete_mapping in web_server.py to handle explicit deletions.

    def cleanup_stale_jobs(self):
        """Delegate to BackgroundJobService."""
        self.background_job_service.cleanup_stale_jobs()

    def cleanup_cache(self):
        """Delete files from ebook cache that are not referenced in the DB."""
        if not self.epub_cache_dir.exists():
            return

        logger.info("Starting ebook cache cleanup...")

        try:
            # 1. Collect all valid filenames from DB
            valid_filenames = set()

            # From Active Books
            books = self.database_service.get_all_books()
            for book in books:
                if book.ebook_filename:
                    valid_filenames.add(book.ebook_filename)

            # From Pending Suggestions (covers auto-discovery matches)
            suggestions = self.database_service.get_all_pending_suggestions()
            for suggestion in suggestions:
                # matches property automatically parses the JSON
                for match in suggestion.matches:
                    if match.get('filename'):
                        valid_filenames.add(match['filename'])

            # 2. Iterate cache and delete orphans
            deleted_count = 0
            reclaimed_bytes = 0

            for file_path in self.epub_cache_dir.iterdir():
                # Only check files, and ensure we don't delete if it's in our valid list
                if file_path.is_file() and file_path.name not in valid_filenames:
                    try:
                        size = file_path.stat().st_size
                        file_path.unlink()
                        deleted_count += 1
                        reclaimed_bytes += size
                        logger.debug(f"   Deleted orphaned cache file: {file_path.name}")
                    except Exception as e:
                        logger.warning(f"   Failed to delete {file_path.name}: {e}")

            if deleted_count > 0:
                mb = reclaimed_bytes / (1024 * 1024)
                logger.info(f"Cache cleanup complete: Removed {deleted_count} files ({mb:.2f} MB)")
            else:
                logger.info("Cache is clean (no orphaned files found)")

        except Exception as e:
            logger.error(f"Error during cache cleanup: {e}")

    def get_abs_title(self, ab):
        media = ab.get('media', {})
        metadata = media.get('metadata', {})
        return metadata.get('title') or ab.get('name', 'Unknown')

    def get_duration(self, ab):
        """Extract duration from audiobook media data."""
        media = ab.get('media', {})
        return media.get('duration', 0)

    def _normalize_for_cross_format_comparison(self, book, config):
        """
        Normalize positions for cross-format comparison (audiobook vs ebook).

        When syncing between audiobook (ABS) and ebook clients (KoSync, etc.),
        raw percentages are not comparable because:
        - Audiobook % = time position / total duration
        - Ebook % = text position / total text

        These don't correlate linearly. This method converts ebook positions
        to equivalent audiobook timestamps using text-matching, enabling
        accurate comparison of "who is further in the story".

        Returns:
            dict: {client_name: normalized_timestamp} for comparison,
                  or None if normalization not possible/needed
        """
        # Check if we have both ABS and ebook clients in the mix
        has_abs = 'ABS' in config
        ebook_clients = [k for k in config.keys() if k != 'ABS']

        if not ebook_clients:
            # ABS-only, nothing to compare across formats
            return None

        if not has_abs:
            # Ebook-only path: normalize via character offsets in the shared EPUB
            if not book.ebook_filename or len(ebook_clients) < 2:
                return None
            try:
                book_path = self.ebook_parser.resolve_book_path(book.ebook_filename)
                full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
                total_text_len = len(full_text)
            except Exception as e:
                logger.debug(f"'{book.abs_id}' Could not load ebook for normalization: {e}")
                return None
            if not total_text_len:
                return None
            normalized = {}
            for client_name in ebook_clients:
                client = self.sync_clients.get(client_name)
                if not client:
                    continue
                client_state = config[client_name]
                client_pct = client_state.current.get('pct', 0)
                try:
                    client_pct = max(0.0, min(1.0, float(client_pct)))
                except (TypeError, ValueError):
                    client_pct = 0.0
                try:
                    text_snippet = client.get_text_from_current_state(book, client_state)
                    if text_snippet:
                        loc = self.ebook_parser.find_text_location(
                            book.ebook_filename, text_snippet,
                            hint_percentage=client_pct
                        )
                        if loc and loc.match_index is not None:
                            normalized[client_name] = loc.match_index
                            logger.debug(f"'{book.abs_id}' Normalized '{client_name}' {client_pct:.2%} -> char {loc.match_index}")
                            continue
                except Exception as e:
                    logger.debug(f"'{book.abs_id}' Text-based normalization failed for '{client_name}': {e}")
                # Fallback: percentage-derived offset
                normalized[client_name] = int(client_pct * total_text_len)
                logger.debug(f"'{book.abs_id}' Normalized '{client_name}' {client_pct:.2%} -> char {int(client_pct * total_text_len)} (pct fallback)")
            return normalized if len(normalized) > 1 else None

        if not book.transcript_file:
            logger.debug(f"'{book.abs_id}' No transcript available for cross-format normalization")
            return None

        normalized = {}

        # ABS already has timestamp
        abs_state = config['ABS']
        abs_ts = abs_state.current.get('ts', 0)
        normalized['ABS'] = abs_ts

        # For each ebook client, get their text and find equivalent timestamp
        for client_name in ebook_clients:
            client = self.sync_clients.get(client_name)
            if not client:
                continue

            client_state = config[client_name]
            client_pct = client_state.current.get('pct', 0)

            try:
                # Get character offset from the ebook position for precise alignment
                book_path = self.ebook_parser.resolve_book_path(book.ebook_filename)
                full_text, _ = self.ebook_parser.extract_text_and_map(book_path)
                total_text_len = len(full_text)

                char_offset = int(client_pct * total_text_len)
                txt = full_text[max(0, char_offset - 400):min(total_text_len, char_offset + 400)]

                if not txt:
                    logger.debug(f"'{book.abs_id}' Could not get text from '{client_name}' for normalization")
                    continue

                # Find equivalent timestamp in audiobook using the precise aligner if available
                if self.alignment_service:
                    ts_for_text = self.alignment_service.get_time_for_text(
                        book.abs_id, txt,
                        char_offset_hint=char_offset
                    )
                else:
                    # Fallback or strict error?
                    ts_for_text = None

                if ts_for_text is not None:
                    normalized[client_name] = ts_for_text
                    logger.debug(f"'{book.abs_id}' Normalized '{client_name}' {client_pct:.2%} -> {ts_for_text:.1f}s")
                else:
                    logger.debug(f"'{book.abs_id}' Could not find timestamp for '{client_name}' text")
            except Exception as e:
                logger.warning(f"'{book.abs_id}' Cross-format normalization failed for '{client_name}': {e}")

        # Only return if we successfully normalized at least one ebook client
        if len(normalized) > 1:
            return normalized
        return None


    def _fetch_states_parallel(self, book, prev_states_by_client, title_snip, bulk_states_per_client=None, clients_to_use=None):
        """Fetch states from specified clients (or all if not specified) in parallel."""
        clients_to_use = clients_to_use or self.sync_clients
        config = {}
        bulk_states_per_client = bulk_states_per_client or {}

        with ThreadPoolExecutor(max_workers=len(clients_to_use)) as executor:
            futures = {}
            for client_name, client in clients_to_use.items():
                prev_state = prev_states_by_client.get(client_name.lower())

                # Get bulk context from the unified dict
                bulk_ctx = bulk_states_per_client.get(client_name)

                future = executor.submit(
                    client.get_service_state, book, prev_state, title_snip, bulk_ctx
                )
                futures[future] = client_name

            for future in as_completed(futures, timeout=15):
                client_name = futures[future]
                try:
                    state = future.result()
                    if state is not None:
                        config[client_name] = state
                except Exception as e:
                    logger.warning(f"'{client_name}' state fetch failed: {e}")

        return config





    def _get_local_epub(self, ebook_filename):
        """Get local path to EPUB file, downloading from Booklore if necessary."""
        return get_local_epub(
            ebook_filename, self.books_dir, self.epub_cache_dir, self._booklore_clients
        )

    # ── Suggestion delegation (implementation in SuggestionService) ──

    def queue_suggestion(self, abs_id: str) -> None:
        """Queue suggestion discovery for an unmapped book (called from socket listener)."""
        self.suggestion_service.queue_suggestion(abs_id)

    def check_for_suggestions(self, abs_progress_map, active_books):
        """Check for unmapped books with progress and create suggestions."""
        self.suggestion_service.check_for_suggestions(abs_progress_map, active_books)

    # ── Background job delegation (implementation in BackgroundJobService) ──

    def check_pending_jobs(self):
        """Check for pending jobs and run them in a background thread."""
        self.background_job_service.check_pending_jobs()

    def _has_significant_delta(self, client_name, config, book):
        """
        Check if a client has a significant delta using hybrid time/percentage logic.

        Returns True if:
        - Percentage delta > 0.05% (catches large jumps)
        - OR absolute time delta > 30 seconds (catches small but real progress)

        This prevents:
        - API noise on short books (0.3s changes don't count)
        - API noise on long books (BookLore's 20s rounding errors filtered)
        - Missing real progress on all books (30s+ changes do count)
        - A newly-added client reporting 0% from being elected leader
        """
        state = config[client_name]
        delta_pct = state.delta
        current_pct = state.current.get('pct', 0) or 0

        # Reject backward jumps to 0% — this is a new/reset client, not real reading
        if current_pct < 0.001 and state.previous_pct > 0.01:
            return False

        # Quick check: percentage threshold
        MIN_PCT_THRESHOLD = 0.0005  # 0.05%
        if delta_pct > MIN_PCT_THRESHOLD:
            return True

        # Time-based check (if we have duration info)
        if hasattr(book, 'duration') and book.duration:
            delta_seconds = delta_pct * book.duration
            MIN_TIME_THRESHOLD = 30  # seconds
            if delta_seconds > MIN_TIME_THRESHOLD:
                return True

        return False

    def _determine_leader(self, config, book, abs_id, title_snip):
        """
        Determines which client should be the leader based on:
        1. Most recent change (delta > threshold)
        2. Furthest progress (fallback)
        3. Cross-format normalization (if needed)

        Returns:
            tuple: (leader_client_name, leader_percentage) or (None, None)
        """
        # Build vals from config - only include clients that can be leaders
        vals = {}
        for k, v in config.items():
            client = self.sync_clients[k]
            if client.can_be_leader():
                pct = v.current.get('pct')
                if pct is not None:
                    vals[k] = pct

        # Ensure we have at least one potential leader
        if not vals:
            logger.warning(f"'{abs_id}' '{title_snip}' No clients available to be leader")
            return None, None

        # Check which clients have changed (delta > minimum threshold)
        # "Most recent change wins" - if only one client changed, it becomes the leader
        # Use hybrid time/percentage logic to filter out phantom API noise
        clients_with_delta = {k: v for k, v in vals.items() if self._has_significant_delta(k, config, book)}

        leader = None
        leader_pct = None

        if len(clients_with_delta) == 1:
            # Only one client changed - that client is the leader (most recent change wins)
            leader = list(clients_with_delta.keys())[0]
            leader_pct = vals[leader]
            logger.info(f"'{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (only client with change)")
        else:
            # Multiple clients changed or this is a discrepancy resolution
            # Use "furthest wins" logic among changed clients (or all if none changed)
            candidates = clients_with_delta if clients_with_delta else vals

            # For cross-format sync (audiobook vs ebook), use normalized timestamps
            normalized_positions = self._normalize_for_cross_format_comparison(book, config)

            if normalized_positions and len(normalized_positions) > 1:
                # Filter normalized positions to only include candidates
                normalized_candidates = {k: v for k, v in normalized_positions.items() if k in candidates}
                if normalized_candidates:
                    leader = max(normalized_candidates, key=normalized_candidates.get)
                    leader_ts = normalized_candidates[leader]
                    leader_pct = vals[leader]
                    norm_label = f"{leader_ts:.1f}s" if 'ABS' in config else f"char {leader_ts}"
                    logger.info(f"'{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (normalized: {norm_label})")
                else:
                    # Fallback to percentage-based comparison among candidates
                    leader = max(candidates, key=candidates.get)
                    leader_pct = vals[leader]
                    logger.info(f"'{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)}")
            else:
                # Same-format sync or normalization failed - use raw percentages
                leader = max(candidates, key=candidates.get)
                leader_pct = vals[leader]
                logger.info(f"'{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)}")

        return leader, leader_pct

    def sync_cycle(self, target_abs_id=None):
        """
        Run a sync cycle.

        Args:
            target_abs_id: If provided, only sync this specific book (Instant Sync trigger).
                           Otherwise, sync all active books using bulk-poll optimization.
        """
        # Prevent race condition: If daemon is running, skip. If Instant Sync, wait.
        acquired = False
        if target_abs_id:
             # Instant Sync: Block and wait for lock (up to 10s)
             acquired = self._sync_lock.acquire(timeout=10)
             if not acquired:
                 logger.warning(f"Sync lock timeout for '{target_abs_id}' - skipping")
                 return
        else:
             # Daemon: Non-blocking attempt
             acquired = self._sync_lock.acquire(blocking=False)
             if not acquired:
                 logger.debug("Sync cycle skipped - another cycle is running")
                 return

        try:
            self._sync_cycle_internal(target_abs_id)
        except Exception as e:
            logger.error(f"Sync cycle internal error: {e}")
            # Log traceback for robust debugging
            logger.error(traceback.format_exc())
        finally:
            self._sync_lock.release()

    def _sync_cycle_internal(self, target_abs_id=None):
        # Clear caches at start of cycle
        storyteller_client = self.sync_clients.get('Storyteller')
        if storyteller_client and hasattr(storyteller_client, 'storyteller_client'):
            if hasattr(storyteller_client.storyteller_client, 'clear_cache'):
                storyteller_client.storyteller_client.clear_cache()

        # Refresh Library Metadata (Booklore) — throttle to once per 15 minutes
        if self.library_service and (time.time() - self._last_library_sync > 900):
            self.library_service.sync_library_books()
            self._last_library_sync = time.time()

        active_books, bulk_states_per_client = self._prepare_sync_books(target_abs_id)
        if not active_books:
            return

        # Main sync loop - process each active book
        for book in active_books:
            abs_id = book.abs_id

            # Skip books with pending clear — clear_progress will handle them
            with self._pending_clears_lock:
                skip = abs_id in self._pending_clears
            if skip:
                logger.debug(f"'{abs_id}' Skipping sync — pending clear progress")
                continue

            try:
                self._sync_single_book(book, bulk_states_per_client)
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"Sync error: {e}")

        logger.debug("End of sync cycle for active books")
        self._process_deferred_clears()

    def _prepare_sync_books(self, target_abs_id):
        """Fetch active books, pre-fetch bulk states, and trigger suggestions."""
        active_books = []
        if target_abs_id:
            logger.info(f"Instant Sync triggered for '{target_abs_id}'")
            book = self.database_service.get_book(target_abs_id)
            if book and book.status == 'active':
                active_books = [book]
        else:
            active_books = self.database_service.get_books_by_status('active')

        bulk_states_per_client = {}
        if not target_abs_id and active_books:
            logger.debug(f"Sync cycle starting - {len(active_books)} active book(s)")
            for client_name, client in self.sync_clients.items():
                bulk_data = client.fetch_bulk_state()
                if bulk_data:
                    bulk_states_per_client[client_name] = bulk_data
                    logger.debug(f"Pre-fetched bulk state for {client_name}")

            # Check for suggestions
            if 'ABS' in bulk_states_per_client:
                self.check_for_suggestions(bulk_states_per_client['ABS'], active_books)

        return active_books, bulk_states_per_client

    def _sync_single_book(self, book, bulk_states_per_client):
        """Process a single book in the sync cycle."""
        abs_id = book.abs_id
        title_snip = sanitize_log_data(book.abs_title or 'Unknown')
        logger.info(f"'{abs_id}' Syncing '{title_snip}'")

        # Migration upgrade
        if self.alignment_service:
            alignment = self.alignment_service._get_alignment(abs_id)
            if alignment:
                if getattr(book, 'transcript_file', None) != 'DB_MANAGED':
                    logger.info(f"   Upgrading '{title_snip}' to DB_MANAGED unified architecture")
                    book.transcript_file = 'DB_MANAGED'
                    self.database_service.save_book(book)

        # Get previous state for this book from database
        previous_states = self.database_service.get_states_for_book(abs_id)
        prev_states_by_client = {}
        for state in previous_states:
            prev_states_by_client[state.client_name] = state

        # Determine active clients based on sync_mode
        sync_type = 'ebook' if (hasattr(book, 'sync_mode') and book.sync_mode == 'ebook_only') else 'audiobook'
        is_audio_only = (sync_type == 'audiobook' and not book.kosync_doc_id)
        active_clients = {
            name: client for name, client in self.sync_clients.items()
            if sync_type in client.get_supported_sync_types()
        }
        if is_audio_only:
            audio_only_clients = {'ABS', 'Hardcover'}
            active_clients = {name: client for name, client in active_clients.items() if name in audio_only_clients}
            logger.debug(f"'{abs_id}' '{title_snip}' Audio-only mode - using clients: {list(active_clients.keys())}")
        elif sync_type == 'ebook':
            logger.debug(f"'{abs_id}' '{title_snip}' Ebook-only mode - using clients: {list(active_clients.keys())}")

        # Build config using active_clients - parallel fetch
        config = self._fetch_states_parallel(book, prev_states_by_client, title_snip, bulk_states_per_client, active_clients)
        if not config:
            return

        # Check for ABS offline condition (only for audiobook mode)
        if not (hasattr(book, 'sync_mode') and book.sync_mode == 'ebook_only'):
            abs_state = config.get('ABS')
            if abs_state is None:
                ebook_clients_active = [k for k in config.keys() if k != 'ABS']
                if ebook_clients_active:
                     logger.info(f"'{abs_id}' '{title_snip}' ABS audiobook not found/offline, falling back to ebook-only sync between {ebook_clients_active}")
                else:
                     logger.debug(f"'{abs_id}' '{title_snip}' ABS audiobook offline and no other clients, skipping")
                     return

        # Evaluate whether sync is needed
        should_sync = self._evaluate_sync_significance(config, book, abs_id, title_snip, prev_states_by_client)
        if not should_sync:
            return

        # Execute the sync update
        self._execute_sync_update(book, config, abs_id, title_snip)

    def _evaluate_sync_significance(self, config, book, abs_id, title_snip, prev_states_by_client):
        """
        Check deltas, thresholds, character-level checks, and discrepancies
        to determine if a sync update should proceed.

        Returns True if sync should proceed, False to skip.
        """
        # Check for sync delta threshold between clients
        progress_values = [cfg.current.get('pct', 0) for cfg in config.values() if cfg.current.get('pct') is not None]
        significant_diff = False
        max_progress = 0
        min_progress = 0

        if len(progress_values) >= 2:
            max_progress = max(progress_values)
            min_progress = min(progress_values)
            progress_diff = max_progress - min_progress

            if progress_diff >= self.sync_delta_between_clients:
                significant_diff = True
                logger.debug(f"'{abs_id}' '{title_snip}' Detected discrepancies between clients ({progress_diff:.2%}), forcing sync check even if deltas are 0")
                logger.debug(f"'{abs_id}' '{title_snip}' Client discrepancy detected: {min_progress:.1%} to {max_progress:.1%}")
            else:
                logger.debug(f"'{abs_id}' '{title_snip}' Progress difference {progress_diff:.2%} below threshold {self.sync_delta_between_clients:.2%} - skipping sync")

        # Check for Character Delta Threshold
        char_delta_triggered = False
        if not significant_diff and hasattr(book, 'ebook_filename') and book.ebook_filename:
            for client_name_key, client_state in config.items():
                 if client_state.delta > 0:
                     try:
                         epub_path = self._get_local_epub(book.original_ebook_filename or book.ebook_filename)
                         if not epub_path:
                             logger.warning(f"Could not locate or download EPUB for '{book.ebook_filename}'")
                             continue

                         full_text, _ = self.ebook_parser.extract_text_and_map(epub_path)
                         if full_text:
                             total_chars = len(full_text)
                             char_delta = int(client_state.delta * total_chars)

                             if char_delta >= self.delta_chars_thresh:
                                 logger.info(f"'{abs_id}' '{title_snip}' Significant character change detected for '{client_name_key}': {char_delta} chars (Threshold: {self.delta_chars_thresh})")
                                 significant_diff = True
                                 char_delta_triggered = True
                                 break
                     except Exception as e:
                         logger.warning(f"Failed to check char delta for '{client_name_key}': {e}")

        deltas_zero = all(round(cfg.delta, 4) == 0 for cfg in config.values())
        any_significant_delta = any(
            self._has_significant_delta(k, config, book)
            for k in config.keys()
        )

        # If nothing changed AND clients are effectively in sync, skip
        if deltas_zero and not significant_diff:
            logger.debug(f"'{abs_id}' '{title_snip}' No changes and clients in sync, skipping")
            return False

        # Check for discrepancy without activity
        new_client_in_config = any(
            client_name.lower() not in prev_states_by_client
            for client_name in config.keys()
        )
        client_needs_catchup = significant_diff and any(
            (cfg.current.get('pct', 0) or 0) < 0.001 and max_progress > 0.05
            for cfg in config.values()
        )
        if significant_diff and not any_significant_delta and not char_delta_triggered and not new_client_in_config and not client_needs_catchup:
            logger.debug(f"'{abs_id}' '{title_snip}' Discrepancy exists ({max_progress*100:.1f}% vs {min_progress*100:.1f}%) but no recent client activity detected. Waiting for a new read event to determine true leader")
            return False

        if significant_diff:
            logger.debug(f"'{abs_id}' '{title_snip}' Proceeding due to client discrepancy")

        # Small changes (below thresholds) should be noisy-reduced
        small_changes = []
        for key, cfg in config.items():
            delta = cfg.delta
            threshold = cfg.threshold

            if delta is None or threshold is None:
                 logger.debug(f"'{title_snip}' '{key}' delta={delta}, threshold={threshold}")

            if delta is not None and threshold is not None and 0 < delta < threshold:
                label, fmt = cfg.display
                delta_str = cfg.value_seconds_formatter(delta) if cfg.value_seconds_formatter else cfg.value_formatter(delta)
                small_changes.append(f"✋ [{abs_id}] [{title_snip}] {label} delta {delta_str} (Below threshold)")

        if small_changes and not any(cfg.delta >= cfg.threshold for cfg in config.values()):
            if significant_diff:
                logger.debug(f"'{abs_id}' '{title_snip}' Proceeding with sync despite small deltas due to client discrepancies")
            else:
                for s in small_changes:
                    logger.info(s)
                return False

        logger.info(f"'{abs_id}' '{title_snip}' Change detected")

        # Status block - show only changed lines
        for _key, cfg in config.items():
            if cfg.delta > 0:
                prev = cfg.previous_pct
                curr = cfg.current.get('pct')
                label, fmt = cfg.display
                logger.info(f"{label}: {fmt.format(prev=prev, curr=curr)}")

        return True

    def _execute_sync_update(self, book, config, abs_id, title_snip):
        """Resolve locator from leader, update followers, and save states."""
        leader, leader_pct = self._determine_leader(config, book, abs_id, title_snip)
        if not leader:
            return

        leader_client = self.sync_clients[leader]
        leader_state = config[leader]

        # Get canonical text from leader
        txt = leader_client.get_text_from_current_state(book, leader_state)
        if not txt:
            logger.warning(f"'{abs_id}' '{title_snip}' Could not get text from leader '{leader}'")
            return

        # Get locator (percentage, xpath, etc) from text
        epub = book.ebook_filename
        locator = leader_client.get_locator_from_text(txt, epub, leader_pct)
        if not locator:
            # Try fallback if enabled (e.g. look at previous segment)
            if getattr(self.ebook_parser, 'useXpathSegmentFallback', False):
                fallback_txt = leader_client.get_fallback_text(book, leader_state)
                if fallback_txt and fallback_txt != txt:
                    logger.info(f"'{abs_id}' '{title_snip}' Primary text match failed. Trying previous segment fallback...")
                    locator = leader_client.get_locator_from_text(fallback_txt, epub, leader_pct)
                    if locator:
                        logger.info(f"'{abs_id}' '{title_snip}' Fallback successful!")

        if not locator:
            logger.warning(f"'{abs_id}' '{title_snip}' Could not resolve locator from text for leader '{leader}', falling back to percentage of leader")
            locator = LocatorResult(percentage=leader_pct)

        # Update all other clients and store results
        results: dict[str, SyncResult] = {}
        for client_name, client in self.sync_clients.items():
            if client_name == leader:
                continue
            if client_name == 'ABS' and hasattr(book, 'sync_mode') and book.sync_mode == 'ebook_only':
                continue
            try:
                request = UpdateProgressRequest(locator, txt, previous_location=config.get(client_name).previous_pct if config.get(client_name) else None)
                result = client.update_progress(book, request)
                results[client_name] = result
            except Exception as e:
                logger.warning(f"Failed to update '{client_name}': {e}")
                results[client_name] = SyncResult(None, False)

        # Save states to database
        current_time = time.time()
        leader_state_data = leader_state.current

        leader_state_model = State(
            abs_id=book.abs_id,
            client_name=leader.lower(),
            last_updated=current_time,
            percentage=leader_state_data.get('pct'),
            timestamp=leader_state_data.get('ts'),
            xpath=leader_state_data.get('xpath'),
            cfi=leader_state_data.get('cfi')
        )
        self.database_service.save_state(leader_state_model)

        for client_name, result in results.items():
            if result.success:
                state_data = result.updated_state if result.updated_state else {'pct': result.location}
                logger.info(f"'{abs_id}' '{title_snip}' Updated state data for '{client_name}': {state_data}")
                client_state_model = State(
                    abs_id=book.abs_id,
                    client_name=client_name.lower(),
                    last_updated=current_time,
                    percentage=state_data.get('pct'),
                    timestamp=state_data.get('ts'),
                    xpath=state_data.get('xpath'),
                    cfi=state_data.get('cfi')
                )
                self.database_service.save_state(client_state_model)

        logger.info(f"'{abs_id}' '{title_snip}' States saved to database")

        # Flush logs to ensure we see this before any potential hard crash
        for handler in logger.handlers:
            handler.flush()
        if hasattr(root_logger, 'handlers'):
            for handler in root_logger.handlers:
                handler.flush()

    def _process_deferred_clears(self):
        """Process any pending clears that couldn't acquire the lock earlier."""
        with self._pending_clears_lock:
            pending = list(self._pending_clears)
        if pending:
            logger.info(f"Processing {len(pending)} deferred clear(s): {pending}")
            for pending_id in pending:
                try:
                    self.progress_reset_service._reset_external_clients(pending_id)
                    self.progress_reset_service._finalize_clear_status(pending_id)
                    with self._pending_clears_lock:
                        self._pending_clears.discard(pending_id)
                except Exception as e:
                    logger.warning(f"Deferred clear failed for '{pending_id}': {e}")

    # ── Progress reset delegation (implementation in ProgressResetService) ──

    def clear_progress(self, abs_id):
        """Clear progress data for a specific book and reset all sync clients to 0%."""
        return self.progress_reset_service.clear_progress(abs_id)

    def run_daemon(self):
        """Legacy method - daemon is now run from web_server.py"""
        logger.warning("run_daemon() called — daemon should be started from web_server.py instead")
        schedule.every(int(os.getenv("SYNC_PERIOD_MINS", 5))).minutes.do(self.sync_cycle)
        schedule.every(1).minutes.do(self.check_pending_jobs)
        logger.info("Daemon started")
        self.sync_cycle()
        while True:
            schedule.run_pending()
            time.sleep(30)

if __name__ == "__main__":
    # This is only used for standalone testing - production uses web_server.py
    logger.info("Running sync manager in standalone mode (for testing)")

    from src.utils.di_container import create_container
    di_container = create_container()
    # Try to use dependency injection, fall back to legacy if there are issues
    sync_manager = di_container.sync_manager()
    logger.info("Using dependency injection")

    sync_manager.run_daemon()
