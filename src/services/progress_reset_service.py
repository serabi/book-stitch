import logging
import os
import threading
import time
import traceback

from src.db.models import State
from src.sync_clients.sync_client_interface import (
    LocatorResult,
    UpdateProgressRequest,
)
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


class ProgressResetService:
    """Handles clearing progress and resetting sync clients to 0%."""

    def __init__(self,
                 database_service,
                 alignment_service,
                 sync_clients: dict,
                 sync_lock: threading.Lock,
                 pending_clears: set,
                 pending_clears_lock: threading.Lock):
        self.database_service = database_service
        self.alignment_service = alignment_service
        self.sync_clients = sync_clients
        self._sync_lock = sync_lock
        self._pending_clears = pending_clears
        self._pending_clears_lock = pending_clears_lock

    def clear_progress(self, abs_id):
        """
        Clear progress data for a specific book and reset all sync clients to 0%.

        Phase 1 (immediate, no lock): clears local DB states and reading dates.
        Phase 2 (lock required): resets external clients to 0% and handles book status.
        If the sync lock is busy, Phase 1 still takes effect and 0% states are saved
        with a recent timestamp so the sync daemon won't overwrite them.

        Args:
            abs_id: The book ID to clear progress for

        Returns:
            dict: Summary of cleared data
        """
        try:
            logger.info(f"Clearing progress for book {sanitize_log_data(abs_id)}...")

            book = self.database_service.get_book(abs_id)
            if not book:
                raise ValueError(f"Book not found: {abs_id}")

            # Mark book so the sync daemon skips it while we're clearing
            with self._pending_clears_lock:
                self._pending_clears.add(abs_id)

            # ── Phase 1: Immediate DB cleanup (no lock needed) ──
            cleared_count = self.database_service.delete_states_for_book(abs_id)
            logger.info(f"Cleared {cleared_count} state records from database")

            # Delete KOSync document records to prevent stale re-sync
            sibling_docs = self.database_service.get_kosync_documents_for_book(abs_id)
            for doc in sibling_docs:
                self.database_service.delete_kosync_document(doc.document_hash)
                logger.info(f"Deleted KOSync document record: {doc.document_hash[:8]}...")
            if not sibling_docs and book.kosync_doc_id:
                self.database_service.delete_kosync_document(book.kosync_doc_id)
                logger.info(f"Deleted KOSync document record: {book.kosync_doc_id[:8]}...")

            # Save 0% states with a fresh timestamp so the sync daemon sees
            # "already up to date" and won't pull stale progress from external services
            now = time.time()
            for client_name in self.sync_clients:
                if client_name == 'ABS' and book.sync_mode == 'ebook_only':
                    continue
                state = State(
                    abs_id=abs_id,
                    client_name=client_name.lower(),
                    percentage=0.0,
                    timestamp=now,
                    last_updated=now
                )
                self.database_service.save_state(state)

            # Clear reading timestamps so the book appears as "not started"
            self.database_service.update_book_reading_fields(abs_id, started_at=None, finished_at=None)

            # Set not_started immediately so the sync daemon won't pick this book up
            if book.status not in ('pending', 'processing'):
                book.status = 'not_started'
                self.database_service.save_book(book)

            logger.info("Phase 1 complete: local states cleared, 0% states saved")

            # ── Phase 2: Reset external clients (needs sync lock) ──
            acquired = self._sync_lock.acquire(timeout=30)
            if not acquired:
                logger.warning(f"Sync lock busy — external clients will be reset on next clear attempt. "
                               f"Local progress already cleared for '{sanitize_log_data(abs_id)}'")
                # Keep abs_id in _pending_clears so _process_deferred_clears picks it up
                return {
                    'book_id': abs_id,
                    'book_title': book.abs_title,
                    'database_states_cleared': cleared_count,
                    'client_reset_results': {},
                    'successful_resets': 0,
                    'total_clients': 0,
                    'note': 'Local DB cleared; external client reset deferred (sync cycle running)',
                }
            try:
                reset_results = self._reset_external_clients(abs_id)

                summary = {
                    'book_id': abs_id,
                    'book_title': book.abs_title,
                    'database_states_cleared': cleared_count,
                    'client_reset_results': reset_results,
                    'successful_resets': sum(1 for r in reset_results.values() if r['success']),
                    'total_clients': len(reset_results)
                }

                # Handle alignment-based re-processing (status already set to not_started in Phase 1)
                self._finalize_clear_status(abs_id)

                logger.info(f"Progress clearing completed for '{sanitize_log_data(book.abs_title)}'")
                logger.info(f"   Database states cleared: {cleared_count}")
                logger.info(f"   Client resets: {summary['successful_resets']}/{summary['total_clients']} successful")

                return summary
            finally:
                self._sync_lock.release()
                with self._pending_clears_lock:
                    self._pending_clears.discard(abs_id)

        except Exception as e:
            with self._pending_clears_lock:
                self._pending_clears.discard(abs_id)
            logger.error(f"Error clearing progress for {sanitize_log_data(abs_id)}: {type(e).__name__}")
            logger.debug(traceback.format_exc())
            raise RuntimeError(f"Failed to clear progress for book {sanitize_log_data(abs_id)}") from e

    def _finalize_clear_status(self, abs_id):
        """Handle smart-reset status finalization after clearing progress."""
        smart_reset = os.getenv('REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT', 'true').lower() == 'true'
        if not smart_reset:
            logger.info("   Reset progress to 0% (Smart re-process disabled)")
            return

        has_alignment = bool(self.alignment_service and self.alignment_service.has_alignment(abs_id))
        if has_alignment:
            logger.info(f"   Alignment map exists for '{sanitize_log_data(abs_id)}' — no re-transcription needed")
        else:
            book = self.database_service.get_book(abs_id)
            if book:
                book.status = 'pending'
                self.database_service.save_book(book)
                logger.info(f"   Book '{sanitize_log_data(abs_id)}' marked 'pending' for alignment check")

    def _reset_external_clients(self, abs_id):
        """Push 0% progress to all external sync clients for a book.

        Returns:
            dict: Mapping of client_name -> {'success': bool, 'message': str}.
                  Empty dict if book not found.
        """
        from src.services.write_tracker import record_write

        book = self.database_service.get_book(abs_id)
        if not book:
            return {}
        reset_results = {}
        locator = LocatorResult(percentage=0.0)
        request = UpdateProgressRequest(locator_result=locator, txt="", previous_location=None)
        for client_name, client in self.sync_clients.items():
            if client_name == 'ABS' and book.sync_mode == 'ebook_only':
                logger.debug(f"'{book.abs_title}' Ebook-only mode - skipping ABS progress reset")
                continue
            try:
                result = client.update_progress(book, request)
                reset_results[client_name] = {
                    'success': result.success,
                    'message': 'Reset to 0%' if result.success else 'Failed to reset'
                }
                if result.success:
                    record_write(client_name, book.abs_id)
                    logger.info(f"Reset '{client_name}' to 0%")
                else:
                    logger.warning(f"Failed to reset '{client_name}'")
            except Exception as e:
                reset_results[client_name] = {
                    'success': False,
                    'message': str(e)
                }
                logger.warning(f"Error resetting '{client_name}': {e}")
        return reset_results
