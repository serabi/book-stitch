import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.api.storyteller_api import StorytellerAPIClient
from src.db.models import State
from src.services.alignment_service import AlignmentService, normalize_for_cross_format_comparison
from src.services.background_job_service import BackgroundJobService
from src.services.cache_cleanup_service import CacheCleanupService
from src.services.library_service import LibraryService
from src.services.migration_service import MigrationService
from src.services.progress_reset_service import ProgressResetService
from src.services.status_machine import StatusMachine
from src.services.suggestion_service import SuggestionService
from src.services.sync_manager_startup import SyncManagerStartup
from src.sync_clients.sync_client_interface import (
    LocatorResult,
    SyncClient,
    SyncResult,
    UpdateProgressRequest,
)
from src.utils.epub_resolver import get_local_epub

# Logging utilities (placed at top to ensure availability during sync)
from src.utils.logging_utils import sanitize_exception, sanitize_log_data

# Silence noisy third-party loggers
for noisy in ("urllib3", "requests", "schedule", "chardet", "multipart", "faster_whisper"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Only call basicConfig if logging hasn't been configured already (by memory_logger)
root_logger = logging.getLogger()
if not hasattr(root_logger, "_configured") or not root_logger._configured:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
logger = logging.getLogger(__name__)


class SyncManager:
    def __init__(
        self,
        container=None,
        abs_client=None,
        grimmory_client=None,
        hardcover_client=None,
        transcriber=None,
        ebook_parser=None,
        database_service=None,
        storyteller_client: StorytellerAPIClient = None,
        sync_clients: dict[str, SyncClient] = None,
        alignment_service: AlignmentService = None,
        library_service: LibraryService = None,
        migration_service: MigrationService = None,
        suggestion_service: SuggestionService = None,
        background_job_service: BackgroundJobService = None,
        epub_cache_dir=None,
        data_dir=None,
        books_dir=None,
    ):

        logger.info("=== Sync Manager Starting ===")
        # Use dependency injection
        self.container = container
        self.abs_client = abs_client
        self.grimmory_client = grimmory_client
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
        self.epub_cache_dir = epub_cache_dir or (
            self.data_dir / "epub_cache" if self.data_dir else Path("/data/epub_cache")
        )

        # Extracted services — auto-construct if not injected (backward compat for tests)
        if suggestion_service is not None:
            self.suggestion_service = suggestion_service
        else:
            self.suggestion_service = SuggestionService(
                database_service=database_service,
                abs_client=abs_client,
                grimmory_client=grimmory_client,
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
                grimmory_client=grimmory_client,
                ebook_parser=ebook_parser,
                transcriber=transcriber,
                alignment_service=alignment_service,
                library_service=library_service,
                storyteller_client=storyteller_client,
                storyteller_submission_service=None,
                epub_cache_dir=self.epub_cache_dir,
                data_dir=data_dir,
                books_dir=books_dir,
            )

        self._sync_lock = threading.Lock()
        self._pending_clears: set[int] = set()  # book ids awaiting clear during sync
        self._pending_clears_lock = threading.Lock()
        self._last_library_sync = 0

        self._setup_sync_clients(sync_clients or {})

        # ProgressResetService created internally (shares lock references with sync cycle)
        self.progress_reset_service = ProgressResetService(
            database_service=database_service,
            alignment_service=alignment_service,
            sync_clients=self.sync_clients,
            sync_lock=self._sync_lock,
            pending_clears=self._pending_clears,
            pending_clears_lock=self._pending_clears_lock,
        )

        self._startup = SyncManagerStartup(
            sync_clients=self.sync_clients,
            library_service=self.library_service,
            abs_client=self.abs_client,
            migration_service=self.migration_service,
        )
        self._cache_cleanup = CacheCleanupService(self.database_service, self.epub_cache_dir)

        self.startup_checks()
        self.background_job_service.cleanup_stale_jobs()
        self.background_job_service.prune_hardcover_sync_logs()

    def _setup_sync_clients(self, clients: dict[str, SyncClient]):
        self.sync_clients = {}
        for name, client in clients.items():
            if client.is_configured():
                self.sync_clients[name] = client
                logger.info(f"Sync client enabled: '{name}'")
            else:
                logger.debug(f"Sync client disabled/unconfigured: '{name}'")

    def startup_checks(self):
        self._startup.run()

    def cleanup_stale_jobs(self):
        """Delegate to BackgroundJobService."""
        self.background_job_service.cleanup_stale_jobs()

    def cleanup_cache(self):
        """Delete files from ebook cache that are not referenced in the DB."""
        self._cache_cleanup.cleanup()

    def get_audiobook_title(self, ab):
        media = ab.get("media", {})
        metadata = media.get("metadata", {})
        return metadata.get("title") or ab.get("name", "Unknown")

    def get_duration(self, ab):
        """Extract duration from audiobook media data."""
        media = ab.get("media", {})
        return media.get("duration", 0)

    def _normalize_for_cross_format_comparison(self, book, config):
        """Delegate to alignment_service module-level function."""
        # Ensure epub is available locally (download from Grimmory if needed)
        if book.ebook_filename and hasattr(self, "books_dir"):
            try:
                self._get_local_epub(book.ebook_filename)
            except Exception as e:
                logger.debug(f"'{book.abs_id}' EPUB prefetch failed: {sanitize_exception(e)}")

        return normalize_for_cross_format_comparison(
            book, config, self.sync_clients, self.ebook_parser, self.alignment_service
        )

    def _fetch_states_parallel(
        self, book, prev_states_by_client, title_snip, bulk_states_per_client=None, clients_to_use=None
    ):
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

                future = executor.submit(client.get_service_state, book, prev_state, title_snip, bulk_ctx)
                futures[future] = client_name

            for future in as_completed(futures, timeout=15):
                client_name = futures[future]
                try:
                    state = future.result()
                    if state is not None:
                        config[client_name] = state
                except Exception as e:
                    logger.warning(f"'{client_name}' state fetch failed: {sanitize_exception(e)}")

        return config

    def _get_local_epub(self, ebook_filename):
        """Get local path to EPUB file, downloading from Grimmory if necessary."""
        return get_local_epub(ebook_filename, self.books_dir, self.epub_cache_dir, self.grimmory_client)

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
        - API noise on long books (Grimmory's 20s rounding errors filtered)
        - Missing real progress on all books (30s+ changes do count)
        - A newly-added client reporting 0% from being elected leader
        """
        state = config[client_name]
        delta_pct = state.delta
        current_pct = state.current.get("pct", 0) or 0

        # Reject backward jumps to 0% — this is a new/reset client, not real reading
        if current_pct < 0.001 and state.previous_pct > 0.01:
            return False

        # Quick check: percentage threshold
        MIN_PCT_THRESHOLD = 0.0005  # 0.05%
        if delta_pct > MIN_PCT_THRESHOLD:
            return True

        # Time-based check (if we have duration info)
        if hasattr(book, "duration") and book.duration:
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
                pct = v.current.get("pct")
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
            logger.info(
                f"'{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (only client with change)"
            )
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
                    norm_label = f"{leader_ts:.1f}s" if "ABS" in config else f"char {leader_ts}"
                    logger.info(
                        f"'{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)} (normalized: {norm_label})"
                    )
                else:
                    # Fallback to percentage-based comparison among candidates
                    leader = max(candidates, key=candidates.get)
                    leader_pct = vals[leader]
                    logger.info(
                        f"'{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)}"
                    )
            else:
                # Same-format sync or normalization failed - use raw percentages
                leader = max(candidates, key=candidates.get)
                leader_pct = vals[leader]
                logger.info(f"'{abs_id}' '{title_snip}' {leader} leads at {config[leader].value_formatter(leader_pct)}")

        return leader, leader_pct

    def sync_cycle(self, target_book_id=None):
        """
        Run a sync cycle.

        Args:
            target_book_id: If provided, only sync this specific book by DB id (Instant Sync trigger).
                            Otherwise, sync all active books using bulk-poll optimization.
        """
        # Prevent race condition: If daemon is running, skip. If Instant Sync, wait.
        acquired = False
        if target_book_id:
            # Instant Sync: Block and wait for lock (up to 10s)
            acquired = self._sync_lock.acquire(timeout=10)
            if not acquired:
                logger.warning(f"Sync lock timeout for book_id={target_book_id} - skipping")
                return
        else:
            # Daemon: Non-blocking attempt
            acquired = self._sync_lock.acquire(blocking=False)
            if not acquired:
                logger.debug("Sync cycle skipped - another cycle is running")
                return

        try:
            self._sync_cycle_internal(target_book_id)
        except Exception as e:
            logger.error(f"Sync cycle internal error: {type(e).__name__}")
            logger.debug(traceback.format_exc())
        finally:
            self._sync_lock.release()

    def _sync_cycle_internal(self, target_book_id=None):
        # Clear caches at start of cycle
        storyteller_client = self.sync_clients.get("Storyteller")
        if storyteller_client and hasattr(storyteller_client, "storyteller_client"):
            if hasattr(storyteller_client.storyteller_client, "clear_cache"):
                storyteller_client.storyteller_client.clear_cache()

        # Refresh Library Metadata (Grimmory) — throttle to once per 15 minutes
        if self.library_service and (time.time() - self._last_library_sync > 900):
            self.library_service.sync_library_books()
            self._last_library_sync = time.time()

        # Discovery: promote ebook-mapped not_started books once they show real progress.
        # Must run before the active-only sync below, which would otherwise never touch them.
        if not target_book_id:
            try:
                self._promote_discovered_ebooks()
            except Exception as e:
                logger.warning(f"Ebook discovery promotion failed: {sanitize_exception(e)}")

        active_books, bulk_states_per_client = self._prepare_sync_books(target_book_id)
        if not active_books:
            self._process_deferred_clears()
            return

        # Main sync loop - process each active book
        for book in active_books:
            abs_id = book.abs_id

            # Skip books with pending clear — clear_progress will handle them
            with self._pending_clears_lock:
                skip = book.id in self._pending_clears
            if skip:
                logger.debug(f"'{abs_id}' Skipping sync — pending clear progress")
                continue

            try:
                self._sync_single_book(book, bulk_states_per_client)
            except Exception as e:
                logger.error(f"Sync error for '{abs_id}': {sanitize_exception(e)}")
                logger.debug(traceback.format_exc())

        logger.debug("End of sync cycle for active books")
        self._process_deferred_clears()

    def _promote_discovered_ebooks(self):
        """Promote not_started books with a mapped ebook to active once Grimmory reports progress.

        Newly-added ebook-mapped books sit at not_started and are never polled (the sync
        cycle only touches active books), so they would deadlock at 0%. This pass checks
        their Grimmory progress once per cycle and promotes them when it clears 1%.
        """
        if not (self.grimmory_client and self.grimmory_client.is_configured()):
            return

        # Exclude books with a deferred clear in flight — promoting them here would
        # resurrect stale Grimmory progress before _process_deferred_clears runs.
        with self._pending_clears_lock:
            pending = set(self._pending_clears)

        candidates = [
            b
            for b in self.database_service.get_books_by_status("not_started")
            if getattr(b, "ebook_filename", None) and b.id not in pending
        ]
        if not candidates:
            return

        status_machine = StatusMachine(self.database_service)
        for book in candidates:
            try:
                pct, _ = self.grimmory_client.get_progress(book.ebook_filename)
            except Exception as e:
                logger.debug(f"Discovery: progress fetch failed for '{book.ebook_filename}': {e}")
                continue
            status_machine.promote_not_started(book, pct, container=self.container, source_label="Discovery")

    def _prepare_sync_books(self, target_book_id):
        """Fetch active books, pre-fetch bulk states, and trigger suggestions."""
        active_books = []
        if target_book_id:
            book = self.database_service.get_book_by_id(target_book_id)
            logger.info(
                f"Instant Sync triggered for '{sanitize_log_data(book.title)}'"
                if book
                else f"Instant Sync triggered for book_id={target_book_id} (not found)"
            )
            if book and book.status == "active":
                active_books = [book]
        else:
            active_books = self.database_service.get_books_by_status("active")

        bulk_states_per_client = {}
        if not target_book_id:
            logger.debug(f"Sync cycle starting - {len(active_books)} active book(s)")
            for client_name, client in self.sync_clients.items():
                try:
                    bulk_data = client.fetch_bulk_state()
                    if bulk_data:
                        bulk_states_per_client[client_name] = bulk_data
                        logger.debug(f"Pre-fetched bulk state for {client_name}")
                except Exception as e:
                    logger.warning(f"Failed to pre-fetch bulk state for {client_name}: {sanitize_exception(e)}")

            # Check for suggestions (runs even with no active books)
            if "ABS" in bulk_states_per_client:
                self.check_for_suggestions(bulk_states_per_client["ABS"], active_books)

        return active_books, bulk_states_per_client

    def _sync_single_book(self, book, bulk_states_per_client):
        """Process a single book in the sync cycle."""
        abs_id = book.abs_id or f"book-{book.id}"
        title_snip = sanitize_log_data(book.title or "Unknown")
        logger.info(f"'{abs_id}' Syncing '{title_snip}'")

        # Migration upgrade
        if self.alignment_service:
            alignment = self.alignment_service._get_alignment(book.id)
            if alignment:
                if getattr(book, "transcript_file", None) != "DB_MANAGED":
                    logger.info(f"   Upgrading '{title_snip}' to DB_MANAGED unified architecture")
                    book.transcript_file = "DB_MANAGED"
                    self.database_service.save_book(book)

        # Get previous state for this book from database
        previous_states = self.database_service.get_states_for_book(book.id)
        prev_states_by_client = {}
        for state in previous_states:
            prev_states_by_client[state.client_name] = state

        # Determine active clients based on sync_mode
        sync_type = "ebook" if (hasattr(book, "sync_mode") and book.sync_mode == "ebook_only") else "audiobook"
        is_audio_only = sync_type == "audiobook" and not book.kosync_doc_id
        active_clients = {
            name: client for name, client in self.sync_clients.items() if sync_type in client.get_supported_sync_types()
        }
        if is_audio_only:
            audio_only_clients = {"ABS", "Hardcover"}
            active_clients = {name: client for name, client in active_clients.items() if name in audio_only_clients}
            logger.debug(f"'{abs_id}' '{title_snip}' Audio-only mode - using clients: {list(active_clients.keys())}")
        elif sync_type == "ebook":
            logger.debug(f"'{abs_id}' '{title_snip}' Ebook-only mode - using clients: {list(active_clients.keys())}")

        if not active_clients:
            logger.debug(f"'{abs_id}' '{title_snip}' No applicable sync clients, skipping")
            return

        # Build config using active_clients - parallel fetch
        config = self._fetch_states_parallel(
            book, prev_states_by_client, title_snip, bulk_states_per_client, active_clients
        )
        if not config:
            return

        # Check for ABS offline condition (only for audiobook mode)
        if not (hasattr(book, "sync_mode") and book.sync_mode == "ebook_only"):
            abs_state = config.get("ABS")
            if abs_state is None:
                ebook_clients_active = [k for k in config.keys() if k != "ABS"]
                if ebook_clients_active:
                    logger.info(
                        f"'{abs_id}' '{title_snip}' ABS audiobook not found/offline, falling back to ebook-only sync between {ebook_clients_active}"
                    )
                else:
                    logger.debug(f"'{abs_id}' '{title_snip}' ABS audiobook offline and no other clients, skipping")
                    return

        # Evaluate whether sync is needed
        should_sync = self._evaluate_sync_significance(config, book, abs_id, title_snip, prev_states_by_client)
        if not should_sync:
            return

        # Execute the sync update
        self._execute_sync_update(book, config, abs_id, title_snip, active_clients)

    def _evaluate_sync_significance(self, config, book, abs_id, title_snip, prev_states_by_client):
        """
        Check deltas and client discrepancies to determine if a sync update
        should proceed.

        Returns True if sync should proceed, False to skip.
        """
        # Check for sync delta threshold between clients
        progress_values = [cfg.current.get("pct", 0) for cfg in config.values() if cfg.current.get("pct") is not None]
        significant_diff = False
        max_progress = 0
        min_progress = 0

        if len(progress_values) >= 2:
            max_progress = max(progress_values)
            min_progress = min(progress_values)
            progress_diff = max_progress - min_progress

            if progress_diff >= self.sync_delta_between_clients:
                significant_diff = True
                logger.debug(
                    f"'{abs_id}' '{title_snip}' Detected discrepancies between clients ({progress_diff:.2%}), forcing sync check even if deltas are 0"
                )
                logger.debug(
                    f"'{abs_id}' '{title_snip}' Client discrepancy detected: {min_progress:.1%} to {max_progress:.1%}"
                )
            else:
                logger.debug(
                    f"'{abs_id}' '{title_snip}' Progress difference {progress_diff:.2%} below threshold {self.sync_delta_between_clients:.2%} - skipping sync"
                )

        deltas_zero = all(round(cfg.delta, 4) == 0 for cfg in config.values())

        # If nothing changed AND clients are effectively in sync, skip
        if deltas_zero and not significant_diff:
            logger.debug(f"'{abs_id}' '{title_snip}' No changes and clients in sync, skipping")
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
                delta_str = (
                    cfg.value_seconds_formatter(delta) if cfg.value_seconds_formatter else cfg.value_formatter(delta)
                )
                small_changes.append(f"✋ [{abs_id}] [{title_snip}] {label} delta {delta_str} (Below threshold)")

        if small_changes and not any(cfg.delta >= cfg.threshold for cfg in config.values()):
            if significant_diff:
                logger.debug(
                    f"'{abs_id}' '{title_snip}' Proceeding with sync despite small deltas due to client discrepancies"
                )
            else:
                for s in small_changes:
                    logger.info(s)
                return False

        logger.info(f"'{abs_id}' '{title_snip}' Change detected")

        # Status block - show only changed lines
        for _key, cfg in config.items():
            if cfg.delta > 0:
                prev = cfg.previous_pct
                curr = cfg.current.get("pct")
                label, fmt = cfg.display
                logger.info(f"{label}: {fmt.format(prev=prev, curr=curr)}")

        return True

    def _execute_sync_update(self, book, config, abs_id, title_snip, active_clients):
        """Resolve locator from leader, update followers, and save states."""
        leader, leader_pct = self._determine_leader(config, book, abs_id, title_snip)
        if not leader:
            return

        # Ensure epub is available locally (download from Grimmory if needed)
        if book.ebook_filename and hasattr(self, "books_dir"):
            try:
                self._get_local_epub(book.ebook_filename)
            except Exception as e:
                logger.debug(f"'{book.abs_id}' EPUB prefetch failed: {sanitize_exception(e)}")

        leader_client = self.sync_clients[leader]
        leader_state = config[leader]

        # Get canonical text from leader
        try:
            txt = leader_client.get_text_from_current_state(book, leader_state)
        except Exception as e:
            logger.warning(
                f"'{abs_id}' '{title_snip}' Error getting text from leader '{leader}': {sanitize_exception(e)}"
            )
            txt = None
        if not txt:
            logger.warning(
                f"'{abs_id}' '{title_snip}' Could not get text from leader '{leader}', persisting leader snapshot"
            )
            # Persist leader state so this delta isn't rediscovered on the next poll
            leader_current = leader_state.current
            state = State(
                abs_id=abs_id,
                book_id=book.id,
                client_name=leader.lower(),
                percentage=leader_current.get("pct"),
                timestamp=leader_current.get("ts"),
                xpath=leader_current.get("xpath"),
                cfi=leader_current.get("cfi"),
                last_updated=time.time(),
            )
            self.database_service.save_state(state)
            return

        # Get locator (percentage, xpath, etc) from text
        epub = book.ebook_filename
        locator = leader_client.get_locator_from_text(txt, epub, leader_pct)
        if not locator:
            # Try fallback if enabled (e.g. look at previous segment)
            if getattr(self.ebook_parser, "useXpathSegmentFallback", False):
                fallback_txt = leader_client.get_fallback_text(book, leader_state)
                if fallback_txt and fallback_txt != txt:
                    logger.info(
                        f"'{abs_id}' '{title_snip}' Primary text match failed. Trying previous segment fallback..."
                    )
                    locator = leader_client.get_locator_from_text(fallback_txt, epub, leader_pct)
                    if locator:
                        logger.info(f"'{abs_id}' '{title_snip}' Fallback successful!")

        if not locator:
            logger.warning(
                f"'{abs_id}' '{title_snip}' Could not resolve locator from text for leader '{leader}', falling back to percentage of leader"
            )
            locator = LocatorResult(percentage=leader_pct)

        # Update all other clients and store results
        results: dict[str, SyncResult] = {}
        for client_name, client_state in config.items():
            if client_name == leader:
                continue
            if client_name == "ABS" and hasattr(book, "sync_mode") and book.sync_mode == "ebook_only":
                continue
            client = self.sync_clients.get(client_name)
            if client is None:
                continue
            try:
                request = UpdateProgressRequest(locator, txt, previous_location=client_state.previous_pct)
                result = client.update_progress(book, request)
                results[client_name] = result
            except Exception as e:
                logger.warning(f"Failed to update '{client_name}': {sanitize_exception(e)}")
                results[client_name] = SyncResult(None, False)

        # Push to push-only clients (configured but didn't report state)
        for client_name, client in active_clients.items():
            if client_name in config or client_name == leader:
                continue
            try:
                request = UpdateProgressRequest(locator, txt, previous_location=0.0)
                result = client.update_progress(book, request)
                results[client_name] = result
            except Exception as e:
                logger.warning(f"Failed to update push-only client '{client_name}': {sanitize_exception(e)}")

        # Save states to database
        current_time = time.time()
        leader_state_data = leader_state.current

        leader_state_model = State(
            abs_id=book.abs_id,
            book_id=book.id,
            client_name=leader.lower(),
            last_updated=current_time,
            percentage=leader_state_data.get("pct"),
            timestamp=leader_state_data.get("ts"),
            xpath=leader_state_data.get("xpath"),
            cfi=leader_state_data.get("cfi"),
        )
        self.database_service.save_state(leader_state_model)

        for client_name, result in results.items():
            if result.success:
                state_data = result.updated_state if result.updated_state else {"pct": result.location}
                logger.info(f"'{abs_id}' '{title_snip}' Updated state data for '{client_name}': {state_data}")
                try:
                    from src.services.write_tracker import record_write

                    record_write(client_name, book.id, state_data)
                except ImportError:
                    pass
                client_state_model = State(
                    abs_id=book.abs_id,
                    book_id=book.id,
                    client_name=client_name.lower(),
                    last_updated=current_time,
                    percentage=state_data.get("pct"),
                    timestamp=state_data.get("ts"),
                    xpath=state_data.get("xpath"),
                    cfi=state_data.get("cfi"),
                )
                self.database_service.save_state(client_state_model)

        logger.info(f"'{abs_id}' '{title_snip}' States saved to database")

        # Flush logs to ensure we see this before any potential hard crash
        for handler in logger.handlers:
            handler.flush()
        if hasattr(root_logger, "handlers"):
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
                    logger.warning(f"Deferred clear failed for '{sanitize_log_data(pending_id)}': {type(e).__name__}")

    # ── Progress reset delegation (implementation in ProgressResetService) ──

    def clear_progress(self, abs_id):
        """Clear progress data for a specific book and reset all sync clients to 0%."""
        return self.progress_reset_service.clear_progress(abs_id)
