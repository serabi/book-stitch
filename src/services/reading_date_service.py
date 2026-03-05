"""Service for pulling real reading dates (started_at, finished_at) from external sources."""

import logging
from datetime import date

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
                    dates['started_at'] = date.fromtimestamp(progress["startedAt"] / 1000).isoformat()
                if progress.get("finishedAt"):
                    dates['finished_at'] = date.fromtimestamp(progress["finishedAt"] / 1000).isoformat()
                if dates:
                    logger.debug(f"Pulled dates from ABS for '{abs_id}': {dates}")
    except Exception as e:
        logger.debug(f"Could not pull dates from ABS for '{abs_id}': {e}")

    return dates


def sync_reading_dates(database_service, container):
    """Sync reading dates for all books missing started_at or finished_at.

    Returns dict with counts: {'updated': N, 'completed': N, 'errors': N}.
    """
    books = database_service.get_all_books()
    stats = {'updated': 0, 'completed': 0, 'errors': 0}

    for book in books:
        if book.status in ('pending', 'processing', 'failed_retry_later'):
            continue

        needs_started = not book.started_at and book.status in ('active', 'paused', 'completed', 'dnf')
        needs_finished = not book.finished_at and book.status == 'completed'
        if not needs_started and not needs_finished:
            continue

        try:
            dates = pull_reading_dates(book.abs_id, container, database_service)
            if not dates:
                continue

            updates = {}
            if needs_started and dates.get('started_at'):
                updates['started_at'] = dates['started_at']
            if needs_finished and dates.get('finished_at'):
                updates['finished_at'] = dates['finished_at']

            # If we found a finished_at and the book is still active, mark it completed
            if book.status == 'active' and dates.get('finished_at'):
                updates['finished_at'] = dates['finished_at']
                if not book.started_at and dates.get('started_at'):
                    updates['started_at'] = dates['started_at']
                book.status = 'completed'
                database_service.save_book(book)
                database_service.add_reading_journal(book.abs_id, event='finished', percentage=1.0)
                stats['completed'] += 1
                logger.info(f"Marked '{book.abs_title}' as completed (finished_at='{dates['finished_at']}')")

            if updates:
                database_service.update_book_reading_fields(book.abs_id, **updates)
                stats['updated'] += 1
                logger.info(f"Synced reading dates for '{book.abs_title}': {updates}")
        except Exception as e:
            stats['errors'] += 1
            logger.debug(f"Could not sync dates for '{book.abs_title}': {e}")

    return stats
