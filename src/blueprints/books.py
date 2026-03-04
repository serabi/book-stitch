"""Books blueprint — match, batch_match, delete, clear_progress, sync_now, mark_complete, update_hash."""

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from src.blueprints.helpers import (
    audiobook_matches_search,
    cleanup_mapping_resources,
    find_in_booklore,
    get_abs_service,
    get_audiobooks_conditionally,
    get_container,
    get_database_service,
    get_kosync_id_for_ebook,
    get_manager,
    get_searchable_ebooks,
)
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.utils.logging_utils import sanitize_log_data
from src.utils.path_utils import sanitize_filename

logger = logging.getLogger(__name__)

books_bp = Blueprint('books', __name__)


@books_bp.route('/match', methods=['GET', 'POST'])
def match():
    container = get_container()
    manager = get_manager()
    database_service = get_database_service()

    ABS_COLLECTION_NAME = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")

    if request.method == 'POST':
        action = request.form.get('action', '')

        # --- Audio-only import (no ebook required) ---
        if action == 'audio_only':
            abs_service = get_abs_service()
            if not abs_service.is_available():
                return "ABS is not configured", 400
            abs_id = request.form.get('audiobook_id')
            audiobooks = abs_service.get_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
            if not selected_ab:
                return "Audiobook not found", 404
            book = Book(
                abs_id=abs_id,
                abs_title=manager.get_abs_title(selected_ab),
                ebook_filename=None,
                kosync_doc_id=None,
                status='active',
                duration=manager.get_duration(selected_ab),
                sync_mode='audiobook',
            )
            database_service.save_book(book)
            abs_service.add_to_collection(abs_id, ABS_COLLECTION_NAME)
            hardcover_sync_client = container.sync_clients().get('Hardcover')
            if hardcover_sync_client and hardcover_sync_client.is_configured():
                hardcover_sync_client._automatch_hardcover(book)
            database_service.dismiss_suggestion(abs_id)
            return redirect(url_for('dashboard.index'))

        # --- Ebook-only import (no audiobook required) ---
        if action == 'ebook_only':
            ebook_filename = sanitize_filename(request.form.get('ebook_filename'))
            ebook_display_name = request.form.get('ebook_display_name', '')
            storyteller_uuid = request.form.get('storyteller_uuid') or None
            storyteller_title = request.form.get('storyteller_title', '')

            if not ebook_filename and not storyteller_uuid:
                return "An ebook or Storyteller selection is required", 400

            if ebook_filename:
                # Ebook present (possibly with Storyteller too)
                booklore_id = None
                matched_bl_client = None
                bl_book, matched_bl_client = find_in_booklore(ebook_filename)
                if bl_book:
                    booklore_id = bl_book.get('id')
                kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id, bl_client=matched_bl_client)
                if not kosync_doc_id:
                    return "Could not compute KOSync ID for ebook", 404
                book_id = f"ebook-{kosync_doc_id[:16]}"
                title = ebook_display_name or (bl_book.get('title') if bl_book else None) or Path(ebook_filename).stem
            else:
                # Storyteller-only (no ebook file)
                book_id = f"ebook-{hashlib.md5(storyteller_uuid.encode()).hexdigest()[:16]}"
                title = storyteller_title or ebook_display_name or 'Storyteller Book'
                ebook_filename = None
                kosync_doc_id = None

            book = Book(
                abs_id=book_id,
                abs_title=title,
                ebook_filename=ebook_filename,
                kosync_doc_id=kosync_doc_id,
                status='active',
                sync_mode='ebook_only',
                storyteller_uuid=storyteller_uuid,
            )
            database_service.save_book(book)
            if kosync_doc_id:
                database_service.dismiss_suggestion(kosync_doc_id)
            return redirect(url_for('dashboard.index'))

        # --- Attach ebook to audio-only book ---
        if action == 'attach_ebook':
            attach_abs_id = request.form.get('attach_abs_id')
            ebook_filename = sanitize_filename(request.form.get('ebook_filename'))
            if not attach_abs_id or not ebook_filename:
                return "Missing book ID or ebook filename", 400
            book = database_service.get_book(attach_abs_id)
            if not book:
                return "Book not found", 404
            booklore_id = None
            bl_book, bl_client = find_in_booklore(ebook_filename)
            if bl_book:
                booklore_id = bl_book.get('id')
            kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id, bl_client=bl_client)
            if not kosync_doc_id:
                return "Could not compute KOSync ID for ebook", 404
            book.ebook_filename = ebook_filename
            book.kosync_doc_id = kosync_doc_id
            book.status = 'pending'
            database_service.save_book(book)
            if bl_client:
                try:
                    bl_client.add_to_shelf(ebook_filename)
                except Exception as e:
                    logger.warning(f"Booklore add_to_shelf failed for '{sanitize_log_data(ebook_filename)}': {e}")
            database_service.dismiss_suggestion(kosync_doc_id)
            return redirect(url_for('dashboard.index'))

        # --- Attach audiobook to ebook-only book ---
        if action == 'attach_audiobook':
            abs_service = get_abs_service()
            if not abs_service.is_available():
                return "ABS is not configured", 400
            link_book_id = request.form.get('link_book_id')
            abs_id = request.form.get('audiobook_id')
            if not link_book_id or not abs_id:
                return "Missing book ID or audiobook ID", 400
            book = database_service.get_book(link_book_id)
            if not book:
                return "Book not found", 404
            audiobooks = abs_service.get_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
            if not selected_ab:
                return "Audiobook not found", 404
            new_book = Book(
                abs_id=abs_id,
                abs_title=manager.get_abs_title(selected_ab),
                ebook_filename=book.ebook_filename,
                kosync_doc_id=book.kosync_doc_id,
                status='active',
                duration=manager.get_duration(selected_ab),
                sync_mode='audiobook',
                storyteller_uuid=book.storyteller_uuid,
                original_ebook_filename=book.original_ebook_filename,
            )
            database_service.save_book(new_book)
            database_service.migrate_book_data(link_book_id, abs_id)
            database_service.delete_book(link_book_id)
            abs_service.add_to_collection(abs_id, ABS_COLLECTION_NAME)
            hardcover_sync_client = container.sync_clients().get('Hardcover')
            if hardcover_sync_client and hardcover_sync_client.is_configured():
                hardcover_sync_client._automatch_hardcover(new_book)
            return redirect(url_for('dashboard.index'))

        # --- Standard flow (requires audiobook) ---
        abs_service = get_abs_service()
        abs_id = request.form.get('audiobook_id')
        selected_filename = sanitize_filename(request.form.get('ebook_filename'))
        ebook_filename = selected_filename
        original_ebook_filename = None
        audiobooks = abs_service.get_audiobooks()
        selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
        if not selected_ab:
            return "Audiobook not found", 404

        booklore_id = None
        storyteller_uuid = request.form.get('storyteller_uuid')

        bl_match, bl_match_client = find_in_booklore(ebook_filename)
        if bl_match:
            booklore_id = bl_match.get('id')

        kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id, bl_client=bl_match_client)

        if not kosync_doc_id:
            logger.warning(f"Cannot compute KOSync ID for '{sanitize_log_data(ebook_filename)}': File not found in Booklore or filesystem")
            return "Could not compute KOSync ID for ebook", 404

        # Hash Preservation
        current_book_entry = database_service.get_book(abs_id)
        if current_book_entry and current_book_entry.kosync_doc_id:
            logger.info(f"Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{abs_id}' instead of new hash '{kosync_doc_id}'")
            kosync_doc_id = current_book_entry.kosync_doc_id

        # Duplicate Merge
        existing_book = database_service.get_book_by_kosync_id(kosync_doc_id)
        migration_source_id = None

        if existing_book and existing_book.abs_id != abs_id:
            logger.info(f"Found existing book entry '{existing_book.abs_id}' for this ebook -- Merging into '{abs_id}'")
            migration_source_id = existing_book.abs_id
            abs_ebook_item_id = existing_book.abs_ebook_item_id or existing_book.abs_id

            if not original_ebook_filename:
                original_ebook_filename = existing_book.original_ebook_filename or existing_book.ebook_filename
        else:
            abs_ebook_item_id = None

        book = Book(
            abs_id=abs_id,
            abs_title=manager.get_abs_title(selected_ab),
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=None,
            status="pending",
            duration=manager.get_duration(selected_ab),
            storyteller_uuid=storyteller_uuid,
            original_ebook_filename=original_ebook_filename,
            abs_ebook_item_id=abs_ebook_item_id
        )

        database_service.save_book(book)

        # Duplicate Merge: Migrate
        if migration_source_id:
            try:
                database_service.migrate_book_data(migration_source_id, abs_id)
                database_service.delete_book(migration_source_id)
                logger.info(f"Successfully merged {migration_source_id} into {abs_id}")
            except Exception as e:
                logger.error(f"Failed to merge book data: {e}")

        # Trigger Hardcover Automatch
        hardcover_sync_client = container.sync_clients().get('Hardcover')
        if hardcover_sync_client and hardcover_sync_client.is_configured():
            hardcover_sync_client._automatch_hardcover(book)

        abs_service.add_to_collection(abs_id, ABS_COLLECTION_NAME)
        if bl_match_client:
            shelf_filename = original_ebook_filename or ebook_filename
            try:
                bl_match_client.add_to_shelf(shelf_filename)
            except Exception as e:
                logger.warning(f"Booklore add_to_shelf failed for '{sanitize_log_data(shelf_filename)}': {e}")
        # Auto-dismiss pending suggestions
        database_service.dismiss_suggestion(abs_id)
        database_service.dismiss_suggestion(kosync_doc_id)

        try:
            device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                logger.info(f"Dismissing additional suggestion/hash for '{ebook_filename}': '{device_doc.document_hash}'")
                database_service.dismiss_suggestion(device_doc.document_hash)
        except Exception as e:
            logger.warning(f"Failed to check/dismiss device hash: {e}")

        return redirect(url_for('dashboard.index'))

    # GET request
    search = request.args.get('search', '').strip().lower()
    attach_to = request.args.get('attach_to', '').strip()
    link_to = request.args.get('link_to', '').strip()
    attach_title = ''
    link_title = ''

    if attach_to:
        attach_book = database_service.get_book(attach_to)
        if attach_book:
            attach_title = attach_book.abs_title or attach_to

    if link_to:
        link_book = database_service.get_book(link_to)
        if link_book:
            link_title = link_book.abs_title or link_to

    abs_service = get_abs_service()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        if not attach_to:
            audiobooks = get_audiobooks_conditionally()
            audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
            for ab in audiobooks:
                ab['cover_url'] = abs_service.get_cover_proxy_url(ab['id'])

        if not link_to:
            ebooks = get_searchable_ebooks(search)

            if container.storyteller_client().is_configured():
                try:
                    storyteller_books = container.storyteller_client().search_books(search)
                except Exception as e:
                    logger.warning(f"Storyteller search failed in match route: {e}")

    return render_template('match.html', audiobooks=audiobooks, ebooks=ebooks,
                           storyteller_books=storyteller_books, search=search,
                           get_title=manager.get_abs_title,
                           attach_to=attach_to, attach_title=attach_title,
                           link_to=link_to, link_title=link_title)


@books_bp.route('/batch-match', methods=['GET', 'POST'])
def batch_match():
    container = get_container()
    manager = get_manager()
    database_service = get_database_service()

    ABS_COLLECTION_NAME = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")

    abs_service = get_abs_service()

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_to_queue':
            session.setdefault('queue', [])
            abs_id = request.form.get('audiobook_id')
            ebook_filename = sanitize_filename(request.form.get('ebook_filename', '')) or ''
            ebook_display_name = request.form.get('ebook_display_name', ebook_filename)
            storyteller_uuid = request.form.get('storyteller_uuid', '')
            audiobooks = abs_service.get_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
            if selected_ab:
                if not any(item['abs_id'] == abs_id for item in session['queue']):
                    is_audio_only = not ebook_filename and not storyteller_uuid
                    session['queue'].append({
                        "abs_id": abs_id,
                        "abs_title": manager.get_abs_title(selected_ab),
                        "ebook_filename": ebook_filename,
                        "ebook_display_name": ebook_display_name,
                        "storyteller_uuid": storyteller_uuid,
                        "duration": manager.get_duration(selected_ab),
                        "cover_url": abs_service.get_cover_proxy_url(abs_id),
                        "audio_only": is_audio_only,
                    })
                    session.modified = True
            return redirect(url_for('books.batch_match', search=request.form.get('search', '')))
        elif action == 'remove_from_queue':
            abs_id = request.form.get('abs_id')
            session['queue'] = [item for item in session.get('queue', []) if item['abs_id'] != abs_id]
            session.modified = True
            return redirect(url_for('books.batch_match'))
        elif action == 'clear_queue':
            session['queue'] = []
            session.modified = True
            return redirect(url_for('books.batch_match'))
        elif action == 'process_queue':
            for item in session.get('queue', []):
                # Handle audio-only queue items
                if item.get('audio_only'):
                    book = Book(
                        abs_id=item['abs_id'],
                        abs_title=item['abs_title'],
                        ebook_filename=None,
                        kosync_doc_id=None,
                        status='active',
                        duration=item['duration'],
                        sync_mode='audiobook',
                    )
                    database_service.save_book(book)
                    abs_service.add_to_collection(item['abs_id'], ABS_COLLECTION_NAME)
                    hardcover_sync_client = container.sync_clients().get('Hardcover')
                    if hardcover_sync_client and hardcover_sync_client.is_configured():
                        hardcover_sync_client._automatch_hardcover(book)
                    database_service.dismiss_suggestion(item['abs_id'])
                    continue

                ebook_filename = item['ebook_filename']
                storyteller_uuid = item.get('storyteller_uuid', '')
                original_ebook_filename = None
                duration = item['duration']
                booklore_id = None
                kosync_doc_id = None

                bl_match, bl_match_client = find_in_booklore(ebook_filename)
                if bl_match:
                    booklore_id = bl_match.get('id')

                kosync_doc_id = get_kosync_id_for_ebook(ebook_filename, booklore_id, bl_client=bl_match_client)

                if not kosync_doc_id:
                    logger.warning(f"Could not compute KOSync ID for {sanitize_log_data(ebook_filename)}, skipping")
                    continue

                # Hash Preservation
                current_book_entry = database_service.get_book(item['abs_id'])
                if current_book_entry and current_book_entry.kosync_doc_id:
                    logger.info(f"Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{item['abs_id']}' instead of new hash '{kosync_doc_id}'")
                    kosync_doc_id = current_book_entry.kosync_doc_id

                book = Book(
                    abs_id=item['abs_id'],
                    abs_title=item['abs_title'],
                    ebook_filename=ebook_filename,
                    kosync_doc_id=kosync_doc_id,
                    transcript_file=None,
                    status="pending",
                    duration=duration,
                    storyteller_uuid=storyteller_uuid or None,
                    original_ebook_filename=original_ebook_filename
                )

                database_service.save_book(book)

                # Trigger Hardcover Automatch
                hardcover_sync_client = container.sync_clients().get('Hardcover')
                if hardcover_sync_client and hardcover_sync_client.is_configured():
                    hardcover_sync_client._automatch_hardcover(book)

                abs_service.add_to_collection(item['abs_id'], ABS_COLLECTION_NAME)
                if bl_match_client:
                    shelf_filename = original_ebook_filename or ebook_filename
                    try:
                        bl_match_client.add_to_shelf(shelf_filename)
                    except Exception as e:
                        logger.warning(f"Booklore add_to_shelf failed for '{sanitize_log_data(shelf_filename)}': {e}")
                database_service.dismiss_suggestion(item['abs_id'])
                database_service.dismiss_suggestion(kosync_doc_id)

                try:
                    device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
                    if device_doc and device_doc.document_hash != kosync_doc_id:
                        database_service.dismiss_suggestion(device_doc.document_hash)
                except Exception as e:
                    logger.debug(f"Failed to check/dismiss device hash during batch processing: {e}")

            session['queue'] = []
            session.modified = True
            return redirect(url_for('dashboard.index'))

    # GET request
    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        audiobooks = get_audiobooks_conditionally()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks:
            ab['cover_url'] = abs_service.get_cover_proxy_url(ab['id'])

        ebooks = get_searchable_ebooks(search)
        ebooks.sort(key=lambda x: x.name.lower())

        if container.storyteller_client().is_configured():
            try:
                storyteller_books = container.storyteller_client().search_books(search)
            except Exception as e:
                logger.warning(f"Storyteller search failed in batch_match route: {e}")

    return render_template('batch_match.html', audiobooks=audiobooks, ebooks=ebooks, storyteller_books=storyteller_books,
                           queue=session.get('queue', []), search=search, get_title=manager.get_abs_title)


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

    try:
        logger.info(f"Clearing progress for {sanitize_log_data(book.abs_title or abs_id)}")
        manager.clear_progress(abs_id)
        logger.info(f"Progress cleared successfully for {sanitize_log_data(book.abs_title or abs_id)}")
    except Exception as e:
        logger.error(f"Failed to clear progress for '{abs_id}': {e}")

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

    threading.Thread(target=manager.sync_cycle, kwargs={'target_abs_id': abs_id}, daemon=True).start()
    return jsonify({"success": True})


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
    if book.status != 'active':
        return jsonify({"success": False, "error": f"Cannot pause a book with status '{book.status}'"}), 400

    book.status = 'paused'
    database_service.save_book(book)
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
    if book.status not in ('active', 'paused'):
        return jsonify({"success": False, "error": f"Cannot mark DNF a book with status '{book.status}'"}), 400

    book.status = 'dnf'
    database_service.save_book(book)
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
    if book.status not in ('paused', 'dnf'):
        return jsonify({"success": False, "error": f"Cannot resume a book with status '{book.status}'"}), 400

    was_inactive = book.status in ('dnf', 'paused')
    book.status = 'active'
    book.activity_flag = False
    database_service.save_book(book)
    logger.info(f"Book resumed: '{sanitize_log_data(book.abs_title or abs_id)}'")

    # If resuming from DNF or Paused, reset Hardcover to Currently Reading (status_id=2)
    if was_inactive:
        container = get_container()
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
