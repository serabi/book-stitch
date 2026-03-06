"""Books blueprint — delete, clear_progress, sync_now, mark_complete, pause, dnf, resume, update_hash."""

import logging
import threading
import time
from datetime import date

from flask import Blueprint, flash, jsonify, redirect, request, url_for

from src.blueprints.helpers import (
    cleanup_mapping_resources,
    find_in_booklore,
    get_container,
    get_database_service,
    get_kosync_id_for_ebook,
    get_manager,
)
from src.db.models import State
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

books_bp = Blueprint('books', __name__)


@books_bp.route('/delete/<abs_id>', methods=['POST'])
def delete_mapping(abs_id):
    database_service = get_database_service()
    book = database_service.get_book(abs_id)
    if book:
        cleanup_mapping_resources(book)

    database_service.delete_book(abs_id)
    return redirect(url_for('dashboard.index'))


@books_bp.route('/clear-progress/<abs_id>', methods=['POST'])
def clear_progress(abs_id):
    """Clear progress for a mapping by setting all systems to 0%"""
    manager = get_manager()
    database_service = get_database_service()
    book = database_service.get_book(abs_id)

    if not book:
        logger.warning(f"Cannot clear progress: book not found for '{abs_id}'")
        return redirect(url_for('dashboard.index'))

    title = sanitize_log_data(book.abs_title or abs_id)

    def _run():
        try:
            logger.info(f"Clearing progress for {title}")
            manager.clear_progress(abs_id)
            logger.info(f"Progress cleared successfully for {title}")
        except Exception as e:
            logger.error(f"Failed to clear progress for '{abs_id}': {e}")

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for('dashboard.index'))


@books_bp.route('/api/retry-transcription/<abs_id>', methods=['POST'])
def retry_transcription(abs_id):
    database_service = get_database_service()
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    if book.status not in ('failed_retry_later', 'failed_permanent'):
        return jsonify({"success": False, "error": "Book is not in a failed state"}), 400

    logger.info(f"Retrying transcription for '{sanitize_log_data(book.abs_title or abs_id)}'")
    database_service.delete_jobs_for_book(abs_id)
    book.status = 'pending'
    database_service.save_book(book)
    return jsonify({"success": True})


@books_bp.route('/api/sync-now/<abs_id>', methods=['POST'])
def sync_now(abs_id):
    manager = get_manager()
    database_service = get_database_service()
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    if book.status == 'completed':
        from src.services.reading_date_service import _push_completion_to_clients
        container = get_container()
        _push_completion_to_clients(book, container, database_service)
        return jsonify({"success": True, "reload": True})
    else:
        threading.Thread(target=manager.sync_cycle, kwargs={'target_abs_id': abs_id}, daemon=True).start()
        return jsonify({"success": True})


def _pull_started_at(abs_id, container, database_service):
    """Try to get the real started_at date from Hardcover or ABS, falling back to today."""
    from src.services.reading_date_service import pull_reading_dates
    dates = pull_reading_dates(abs_id, container, database_service)
    return dates.get('started_at', date.today().isoformat())


@books_bp.route('/api/mark-complete/<abs_id>', methods=['POST'])
def mark_complete(abs_id):
    container = get_container()
    database_service = get_database_service()
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    perform_delete = request.json.get('delete', False) if request.json else False

    locator = LocatorResult(percentage=1.0)
    update_req = UpdateProgressRequest(locator_result=locator, txt="Book finished", previous_location=None)

    for client_name, client in container.sync_clients().items():
        if client.is_configured():
            if client_name.lower() == 'abs':
                client.abs_client.mark_finished(abs_id)
            else:
                client.update_progress(book, update_req)

            state = State(
                abs_id=abs_id,
                client_name=client_name.lower(),
                percentage=1.0,
                timestamp=int(time.time()),
                last_updated=int(time.time())
            )
            database_service.save_state(state)

    # Record completion locally (skip if already completed — idempotent)
    if book.status != 'completed':
        today = date.today().isoformat()
        reading_updates = {'finished_at': today}
        if not book.started_at:
            reading_updates['started_at'] = _pull_started_at(abs_id, container, database_service)
        if book.finished_at:
            # Re-read: increment read_count
            reading_updates['read_count'] = (book.read_count or 1) + 1

        book.status = 'completed'
        database_service.save_book(book)
        database_service.update_book_reading_fields(abs_id, **reading_updates)
        database_service.add_reading_journal(abs_id, event='finished', percentage=1.0)

    # Push READ status to Booklore instances (auto-sets dateFinished)
    if book.ebook_filename:
        from src.services.reading_date_service import _push_booklore_read_status
        _push_booklore_read_status(book, container, 'READ')

    if perform_delete:
        cleanup_mapping_resources(book)
        database_service.delete_book(abs_id)

    return jsonify({"success": True})


@books_bp.route('/api/pause/<abs_id>', methods=['POST'])
def pause_book(abs_id):
    database_service = get_database_service()
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404
    if book.status not in ('active', 'not_started'):
        return jsonify({"success": False, "error": f"Cannot pause a book with status '{book.status}'"}), 400

    book.status = 'paused'
    database_service.save_book(book)
    database_service.add_reading_journal(abs_id, event='paused')
    logger.info(f"Book paused: '{sanitize_log_data(book.abs_title or abs_id)}'")

    # Sync Paused status to Hardcover (status_id=4)
    container = get_container()
    hardcover_client = container.hardcover_client()
    if hardcover_client.is_configured():
        hc_details = database_service.get_hardcover_details(abs_id)
        if hc_details and hc_details.hardcover_book_id:
            try:
                hardcover_client.update_status(
                    int(hc_details.hardcover_book_id), 4,
                    int(hc_details.hardcover_edition_id) if hc_details.hardcover_edition_id else None
                )
                logger.info(f"Hardcover status set to Paused for '{sanitize_log_data(book.abs_title)}'")
            except Exception as e:
                logger.warning(f"Failed to update Hardcover paused status: {e}")

    return jsonify({"success": True})


@books_bp.route('/api/dnf/<abs_id>', methods=['POST'])
def dnf_book(abs_id):
    database_service = get_database_service()
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404
    if book.status not in ('active', 'paused', 'not_started'):
        return jsonify({"success": False, "error": f"Cannot mark DNF a book with status '{book.status}'"}), 400

    book.status = 'dnf'
    database_service.save_book(book)
    database_service.add_reading_journal(abs_id, event='dnf')
    logger.info(f"Book marked DNF: '{sanitize_log_data(book.abs_title or abs_id)}'")

    # Sync DNF status to Hardcover (status_id=5)
    container = get_container()
    hardcover_client = container.hardcover_client()
    if hardcover_client.is_configured():
        hc_details = database_service.get_hardcover_details(abs_id)
        if hc_details and hc_details.hardcover_book_id:
            try:
                hardcover_client.update_status(
                    int(hc_details.hardcover_book_id), 5,
                    int(hc_details.hardcover_edition_id) if hc_details.hardcover_edition_id else None
                )
                logger.info(f"Hardcover status set to DNF for '{sanitize_log_data(book.abs_title)}'")
            except Exception as e:
                logger.warning(f"Failed to update Hardcover DNF status: {e}")

    return jsonify({"success": True})


@books_bp.route('/api/resume/<abs_id>', methods=['POST'])
def resume_book(abs_id):
    database_service = get_database_service()
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404
    if book.status not in ('paused', 'dnf', 'not_started'):
        return jsonify({"success": False, "error": f"Cannot resume a book with status '{book.status}'"}), 400

    was_inactive = book.status in ('dnf', 'paused')
    book.status = 'active'
    book.activity_flag = False
    database_service.save_book(book)
    database_service.add_reading_journal(abs_id, event='resumed')
    container = get_container()
    if not book.started_at:
        database_service.update_book_reading_fields(
            abs_id, started_at=_pull_started_at(abs_id, container, database_service)
        )
    logger.info(f"Book resumed: '{sanitize_log_data(book.abs_title or abs_id)}'")

    # If resuming from DNF or Paused, reset Hardcover to Currently Reading (status_id=2)
    if was_inactive:
        hardcover_client = container.hardcover_client()
        if hardcover_client.is_configured():
            hc_details = database_service.get_hardcover_details(abs_id)
            if hc_details and hc_details.hardcover_book_id:
                try:
                    hardcover_client.update_status(
                        int(hc_details.hardcover_book_id), 2,
                        int(hc_details.hardcover_edition_id) if hc_details.hardcover_edition_id else None
                    )
                    logger.info(f"Hardcover status reset to Currently Reading for '{sanitize_log_data(book.abs_title)}'")
                except Exception as e:
                    logger.warning(f"Failed to update Hardcover resume status: {e}")

        # Push READING status to Booklore
        if book.ebook_filename:
            from src.services.reading_date_service import _push_booklore_read_status
            _push_booklore_read_status(book, container, 'READING')

    return jsonify({"success": True})


@books_bp.route('/update-hash/<abs_id>', methods=['POST'])
def update_hash(abs_id):
    manager = get_manager()
    database_service = get_database_service()

    new_hash = request.form.get('new_hash', '').strip()
    book = database_service.get_book(abs_id)

    if not book:
        flash("Book not found", "error")
        return redirect(url_for('dashboard.index'))

    old_hash = book.kosync_doc_id

    if new_hash:
        book.kosync_doc_id = new_hash
        database_service.save_book(book)
        logger.info(f"Updated KoSync hash for '{sanitize_log_data(book.abs_title)}' to manual input: '{new_hash}'")
        updated = True
    else:
        target_filename = book.original_ebook_filename or book.ebook_filename

        booklore_id = None
        bl_book, matched_bl_client = find_in_booklore(target_filename)
        if bl_book:
            booklore_id = bl_book.get('id')

        recalc_hash = get_kosync_id_for_ebook(target_filename, booklore_id, original_filename=book.ebook_filename, bl_client=matched_bl_client)

        if recalc_hash:
            book.kosync_doc_id = recalc_hash
            database_service.save_book(book)
            logger.info(f"Auto-regenerated KoSync hash for '{sanitize_log_data(book.abs_title)}': '{recalc_hash}'")
            updated = True
        else:
            flash("Could not recalculate hash (file not found?)", "error")
            return redirect(url_for('dashboard.index'))

    if updated and book.kosync_doc_id != old_hash:
        logger.info(f"Hash changed for '{sanitize_log_data(book.abs_title)}' -- triggering instant sync to reconcile progress")
        threading.Thread(target=manager.sync_cycle, kwargs={'target_abs_id': abs_id}, daemon=True).start()

    flash(f"Updated KoSync Hash for {book.abs_title}", "success")
    return redirect(url_for('dashboard.index'))
