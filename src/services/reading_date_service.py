"""Service for pulling real reading dates (started_at, finished_at) from external sources."""

import logging
import time
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)


def pull_reading_dates(abs_id, container, database_service):
    """Pull started_at and finished_at from Hardcover or ABS for a book.

    Returns dict with 'started_at' and/or 'finished_at' keys (YYYY-MM-DD strings).
    Only includes keys where a date was found.
    """
    dates = {}

    # 1. Hardcover: check user_book_reads
    try:
        hardcover_client = container.hardcover_client()
        if hardcover_client.is_configured():
            hc_details = database_service.get_hardcover_details(abs_id)
            if hc_details and hc_details.hardcover_book_id:
                user_book = hardcover_client.find_user_book(int(hc_details.hardcover_book_id))
                if user_book:
                    reads = user_book.get("user_book_reads", [])
                    if reads:
                        read = reads[0]
                        if read.get("started_at"):
                            dates['started_at'] = read["started_at"]
                        if read.get("finished_at"):
                            dates['finished_at'] = read["finished_at"]
                    if dates:
                        logger.debug(f"Pulled dates from Hardcover for '{abs_id}': {dates}")
                        return dates
    except Exception as e:
        logger.debug(f"Could not pull dates from Hardcover for '{abs_id}': {e}")

    # 2. ABS: check mediaProgress.startedAt / finishedAt (Unix epoch ms)
    try:
        abs_client = container.abs_client()
        if abs_client.is_configured():
            progress = abs_client.get_progress(abs_id)
            if progress:
                if progress.get("startedAt"):
                    dates['started_at'] = datetime.fromtimestamp(progress["startedAt"] / 1000, tz=UTC).date().isoformat()
                if progress.get("finishedAt"):
                    dates['finished_at'] = datetime.fromtimestamp(progress["finishedAt"] / 1000, tz=UTC).date().isoformat()
                if dates:
                    logger.debug(f"Pulled dates from ABS for '{abs_id}': {dates}")
    except Exception as e:
        logger.debug(f"Could not pull dates from ABS for '{abs_id}': {e}")

    return dates


def _max_state_progress(abs_id, database_service):
    """Return the maximum percentage across all sync clients for a book."""
    states = database_service.get_states_for_book(abs_id)
    percentages = [s.percentage for s in states if s.percentage is not None]
    return max(percentages) if percentages else 0.0


def _is_finished_by_state(abs_id, database_service):
    """Check if any sync client reports >= 99% progress for this book."""
    return _max_state_progress(abs_id, database_service) >= 0.99


def _push_completion_to_clients(book, container, database_service):
    """Push 100% progress to all sync clients and set Booklore read status to READ."""
    from src.db.models import State
    from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest

    locator = LocatorResult(percentage=1.0)
    update_req = UpdateProgressRequest(locator_result=locator, txt="Book finished", previous_location=None)

    for client_name, client in container.sync_clients().items():
        if not client.is_configured():
            continue
        try:
            if client_name.lower() == 'abs':
                client.abs_client.mark_finished(book.abs_id)
            else:
                client.update_progress(book, update_req)

            state = State(
                abs_id=book.abs_id,
                client_name=client_name.lower(),
                percentage=1.0,
                timestamp=int(time.time()),
                last_updated=int(time.time())
            )
            database_service.save_state(state)
            logger.debug(f"Pushed completion to '{client_name}' for '{book.abs_title}'")
        except Exception as e:
            logger.warning(f"Failed to push completion to '{client_name}' for '{book.abs_title}': {e}")

    # Push READ status to Booklore instances (auto-sets dateFinished)
    if book.ebook_filename:
        _push_booklore_read_status(book, container, 'READ')


def _push_booklore_read_status(book, container, status):
    """Push a read status (READING, READ, etc.) to Booklore."""
    try:
        bl_client = container.booklore_client()
        if bl_client.is_configured():
            bl_client.update_read_status(book.ebook_filename, status)
    except Exception as e:
        logger.debug(f"Could not push Booklore status '{status}': {e}")


def _mark_completed(book, dates, database_service, stats, reason, container=None,
                    push_to_clients=False):
    """Mark a book as completed, filling in dates where available.

    If the book was previously completed (has finished_at), this is a re-read
    and read_count is incremented.
    """
    updates = {}
    if dates.get('finished_at'):
        updates['finished_at'] = dates['finished_at']
    elif not book.finished_at:
        updates['finished_at'] = date.today().isoformat()
    if not book.started_at and dates.get('started_at'):
        updates['started_at'] = dates['started_at']

    # Re-read detection: if already has a finished_at, increment read_count
    if book.finished_at:
        updates['read_count'] = (book.read_count or 1) + 1

    book.status = 'completed'
    database_service.save_book(book)
    database_service.add_reading_journal(book.abs_id, event='finished', percentage=1.0)
    if updates:
        database_service.update_book_reading_fields(book.abs_id, **updates)

    if push_to_clients and container:
        _push_completion_to_clients(book, container, database_service)

    stats['completed'] += 1
    logger.info(f"Marked '{book.abs_title}' as completed ({reason})")


def auto_complete_finished_books(database_service, container):
    """Detect active books at 100% progress and mark them completed.

    Uses local sync state (no external API calls for detection). Only books
    with >= 99% progress on at least one client are eligible. Also pushes
    100% to all sync clients to keep services in sync.

    Returns dict with counts: {'completed': N, 'errors': N}.
    """
    books = database_service.get_all_books()
    stats = {'completed': 0, 'errors': 0}

    for book in books:
        if book.status != 'active':
            continue
        try:
            if _is_finished_by_state(book.abs_id, database_service):
                dates = pull_reading_dates(book.abs_id, container, database_service)
                _mark_completed(book, dates, database_service, stats,
                                "client progress >= 99%",
                                container=container, push_to_clients=True)
        except Exception as e:
            stats['errors'] += 1
            logger.debug(f"Could not auto-complete '{book.abs_title}': {e}")

    return stats


def sync_reading_dates(database_service, container):
    """Sync reading dates and detect completed books.

    Pulls started_at/finished_at from Hardcover and ABS for books missing them.
    Also detects books at 100% progress that were never marked completed.

    For external finished_at dates (Path 1), a re-read guard prevents marking
    a book as completed if local progress is between 1-95% — the user is
    likely re-reading.

    Returns dict with counts: {'updated': N, 'completed': N, 'errors': N}.
    """
    books = database_service.get_all_books()
    stats = {'updated': 0, 'completed': 0, 'errors': 0}

    for book in books:
        if book.status in ('pending', 'processing', 'failed_retry_later', 'failed_permanent', 'not_started'):
            continue

        needs_started = not book.started_at and book.status in ('active', 'paused', 'completed', 'dnf')
        needs_finished = not book.finished_at and book.status == 'completed'
        should_check_completion = book.status == 'active'

        if not needs_started and not needs_finished and not should_check_completion:
            continue

        try:
            dates = pull_reading_dates(book.abs_id, container, database_service)

            # Check if an active book is actually finished
            if should_check_completion:
                # Path 1: external source says it's finished
                if dates.get('finished_at'):
                    # Re-read guard: if book was already finished AND local progress
                    # is below 99%, skip — the user is likely re-reading
                    local_pct = _max_state_progress(book.abs_id, database_service)
                    if book.finished_at and local_pct < 0.99:
                        logger.debug(
                            f"Skipping auto-complete for '{book.abs_title}': "
                            f"external finished_at='{dates['finished_at']}' but "
                            f"local progress {local_pct:.0%} suggests re-read"
                        )
                    else:
                        _mark_completed(book, dates, database_service, stats,
                                        f"finished_at='{dates['finished_at']}' from external source",
                                        container=container, push_to_clients=True)
                    continue

                # Path 2: local sync state shows 100% progress
                if _is_finished_by_state(book.abs_id, database_service):
                    _mark_completed(book, dates, database_service, stats,
                                    "client progress >= 99%",
                                    container=container, push_to_clients=True)
                    continue

            # Fill in missing dates for books that don't need completion
            # For active books, only set started_at if there's real progress (>1%).
            # ABS/Hardcover auto-set startedAt on first sync even at 0% — unreliable.
            updates = {}
            if needs_started and dates.get('started_at'):
                if book.status == 'active':
                    local_pct = _max_state_progress(book.abs_id, database_service)
                    if local_pct > 0.01:
                        updates['started_at'] = dates['started_at']
                else:
                    updates['started_at'] = dates['started_at']
            if needs_finished and dates.get('finished_at'):
                updates['finished_at'] = dates['finished_at']

            if updates:
                database_service.update_book_reading_fields(book.abs_id, **updates)
                stats['updated'] += 1
                logger.info(f"Synced reading dates for '{book.abs_title}': {updates}")
        except Exception as e:
            stats['errors'] += 1
            logger.debug(f"Could not sync dates for '{book.abs_title}': {e}")

    return stats
