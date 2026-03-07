"""Matching blueprint — suggestions, single match, batch match."""

import hashlib
import json
import logging
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

from src.blueprints.helpers import (
    audiobook_matches_search,
    find_in_booklore,
    get_abs_service,
    get_audiobooks_conditionally,
    get_container,
    get_database_service,
    get_kosync_id_for_ebook,
    get_manager,
    get_searchable_ebooks,
)
from src.db.models import Book
from src.utils.logging_utils import sanitize_log_data
from src.utils.path_utils import sanitize_filename

logger = logging.getLogger(__name__)

matching_bp = Blueprint('matching', __name__)


def _copy_book_merge_metadata(existing_book, overrides=None):
    metadata = {
        'storyteller_uuid': getattr(existing_book, 'storyteller_uuid', None),
        'original_ebook_filename': getattr(existing_book, 'original_ebook_filename', None),
        'abs_ebook_item_id': getattr(existing_book, 'abs_ebook_item_id', None),
        'custom_cover_url': getattr(existing_book, 'custom_cover_url', None),
        'started_at': getattr(existing_book, 'started_at', None),
        'finished_at': getattr(existing_book, 'finished_at', None),
        'rating': getattr(existing_book, 'rating', None),
        'read_count': getattr(existing_book, 'read_count', 1),
    }
    if overrides:
        metadata.update({key: value for key, value in overrides.items() if value is not None})
    return metadata


def _serialize_suggestion(s):
    matches = []
    for m in s.matches:
        evidence = m.get('evidence') or []
        has_bookfusion = (
            m.get('source_family') == 'bookfusion'
            or any(ev.startswith('bookfusion') for ev in evidence)
        )
        matches.append({
            **m,
            'evidence': evidence,
            'has_bookfusion': has_bookfusion,
        })

    has_bookfusion_evidence = any(m.get('has_bookfusion') for m in matches)
    return {
        'id': s.id,
        'source_id': s.source_id,
        'title': s.title,
        'author': s.author,
        'cover_url': s.cover_url,
        'matches': matches,
        'created_at': s.created_at.isoformat() if s.created_at else None,
        'has_bookfusion_evidence': has_bookfusion_evidence,
        'top_match': matches[0] if matches else None,
        'status': 'hidden' if s.status == 'dismissed' else s.status,
        'hidden': s.status in ('hidden', 'dismissed'),
    }


def _build_batch_queue_item(item):
    """Annotate queue entries with display-oriented fields without mutating session data."""
    ebook_label = item.get('ebook_display_name') or item.get('ebook_filename') or 'Not selected'
    storyteller_selected = bool(item.get('storyteller_uuid'))
    storyteller_label = 'Selected' if storyteller_selected else 'None / Skip'

    if item.get('audio_only'):
        status_label = 'Audio Only'
        status_kind = 'audio-only'
    elif item.get('abs_id') and item.get('ebook_filename'):
        status_label = 'Ready'
        status_kind = 'ready'
    else:
        status_label = 'Incomplete'
        status_kind = 'incomplete'

    return {
        **item,
        'ebook_label': ebook_label,
        'storyteller_label': storyteller_label,
        'storyteller_selected': storyteller_selected,
        'status_label': status_label,
        'status_kind': status_kind,
    }


def _build_batch_queue_view(queue):
    queue_items = [_build_batch_queue_item(item) for item in queue]
    return {
        'items': queue_items,
        'total_count': len(queue_items),
        'ready_count': sum(1 for item in queue_items if item['status_kind'] in {'ready', 'audio-only'}),
        'audio_only_count': sum(1 for item in queue_items if item['status_kind'] == 'audio-only'),
        'incomplete_count': sum(1 for item in queue_items if item['status_kind'] == 'incomplete'),
    }


@matching_bp.route('/suggestions')
def suggestions():
    """Dedicated page for browsing and acting on pairing suggestions."""
    container = get_container()
    database_service = get_database_service()
    raw_suggestions = database_service.get_all_actionable_suggestions()
    suggestions_list = [_serialize_suggestion(s) for s in raw_suggestions if s.matches]
    visible_count = sum(1 for s in suggestions_list if not s.get('hidden'))
    hidden_count = sum(1 for s in suggestions_list if s.get('hidden'))
    suggestions_enabled = current_app.config.get('SUGGESTIONS_ENABLED', False)
    bookfusion_enabled = container.bookfusion_client().is_configured()
    bookfusion_catalog_count = len(database_service.get_bookfusion_books()) if bookfusion_enabled else 0
    initial_search = request.args.get('search', '').strip()
    selected_source_id = request.args.get('source_id', '').strip()
    return render_template(
        'suggestions.html',
        suggestions=suggestions_list,
        visible_count=visible_count,
        hidden_count=hidden_count,
        suggestions_enabled=suggestions_enabled,
        bookfusion_enabled=bookfusion_enabled,
        bookfusion_catalog_count=bookfusion_catalog_count,
        suggestions_json=json.dumps(suggestions_list),
        initial_search=initial_search,
        selected_source_id=selected_source_id,
    )


@matching_bp.route('/match', methods=['GET', 'POST'])
def match():
    container = get_container()
    manager = get_manager()
    database_service = get_database_service()

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
            abs_service.add_to_collection(abs_id, current_app.config['ABS_COLLECTION_NAME'])
            hardcover_sync_client = container.sync_clients().get('Hardcover')
            if hardcover_sync_client and hardcover_sync_client.is_configured():
                hardcover_sync_client.automatch_hardcover(book)
            database_service.resolve_suggestion(abs_id)
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
                database_service.resolve_suggestion(kosync_doc_id)
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
            database_service.resolve_suggestion(kosync_doc_id)
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
                **_copy_book_merge_metadata(book, {
                    'storyteller_uuid': book.storyteller_uuid,
                    'original_ebook_filename': book.original_ebook_filename,
                }),
            )
            database_service.save_book(new_book)
            try:
                database_service.migrate_book_data(link_book_id, abs_id)
                database_service.delete_book(link_book_id)
                abs_service.add_to_collection(abs_id, current_app.config['ABS_COLLECTION_NAME'])
                logger.info(f"Successfully merged {link_book_id} into {abs_id}")
            except Exception as e:
                logger.error(f"Failed to merge book data: {e}")
                raise
            hardcover_sync_client = container.sync_clients().get('Hardcover')
            if hardcover_sync_client and hardcover_sync_client.is_configured():
                hardcover_sync_client.automatch_hardcover(new_book)
            database_service.resolve_suggestion(abs_id)
            if new_book.kosync_doc_id:
                database_service.resolve_suggestion(new_book.kosync_doc_id)
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
            merge_metadata = _copy_book_merge_metadata(existing_book, {
                'abs_ebook_item_id': abs_ebook_item_id,
                'original_ebook_filename': original_ebook_filename,
                'storyteller_uuid': storyteller_uuid or existing_book.storyteller_uuid,
            })
        else:
            abs_ebook_item_id = None
            merge_metadata = {
                'storyteller_uuid': storyteller_uuid,
                'original_ebook_filename': original_ebook_filename,
                'abs_ebook_item_id': abs_ebook_item_id,
            }

        book = Book(
            abs_id=abs_id,
            abs_title=manager.get_abs_title(selected_ab),
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=None,
            status="pending",
            duration=manager.get_duration(selected_ab),
            **merge_metadata,
        )

        database_service.save_book(book)

        # Duplicate Merge: Migrate
        if migration_source_id:
            try:
                database_service.migrate_book_data(migration_source_id, abs_id)
                database_service.delete_book(migration_source_id)
                abs_service.add_to_collection(abs_id, current_app.config['ABS_COLLECTION_NAME'])
                logger.info(f"Successfully merged {migration_source_id} into {abs_id}")
            except Exception as e:
                logger.error(f"Failed to merge book data: {e}")
                raise

        # Trigger Hardcover Automatch
        hardcover_sync_client = container.sync_clients().get('Hardcover')
        if hardcover_sync_client and hardcover_sync_client.is_configured():
            hardcover_sync_client.automatch_hardcover(book)

        if not migration_source_id:
            abs_service.add_to_collection(abs_id, current_app.config['ABS_COLLECTION_NAME'])
        if bl_match_client:
            shelf_filename = original_ebook_filename or ebook_filename
            try:
                bl_match_client.add_to_shelf(shelf_filename)
            except Exception as e:
                logger.warning(f"Booklore add_to_shelf failed for '{sanitize_log_data(shelf_filename)}': {e}")
        # Remove resolved suggestions once the mapping is created
        database_service.resolve_suggestion(abs_id)
        database_service.resolve_suggestion(kosync_doc_id)

        try:
            device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                logger.info(f"Resolving additional suggestion/hash for '{ebook_filename}': '{device_doc.document_hash}'")
                database_service.resolve_suggestion(device_doc.document_hash)
        except Exception as e:
            logger.warning(f"Failed to check/resolve device hash: {e}")

        return redirect(url_for('dashboard.index'))

    # GET request
    search = request.args.get('search', '').strip().lower()
    attach_to = request.args.get('attach_to', '').strip()
    link_to = request.args.get('link_to', '').strip()
    preselect_abs_id = request.args.get('abs_id', '').strip()
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

    # If abs_id provided (e.g. from suggestions) but not in search results, fetch it directly
    preselected_audiobook = None
    if preselect_abs_id and not attach_to:
        already_listed = any(ab['id'] == preselect_abs_id for ab in audiobooks)
        if not already_listed:
            all_audiobooks = get_audiobooks_conditionally()
            preselected_audiobook = next((ab for ab in all_audiobooks if ab['id'] == preselect_abs_id), None)
            if preselected_audiobook:
                preselected_audiobook['cover_url'] = abs_service.get_cover_proxy_url(preselect_abs_id)
                audiobooks.insert(0, preselected_audiobook)

    return render_template('match.html', audiobooks=audiobooks, ebooks=ebooks,
                           storyteller_books=storyteller_books, search=search,
                           get_title=manager.get_abs_title,
                           attach_to=attach_to, attach_title=attach_title,
                           link_to=link_to, link_title=link_title,
                           preselect_abs_id=preselect_abs_id)


@matching_bp.route('/batch-match', methods=['GET', 'POST'])
def batch_match():
    container = get_container()
    manager = get_manager()
    database_service = get_database_service()

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
            return redirect(url_for('matching.batch_match', search=request.form.get('search', '')))
        elif action == 'remove_from_queue':
            abs_id = request.form.get('abs_id')
            session['queue'] = [item for item in session.get('queue', []) if item['abs_id'] != abs_id]
            session.modified = True
            return redirect(url_for('matching.batch_match'))
        elif action == 'clear_queue':
            session['queue'] = []
            session.modified = True
            return redirect(url_for('matching.batch_match'))
        elif action == 'process_queue':
            failed_items = []
            for item in session.get('queue', []):
                item_label = item.get('ebook_display_name') or item.get('ebook_filename') or item.get('abs_id')
                try:
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
                        abs_service.add_to_collection(item['abs_id'], current_app.config['ABS_COLLECTION_NAME'])
                        hardcover_sync_client = container.sync_clients().get('Hardcover')
                        if hardcover_sync_client and hardcover_sync_client.is_configured():
                            hardcover_sync_client.automatch_hardcover(book)
                        database_service.resolve_suggestion(item['abs_id'])
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
                        failed_items.append(item.get('ebook_display_name') or ebook_filename)
                        continue

                    # Hash Preservation
                    current_book_entry = database_service.get_book(item['abs_id'])
                    if current_book_entry and current_book_entry.kosync_doc_id:
                        logger.info(f"Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{item['abs_id']}' instead of new hash '{kosync_doc_id}'")
                        kosync_doc_id = current_book_entry.kosync_doc_id

                    # Duplicate Merge
                    existing_book = database_service.get_book_by_kosync_id(kosync_doc_id)
                    migration_source_id = None
                    abs_ebook_item_id = None

                    if existing_book and existing_book.abs_id != item['abs_id']:
                        logger.info(f"Found existing book entry '{existing_book.abs_id}' for this ebook -- Merging into '{item['abs_id']}'")
                        migration_source_id = existing_book.abs_id
                        abs_ebook_item_id = existing_book.abs_ebook_item_id or existing_book.abs_id
                        if not original_ebook_filename:
                            original_ebook_filename = existing_book.original_ebook_filename or existing_book.ebook_filename
                        merge_metadata = _copy_book_merge_metadata(existing_book, {
                            'abs_ebook_item_id': abs_ebook_item_id,
                            'original_ebook_filename': original_ebook_filename,
                            'storyteller_uuid': storyteller_uuid or existing_book.storyteller_uuid,
                        })
                    else:
                        merge_metadata = {
                            'storyteller_uuid': storyteller_uuid or None,
                            'original_ebook_filename': original_ebook_filename,
                            'abs_ebook_item_id': abs_ebook_item_id,
                        }

                    book = Book(
                        abs_id=item['abs_id'],
                        abs_title=item['abs_title'],
                        ebook_filename=ebook_filename,
                        kosync_doc_id=kosync_doc_id,
                        transcript_file=None,
                        status="pending",
                        duration=duration,
                        **merge_metadata,
                    )

                    database_service.save_book(book)

                    # Duplicate Merge: Migrate
                    if migration_source_id:
                        database_service.migrate_book_data(migration_source_id, item['abs_id'])
                        database_service.delete_book(migration_source_id)
                        abs_service.add_to_collection(item['abs_id'], current_app.config['ABS_COLLECTION_NAME'])
                        logger.info(f"Successfully merged {migration_source_id} into {item['abs_id']}")

                    # Trigger Hardcover Automatch
                    hardcover_sync_client = container.sync_clients().get('Hardcover')
                    if hardcover_sync_client and hardcover_sync_client.is_configured():
                        hardcover_sync_client.automatch_hardcover(book)

                    if not migration_source_id:
                        abs_service.add_to_collection(item['abs_id'], current_app.config['ABS_COLLECTION_NAME'])
                    if bl_match_client:
                        shelf_filename = original_ebook_filename or ebook_filename
                        try:
                            bl_match_client.add_to_shelf(shelf_filename)
                        except Exception as e:
                            logger.warning(f"Booklore add_to_shelf failed for '{sanitize_log_data(shelf_filename)}': {e}")
                    database_service.resolve_suggestion(item['abs_id'])
                    database_service.resolve_suggestion(kosync_doc_id)

                    try:
                        device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
                        if device_doc and device_doc.document_hash != kosync_doc_id:
                            database_service.resolve_suggestion(device_doc.document_hash)
                    except Exception as e:
                        logger.warning(f"Failed to check/resolve device hash: {e}")

                except Exception as e:
                    logger.error(f"Failed to process queue item '{sanitize_log_data(item_label)}': {e}")
                    failed_items.append(item_label)

            if failed_items:
                names = ', '.join(failed_items)
                flash(f"Could not compute KOSync ID for: {names}", 'warning')
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

    queue_view = _build_batch_queue_view(session.get('queue', []))
    return render_template('batch_match.html', audiobooks=audiobooks, ebooks=ebooks, storyteller_books=storyteller_books,
                           queue=queue_view['items'], queue_summary=queue_view, search=search, get_title=manager.get_abs_title)
