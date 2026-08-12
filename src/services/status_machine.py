"""Centralized status transition logic for books.

All status transitions flow through StatusMachine.transition() to ensure
consistent side effects (journal entries, date filling, HC push, etc.).
"""

import logging
from dataclasses import dataclass
from datetime import date

from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

# Valid book statuses
VALID_STATUSES = {"active", "completed", "paused", "dnf", "not_started"}

# A not_started book is promoted to active only once reported progress clears this
# fraction, mirroring the >1% "genuinely reading" bar used across the pipeline.
PROGRESS_ACTIVATION_THRESHOLD = 0.01

# Status → journal event mapping
EVENT_MAP = {
    "completed": "finished",
    "paused": "paused",
    "dnf": "dnf",
}


@dataclass(frozen=True)
class TransitionSideEffects:
    journal: bool = True
    fill_dates: bool = True
    push_hardcover: bool = True
    cleanup_tbr: bool = True
    push_grimmory: bool = True


SOURCE_EFFECTS = {
    "local": TransitionSideEffects(),
    "auto_complete": TransitionSideEffects(push_grimmory=False),
    "completion_sync": TransitionSideEffects(push_hardcover=False),
    "manual_progress": TransitionSideEffects(journal=False, push_hardcover=False, cleanup_tbr=False, push_grimmory=False),
}


class StatusMachine:
    """Single entry point for all book status transitions.

    The `source` parameter controls which side effects fire:
    - 'local': journal + HC push + Grimmory push + TBR cleanup + date fill
    - 'auto_complete': journal + date fill + HC push + TBR cleanup
    - 'completion_sync': journal + Grimmory push + TBR cleanup + date fill
    - 'manual_progress': date fill only
    """

    def __init__(self, database_service):
        self.database_service = database_service

    def transition(self, book, new_status, source, *, container=None, dates=None, allowed_from=None):
        """Transition a book to a new status with appropriate side effects.

        Args:
            book: Book model instance (will be mutated).
            new_status: Target status string.
            source: Transition source. Valid values are 'local' for full local
                status changes, 'auto_complete' for automatic completion without
                Grimmory push, 'completion_sync' for completion sync without
                Hardcover push, and 'manual_progress' for date fill only.
            container: DI container (needed for HC push, Grimmory push, date pull).
            dates: Optional dict with 'started_at'/'finished_at' to use instead of pulling.
            allowed_from: If set, only allow transition from these statuses.

        Returns:
            dict with 'success', 'status', 'previous_status', and optionally 'error'.
        """
        effects = SOURCE_EFFECTS.get(source)
        if effects is None:
            return {"success": False, "error": f"Invalid transition source: {source}"}

        if new_status not in VALID_STATUSES:
            return {"success": False, "error": f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}"}

        old_status = book.status

        if allowed_from is not None and old_status not in allowed_from:
            return {"success": False, "error": f"Cannot change to '{new_status}' from status '{old_status}'"}

        if old_status == new_status:
            if source == "completion_sync" and effects.push_grimmory and book.ebook_filename and container:
                self._push_to_grimmory(book, new_status, old_status, container)
            return {"success": True, "status": new_status, "previous_status": old_status}

        # Apply status change
        book.status = new_status
        if new_status == "active":
            book.activity_flag = False
        self.database_service.save_book(book)

        if effects.journal:
            self._record_journal(book, new_status, old_status, source, container)

        if effects.fill_dates:
            self._fill_dates(book, new_status, old_status, source, container, dates)

        if new_status in ("active", "paused", "dnf", "completed"):
            logger.info(f"Book status changed to '{new_status}': '{sanitize_log_data(book.title or book.abs_id)}'")

        # HC push
        if effects.push_hardcover and container:
            self._push_to_hardcover(book, new_status, container)

        if effects.cleanup_tbr and new_status in ("active", "completed", "paused", "dnf"):
            self._cleanup_tbr(book)

        if effects.push_grimmory and book.ebook_filename and container:
            self._push_to_grimmory(book, new_status, old_status, container)

        return {"success": True, "status": new_status, "previous_status": old_status}

    def promote_not_started(self, book, pct, *, container=None, source_label=""):
        """Promote a not_started book to active once external progress clears the bar.

        Shared by the ABS socket listener and the sync-cycle discovery pass. Returns
        the transition result dict when a promotion is attempted, or None when the
        reported progress is at/below the activation threshold (caller does nothing).
        """
        if not pct or pct <= PROGRESS_ACTIVATION_THRESHOLD:
            return None

        result = self.transition(book, "active", source="completion_sync", container=container)
        prefix = f"{source_label}: " if source_label else ""
        title = sanitize_log_data(book.title)
        if result.get("success"):
            logger.info(f"{prefix}Promoted not_started book '{title}' to active ({pct:.1%} progress)")
        else:
            logger.warning(f"{prefix}Failed to promote '{title}': {result.get('error')}")
        return result

    def _cleanup_tbr(self, book):
        """Remove any TBR item for this book, falling back to HC book ID lookup."""
        deleted = self.database_service.delete_tbr_by_book_id(book.id)
        if not deleted:
            hc = self.database_service.get_hardcover_details(book.id)
            if hc and hc.hardcover_book_id:
                tbr = self.database_service.find_tbr_by_hardcover_id(int(hc.hardcover_book_id))
                if tbr:
                    self.database_service.delete_tbr_item(tbr.id)

    def _record_journal(self, book, new_status, old_status, source, container):
        """Create journal entry for the transition."""
        if new_status == "active":
            # Use old_status to decide: unread→started, anything else→resumed
            if old_status in ("not_started", "unread"):
                self.database_service.add_reading_journal(book.id, event="started", abs_id=book.abs_id)
            else:
                self.database_service.add_reading_journal(book.id, event="resumed", abs_id=book.abs_id)
            return

        event = EVENT_MAP.get(new_status)
        if event:
            pct = 1.0 if event == "finished" else None
            self.database_service.add_reading_journal(book.id, event=event, percentage=pct, abs_id=book.abs_id)

    def _fill_dates(self, book, new_status, old_status, source, container, dates):
        """Fill in started_at/finished_at based on the transition."""
        updates = {}

        if new_status == "active":
            if not book.started_at:
                updates["started_at"] = self._resolve_started_at(book.id, container, dates)
        elif new_status == "completed":
            if not book.finished_at:
                updates["finished_at"] = (dates or {}).get("finished_at") or date.today().isoformat()
                if not book.started_at:
                    updates["started_at"] = self._resolve_started_at(book.id, container, dates)
            else:
                # Re-read — increment read count
                updates["read_count"] = (book.read_count or 1) + 1

        if updates:
            self.database_service.update_book_reading_fields(book.id, **updates)

    def _resolve_started_at(self, book_id, container, dates):
        """Get started_at from provided dates, external pull, or today."""
        if dates and dates.get("started_at"):
            return dates["started_at"]
        if container:
            try:
                pulled = container.reading_date_service().pull_reading_dates(book_id)
                return pulled.get("started_at", date.today().isoformat())
            except Exception as e:
                logger.debug("Could not pull started_at for book_id=%s: %s", book_id, e)
        return date.today().isoformat()

    def _push_to_hardcover(self, book, new_status, container):
        """Push status change to Hardcover."""
        try:
            hc_service = container.hardcover_service()
            if hc_service.is_configured():
                hc_service.push_local_status(book, new_status)
        except Exception as e:
            logger.debug(f"Could not push status to Hardcover: {e}")

    def _push_to_grimmory(self, book, new_status, old_status, container):
        """Push read status to Grimmory for relevant transitions."""
        from src.services.reading_date_service import push_grimmory_read_status

        if new_status == "active" and old_status in ("dnf", "paused", "not_started", "completed"):
            push_grimmory_read_status(book, container, "READING")
        elif new_status == "completed":
            push_grimmory_read_status(book, container, "READ")
