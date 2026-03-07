"""API blueprint — /api/status, /api/suggestions/*, /api/storyteller/*, /api/booklore/*.

ABS-specific routes (/api/abs/*, /api/cover-proxy/*) are in abs_bp.py.
"""

import json
import logging

from flask import Blueprint, current_app, jsonify, request

from src.blueprints.helpers import (
    find_in_booklore,
    get_booklore_client,
    get_container,
    get_database_service,
    get_kosync_id_for_ebook,
)
from src.db.models import Book

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__)


def _serialize_suggestion(s):
    try:
        matches = json.loads(s.matches_json) if s.matches_json else []
    except Exception as e:
        logger.debug(f"Failed to parse matches_json for suggestion '{s.source_id}': {e}")
        matches = []

    for m in matches:
        evidence = m.get('evidence') or []
        m['has_bookfusion'] = (
            m.get('source_family') == 'bookfusion'
            or any(ev.startswith('bookfusion') for ev in evidence)
        )

    return {
        "id": s.id,
        "source_id": s.source_id,
        "title": s.title,
        "author": s.author,
        "cover_url": s.cover_url,
        "matches": matches,
        "has_bookfusion_evidence": any(m.get('has_bookfusion') for m in matches),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "status": 'hidden' if s.status == 'dismissed' else s.status,
        "hidden": s.status in ('hidden', 'dismissed'),
    }


# ---------------- Status ----------------

@api_bp.route('/api/status')
def api_status():
    """Return status of all books from database service"""
    database_service = get_database_service()
    books = database_service.get_all_books()

    # Bulk-fetch all states to avoid N+1 queries (one per book)
    all_states = database_service.get_all_states()
    states_by_book = {}
    for state in all_states:
        states_by_book.setdefault(state.abs_id, []).append(state)

    mappings = []
    for book in books:
        state_by_client = {state.client_name: state for state in states_by_book.get(book.abs_id, [])}

        mapping = {
            'abs_id': book.abs_id,
            'abs_title': book.abs_title,
            'ebook_filename': book.ebook_filename,
            'kosync_doc_id': book.kosync_doc_id,
            'transcript_file': book.transcript_file,
            'status': book.status,
            'sync_mode': getattr(book, 'sync_mode', 'audiobook'),
            'duration': book.duration,
            'storyteller_uuid': book.storyteller_uuid,
            'states': {}
        }

        for client_name, state in state_by_client.items():
            pct_val = round(state.percentage * 100, 1) if state.percentage is not None else 0

            mapping['states'][client_name] = {
                'timestamp': state.timestamp or 0,
                'percentage': pct_val,
                'xpath': getattr(state, 'xpath', None),
                'last_updated': state.last_updated
            }

            if client_name == 'kosync':
                mapping['kosync_pct'] = pct_val
                mapping['kosync_xpath'] = getattr(state, 'xpath', None)
            elif client_name == 'abs':
                mapping['abs_pct'] = pct_val
                mapping['abs_ts'] = state.timestamp
            elif client_name == 'storyteller':
                mapping['storyteller_pct'] = pct_val
                mapping['storyteller_xpath'] = getattr(state, 'xpath', None)
            elif client_name == 'booklore':
                mapping['booklore_pct'] = pct_val
                mapping['booklore_xpath'] = getattr(state, 'xpath', None)

        mappings.append(mapping)

    return jsonify({"mappings": mappings})


# ---------------- Processing Status ----------------

@api_bp.route('/api/processing-status')
def api_processing_status():
    """Return status and progress for all non-active (processing/pending/failed) books."""
    database_service = get_database_service()
    books = database_service.get_all_books()
    result = {}
    for book in books:
        if book.status not in ('pending', 'processing', 'failed_retry_later'):
            continue
        job = database_service.get_latest_job(book.abs_id)
        result[book.abs_id] = {
            'status': book.status,
            'job_progress': round((job.progress or 0.0) * 100, 1) if job else 0.0,
            'retry_count': (job.retry_count or 0) if job else 0,
        }
    return jsonify(result)


# ---------------- Suggestions ----------------

@api_bp.route('/api/suggestions', methods=['GET'])
def get_suggestions():
    database_service = get_database_service()
    suggestions = database_service.get_all_actionable_suggestions()
    return jsonify([_serialize_suggestion(s) for s in suggestions if s.matches])


@api_bp.route('/api/suggestions/rescan', methods=['POST'])
def rescan_suggestions():
    container = get_container()
    data = request.get_json(silent=True) or {}
    force = bool(data.get('force'))
    stats = container.suggestion_service().request_rescan_library_suggestions(force=force)
    return jsonify({"success": True, **stats})


@api_bp.route('/api/suggestions/rescan-status', methods=['GET'])
def rescan_suggestions_status():
    container = get_container()
    status = container.suggestion_service().get_rescan_status()
    return jsonify({"success": True, **status})


@api_bp.route('/api/suggestions/<source_id>/hide', methods=['POST'])
def hide_suggestion(source_id):
    database_service = get_database_service()
    if database_service.hide_suggestion(source_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


@api_bp.route('/api/suggestions/<source_id>/unhide', methods=['POST'])
def unhide_suggestion(source_id):
    database_service = get_database_service()
    if database_service.unhide_suggestion(source_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


@api_bp.route('/api/suggestions/<source_id>/ignore', methods=['POST'])
def ignore_suggestion(source_id):
    database_service = get_database_service()
    if database_service.ignore_suggestion(source_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


@api_bp.route('/api/suggestions/clear_stale', methods=['POST'])
def clear_stale_suggestions():
    database_service = get_database_service()
    count = database_service.clear_stale_suggestions()
    logger.info(f"Cleared {count} stale suggestions from database")
    return jsonify({"success": True, "count": count})


@api_bp.route('/api/suggestions/<source_id>/link-bookfusion', methods=['POST'])
def link_suggestion_bookfusion(source_id):
    database_service = get_database_service()
    container = get_container()
    suggestion = database_service.get_pending_suggestion(source_id)
    if not suggestion:
        return jsonify({"error": "Suggestion not found"}), 404

    data = request.get_json(silent=True) or {}
    match_index = data.get('match_index')
    matches = suggestion.matches or []
    if match_index is None or not isinstance(match_index, int) or match_index < 0 or match_index >= len(matches):
        return jsonify({"error": "Valid match_index required"}), 400

    match = matches[match_index]
    bookfusion_ids = match.get('bookfusion_ids') or []
    if match.get('source_family') != 'bookfusion' or not bookfusion_ids:
        return jsonify({"error": "Selected match is not a BookFusion candidate"}), 400

    abs_book = database_service.get_book(source_id)
    if not abs_book:
        abs_client = container.abs_client()
        item = abs_client.get_item_details(source_id) if abs_client else None
        metadata = (item or {}).get('media', {}).get('metadata', {})
        abs_book = Book(
            abs_id=source_id,
            abs_title=metadata.get('title') or suggestion.title or source_id,
            status='active',
            duration=(item or {}).get('media', {}).get('duration'),
            sync_mode='audiobook',
        )
        database_service.save_book(abs_book)
        abs_service = container.abs_service()
        if abs_service and abs_service.is_available():
            try:
                abs_service.add_to_collection(source_id, current_app.config['ABS_COLLECTION_NAME'])
            except Exception as e:
                logger.warning(f"Failed to add '{source_id}' to ABS collection during BookFusion link: {e}")

    for bid in bookfusion_ids:
        database_service.set_bookfusion_book_match(bid, source_id)
        database_service.link_bookfusion_book(bid, source_id)

    database_service.resolve_suggestion(source_id)
    return jsonify({"success": True, "abs_id": source_id})


@api_bp.route('/api/sync-reading-dates', methods=['POST'])
def sync_reading_dates_api():
    """Pull started_at / finished_at from Hardcover and ABS for books missing them."""
    from src.services.reading_date_service import sync_reading_dates
    database_service = get_database_service()
    container = get_container()
    stats = sync_reading_dates(database_service, container)
    logger.info(f"Sync reading dates: {stats}")
    return jsonify({"success": True, **stats})


# ---------------- Storyteller ----------------

@api_bp.route('/api/storyteller/search', methods=['GET'])
def api_storyteller_search():
    container = get_container()
    query = request.args.get('q', '')
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    results = container.storyteller_client().search_books(query)
    return jsonify(results)


@api_bp.route('/api/storyteller/link/<abs_id>', methods=['POST'])
def api_storyteller_link(abs_id):
    database_service = get_database_service()

    data = request.get_json()
    if not data or 'uuid' not in data:
        return jsonify({"error": "Missing 'uuid' in JSON payload"}), 400

    storyteller_uuid = data['uuid']
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"error": "Book not found"}), 404

    # Handle explicit unlinking
    if storyteller_uuid == "none" or not storyteller_uuid:
        logger.info(f"Unlinking Storyteller for '{book.abs_title}'")
        book.storyteller_uuid = None
        book.status = 'pending'
        database_service.save_book(book)
        return jsonify({"message": "Storyteller unlinked successfully", "filename": book.ebook_filename}), 200

    book.storyteller_uuid = storyteller_uuid
    book.status = 'pending'
    database_service.save_book(book)
    database_service.resolve_suggestion(abs_id)
    return jsonify({"message": "Linked successfully"}), 200


# ---------------- Booklore ----------------

def _get_booklore_libraries(client_getter, name):
    container = get_container()
    client = client_getter(container)
    if not client.is_configured():
        return jsonify({"error": f"{name} not configured"}), 400
    return jsonify(client.get_libraries())


@api_bp.route('/api/booklore/libraries', methods=['GET'])
def get_booklore_libraries():
    """Return available Booklore libraries."""
    return _get_booklore_libraries(lambda c: c.booklore_client(), "Booklore")



@api_bp.route('/api/booklore/search', methods=['GET'])
def api_booklore_search():
    """Search Booklore books by title/author/filename."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    client = get_booklore_client()
    if not client.is_configured():
        return jsonify([])

    try:
        label = current_app.config.get("BOOKLORE_LABEL", "Booklore")
        results = []
        books = client.search_books(query)
        for b in (books or []):
            results.append({
                'id': b.get('id'),
                'title': b.get('title', ''),
                'authors': b.get('authors', ''),
                'fileName': b.get('fileName', ''),
                'source': label,
            })
        return jsonify(results)
    except Exception:
        logger.warning("Booklore search failed", exc_info=True)
        return jsonify([])


@api_bp.route('/api/booklore/link/<abs_id>', methods=['POST'])
def api_booklore_link(abs_id):
    """Link or unlink a PageKeeper book to a Booklore book by filename."""
    database_service = get_database_service()
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"error": "Book not found"}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "No data provided"}), 400

    if 'filename' not in data:
        return jsonify({"error": "Missing 'filename' in JSON payload"}), 400
    filename_raw = data.get('filename')
    if filename_raw is None:
        filename = ''
    elif not isinstance(filename_raw, str):
        return jsonify({"error": "'filename' must be a string or null"}), 400
    else:
        filename = filename_raw.strip()

    if not filename:
        logger.info(f"Unlinking Booklore for '{book.abs_title}'")
        book.ebook_filename = None
        book.original_ebook_filename = None
        book.kosync_doc_id = None
        database_service.save_book(book)
        return jsonify({"success": True, "message": "Booklore unlinked"})

    book.ebook_filename = filename
    # Recompute KOSync ID for the new ebook file
    booklore_id = None
    bl_book, bl_client = find_in_booklore(filename)
    if bl_book:
        booklore_id = bl_book.get('id')
    kosync_doc_id = get_kosync_id_for_ebook(filename, booklore_id, bl_client=bl_client)
    if kosync_doc_id:
        book.kosync_doc_id = kosync_doc_id
    book.original_ebook_filename = book.original_ebook_filename or filename
    database_service.save_book(book)
    logger.info(f"Linked Booklore file '{filename}' to '{book.abs_title}'")
    return jsonify({"success": True, "message": "Linked successfully"})
