"""Service for reading date sync: pulling/pushing dates, auto-completing finished books."""

import logging
import time
from datetime import UTC, datetime

from src.db.models import State
from src.services.hardcover_log_service import log_hardcover_action
from src.services.hardcover_service import PROGRESS_COMPLETE_THRESHOLD

logger = logging.getLogger(__name__)


def push_grimmory_read_status(book, container, status):
    """Push a read status (READING, READ, etc.) to Grimmory."""
    try:
        bl_client = container.grimmory_client()
        if bl_client.is_configured():
            bl_client.update_read_status(book.ebook_filename, status)
    except Exception as e:
        logger.debug(f"Could not push Grimmory status '{status}': {e}")


class ReadingDateService:
    """Handles reading date synchronization and auto-completion detection.

    Pure date operations (pull/push dates) use injected clients directly.
    Orchestration methods (auto_complete, sync) receive container for sync client access.
    """

    def __init__(self, database_service, hardcover_client, abs_client, kobo_service=None):
        self.database_service = database_service
        self.hardcover_client = hardcover_client
        self.abs_client = abs_client
        self.kobo_service = kobo_service

    def pull_reading_dates(self, book_id):
        """Pull started_at and finished_at for a book: ABS first, Kobo as fallback.

        Hardcover dates are only pulled once at match time (via HardcoverService._pull_dates_at_match),
        not during the sync cycle. Kobo device dates (Event-table first-opens,
        DateLastRead on finished books) only fill keys ABS didn't provide.

        Returns dict with 'started_at' and/or 'finished_at' keys (YYYY-MM-DD strings).
        Only includes keys where a date was found.
        """
        dates = {}

        try:
            book = self.database_service.get_book_by_id(book_id)
            if not book or not book.abs_id:
                return dates

            if self.abs_client.is_configured():
                progress = self.abs_client.get_progress(book.abs_id)
                if progress:
                    if progress.get("startedAt"):
                        dates["started_at"] = (
                            datetime.fromtimestamp(progress["startedAt"] / 1000, tz=UTC).date().isoformat()
                        )
                    if progress.get("finishedAt"):
                        dates["finished_at"] = (
                            datetime.fromtimestamp(progress["finishedAt"] / 1000, tz=UTC).date().isoformat()
                        )
                    if dates:
                        logger.debug(f"Pulled dates from ABS for '{book.abs_id}': {dates}")
        except Exception as e:
            logger.debug(f"Could not pull dates from ABS for book_id={book_id}: {e}")

        if self.kobo_service:
            try:
                for key, value in self.kobo_service.reading_dates_for(book_id).items():
                    if value and key not in dates:
                        dates[key] = value
            except Exception as e:
                logger.debug(f"Could not pull dates from Kobo for book_id={book_id}: {e}")

        return dates

    def push_dates_to_hardcover(self, book_id, *, force=False):
        """Push local started_at/finished_at to Hardcover.

        By default, only fills in missing dates on the Hardcover side.
        When force=True (user-initiated edit), overwrites existing HC dates.

        Returns (success: bool, message: str).
        """
        try:
            if not self.hardcover_client.is_configured():
                return False, "Hardcover is not configured"

            hc_details = self.database_service.get_hardcover_details(book_id)
            if not hc_details or not hc_details.hardcover_book_id:
                return False, "Book is not linked to Hardcover"

            book = self.database_service.get_book_by_id(book_id)
            if not book:
                return False, "Book not found"

            if not book.started_at and not book.finished_at:
                return False, "No local dates to sync"

            user_book = self.hardcover_client.find_user_book(int(hc_details.hardcover_book_id))
            if not user_book:
                return False, "Book not found in your Hardcover library — try unlinking and re-linking"

            reads = user_book.get("user_book_reads", [])
            if not reads:
                edition_id = user_book.get("edition_id")
                new_read_id = self.hardcover_client.create_read_with_dates(
                    user_book["id"],
                    started_at=book.started_at,
                    finished_at=book.finished_at,
                    edition_id=edition_id,
                )
                if not new_read_id:
                    return False, "Failed to create reading session on Hardcover"
                log_hardcover_action(
                    self.database_service,
                    abs_id=book.abs_id,
                    direction="push",
                    action="date_push",
                    detail={
                        "started_at": book.started_at,
                        "finished_at": book.finished_at,
                        "created_read": True,
                        "force": force,
                    },
                )
                logger.info(f"Created HC reading session with dates for '{book.abs_id}'")
                return True, "Created reading session and synced dates to Hardcover"

            read = reads[0]
            hc_started = read.get("started_at")
            hc_finished = read.get("finished_at")

            if force:
                needs_push = (book.started_at and book.started_at != hc_started) or (
                    book.finished_at and book.finished_at != hc_finished
                )
            else:
                needs_push = (book.started_at and not hc_started) or (book.finished_at and not hc_finished)

            if not needs_push:
                return False, "Dates already match Hardcover"

            read_id = read["id"]

            if force:
                push_started = book.started_at if book.started_at and book.started_at != hc_started else None
                push_finished = book.finished_at if book.finished_at and book.finished_at != hc_finished else None
            else:
                push_started = book.started_at if book.started_at and not hc_started else None
                push_finished = book.finished_at if book.finished_at and not hc_finished else None

            success = self.hardcover_client.update_read_dates(
                read_id,
                started_at=push_started or hc_started,
                finished_at=push_finished or hc_finished,
            )
            if not success:
                logger.warning(f"Hardcover rejected date update for '{book.abs_id}'")
                return False, "Hardcover rejected the date update"

            log_hardcover_action(
                self.database_service,
                abs_id=book.abs_id,
                direction="push",
                action="date_push",
                detail={"started_at": push_started, "finished_at": push_finished, "force": force},
            )
            logger.info(f"Pushed dates to Hardcover for '{book.abs_id}' (force={force})")
            return True, "Dates synced to Hardcover"

        except Exception as e:
            logger.debug(f"Could not push dates to Hardcover for book_id={book_id}: {e}")
            return False, "Unexpected error syncing dates"

    def pull_dates_from_hardcover(self, book_id):
        """Pull started_at/finished_at from Hardcover into local DB.

        Overwrites local dates with HC dates. Only updates fields where HC has a value.
        Returns (success: bool, message: str, updates: dict).
        """
        try:
            if not self.hardcover_client.is_configured():
                return False, "Hardcover is not configured", {}

            hc_details = self.database_service.get_hardcover_details(book_id)
            if not hc_details or not hc_details.hardcover_book_id:
                return False, "Book is not linked to Hardcover", {}

            book = self.database_service.get_book_by_id(book_id)
            if not book:
                return False, "Book not found", {}

            user_book = self.hardcover_client.find_user_book(int(hc_details.hardcover_book_id))
            if not user_book:
                return False, "Book not found in your Hardcover library", {}

            reads = user_book.get("user_book_reads", [])
            if not reads:
                return False, "No reading sessions found on Hardcover", {}

            read = reads[0]
            hc_started = read.get("started_at")
            hc_finished = read.get("finished_at")

            if not hc_started and not hc_finished:
                return False, "No dates found on Hardcover", {}

            # Truncate to YYYY-MM-DD (HC may return full ISO timestamps)
            if hc_started:
                hc_started = hc_started[:10]
            if hc_finished:
                hc_finished = hc_finished[:10]

            updates = {}
            if hc_started and hc_started != book.started_at:
                updates["started_at"] = hc_started
            if hc_finished and hc_finished != book.finished_at:
                updates["finished_at"] = hc_finished

            if not updates:
                return False, "Local dates already match Hardcover", {}

            self.database_service.update_book_reading_fields(book_id, **updates)

            log_hardcover_action(
                self.database_service,
                abs_id=book.abs_id,
                direction="pull",
                action="date_pull",
                detail=updates,
            )
            logger.info(f"Pulled dates from Hardcover for '{book.abs_id}': {updates}")

            # Return the full date state after update
            updated_book = self.database_service.get_book_by_ref(book_id)
            result_dates = {
                "started_at": updated_book.started_at,
                "finished_at": updated_book.finished_at,
            }
            return True, "Dates pulled from Hardcover", result_dates

        except Exception as e:
            logger.debug(f"Could not pull dates from Hardcover for book_id={book_id}: {e}")
            return False, "Unexpected error pulling dates", {}

    def auto_complete_finished_books(self, container):
        """Detect active books at 100% progress and mark them completed.

        Uses local sync state (no external API calls for detection). Only books
        with >= 99% progress on at least one client are eligible.

        Returns dict with counts: {'completed': N, 'errors': N}.
        """
        from src.services.status_machine import StatusMachine

        books = self.database_service.get_all_books()
        stats = {"completed": 0, "errors": 0}
        machine = StatusMachine(self.database_service)

        for book in books:
            if book.status != "active":
                continue
            try:
                if self._is_finished_by_state(book.id):
                    dates = self.pull_reading_dates(book.id)
                    result = machine.transition(book, "completed", "auto_complete", container=container, dates=dates)
                    self._push_completion_to_clients(book, container)
                    if result.get("success"):
                        # Land the real finish date on Hardcover (fills gaps only)
                        self.push_dates_to_hardcover(book.id)
                    stats["completed"] += 1
                    logger.info(f"Marked '{book.title}' as completed (client progress >= 99%)")
            except Exception as e:
                stats["errors"] += 1
                logger.debug(f"Could not auto-complete '{book.title}': {e}")

        return stats

    def _max_state_progress(self, book_id):
        """Return the maximum percentage across all sync clients for a book."""
        states = self.database_service.get_states_for_book(book_id)
        percentages = [s.percentage for s in states if s.percentage is not None]
        return max(percentages) if percentages else 0.0

    def _is_finished_by_state(self, book_id):
        """Check if any sync client reports >= 99% progress for this book."""
        return self._max_state_progress(book_id) >= PROGRESS_COMPLETE_THRESHOLD

    def _push_completion_to_clients(self, book, container):
        """Push 100% progress to all sync clients and set Grimmory read status."""
        from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest

        locator = LocatorResult(percentage=1.0)
        update_req = UpdateProgressRequest(locator_result=locator, txt="Book finished", previous_location=None)

        for client_name, client in container.sync_clients().items():
            if not client.is_configured():
                continue
            try:
                if client_name.lower() == "abs":
                    client.abs_client.mark_finished(book.abs_id)
                else:
                    client.update_progress(book, update_req)

                state = State(
                    abs_id=book.abs_id,
                    book_id=book.id,
                    client_name=client_name.lower(),
                    percentage=1.0,
                    timestamp=int(time.time()),
                    last_updated=int(time.time()),
                )
                self.database_service.save_state(state)
                logger.debug(f"Pushed completion to '{client_name}' for '{book.title}'")
            except Exception as e:
                logger.warning(f"Failed to push completion to '{client_name}' for '{book.title}': {e}")

        if book.ebook_filename:
            push_grimmory_read_status(book, container, "READ")
