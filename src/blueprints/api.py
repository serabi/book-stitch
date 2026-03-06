"""API blueprint — /api/status, /api/suggestions/*, /api/storyteller/*, /api/booklore/*.

ABS-specific routes (/api/abs/*, /api/cover-proxy/*) are in abs_bp.py.
"""

import json
import logging
import os

from flask import Blueprint, jsonify, request

from src.blueprints.helpers import get_booklore_clients, get_container, get_database_service

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__)


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
    suggestions = database_service.get_all_pending_suggestions()
    result = []
    for s in suggestions:
        try:
            matches = json.loads(s.matches_json) if s.matches_json else []
        except Exception as e:
            logger.debug(f"Failed to parse matches_json for suggestion '{s.source_id}': {e}")
            matches = []

        result.append({
            "id": s.id,
            "source_id": s.source_id,
            "title": s.title,
            "author": s.author,
            "cover_url": s.cover_url,
            "matches": matches,
            "created_at": s.created_at.isoformat()
        })
    return jsonify(result)


@api_bp.route('/api/suggestions/<source_id>/dismiss', methods=['POST'])
def dismiss_suggestion(source_id):
    database_service = get_database_service()
    if database_service.dismiss_suggestion(source_id):
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
    database_service.dismiss_suggestion(abs_id)
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

    results = []
    for client in get_booklore_clients():
        if not client.is_configured():
            continue
        try:
            label = os.environ.get(f"{client.config_prefix}_LABEL", "Booklore")
            books = client.search_books(query)
            for b in (books or []):
                results.append({
                    'id': b.get('id'),
                    'title': b.get('title', ''),
                    'authors': b.get('authors', ''),
                    'fileName': b.get('fileName', ''),
                    'source': label,
                    'source_tag': client.source_tag,
                })
        except Exception:
            logger.warning("Booklore search failed for source_tag=%s", client.source_tag, exc_info=True)

    return jsonify(results)


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
        database_service.save_book(book)
        return jsonify({"success": True, "message": "Booklore unlinked"})

    book.ebook_filename = filename
    database_service.save_book(book)
    logger.info(f"Linked Booklore file '{filename}' to '{book.abs_title}'")
    return jsonify({"success": True, "message": "Linked successfully"})
