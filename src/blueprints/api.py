"""API blueprint — /api/status, /api/detected/*, /api/storyteller/*, /api/grimmory/*.

ABS-specific routes (/api/abs/*, /api/cover-proxy/*) are in abs_bp.py.
"""

import logging

from flask import Blueprint, current_app, jsonify, request

from src.blueprints.helpers import (
    find_in_grimmory,
    get_book_or_404,
    get_container,
    get_database_service,
    get_grimmory_client,
    get_kosync_id_for_ebook,
    serialize_suggestion,
)
from src.db.models import Book
from src.utils.http import json_error

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)

_VALID_SUGGESTION_SOURCES = ("abs", "kosync", "storyteller", "grimmory")


# ---------------- Detected Books ----------------


@api_bp.route("/api/detected", methods=["GET"])
def get_detected_books():
    """Return active detected books."""
    database_service = get_database_service()
    try:
        detected = database_service.get_active_detected_books(limit=50)
        results = []
        for d in detected:
            results.append(
                {
                    "id": d.id,
                    "source": d.source,
                    "source_id": d.source_id,
                    "title": d.title,
                    "author": d.author,
                    "cover_url": d.cover_url,
                    "progress_percentage": d.progress_percentage,
                    "first_detected_at": d.first_detected_at.isoformat() if d.first_detected_at else None,
                    "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                    "source_updated_at": (
                        d.source_updated_at.isoformat() if getattr(d, "source_updated_at", None) else None
                    ),
                    "device": d.device,
                    "ebook_filename": d.ebook_filename,
                    "status": d.status,
                }
            )
        return jsonify({"success": True, "detected": results})
    except Exception as e:
        logger.error(f"Failed to get detected books: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@api_bp.route("/api/detected/<source_id>/dismiss", methods=["POST"])
def dismiss_detected_book(source_id):
    """Dismiss a detected book."""
    database_service = get_database_service()
    source = request.args.get("source", "abs")
    if source not in _VALID_SUGGESTION_SOURCES:
        return jsonify({"success": False, "error": "Invalid source"}), 400
    if database_service.dismiss_detected_book(source_id, source=source):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


@api_bp.route("/api/detected/<source_id>/resolve", methods=["POST"])
def resolve_detected_book(source_id):
    """Mark a detected book as resolved (added to library)."""
    database_service = get_database_service()
    source = request.args.get("source", "abs")
    if source not in _VALID_SUGGESTION_SOURCES:
        return jsonify({"success": False, "error": "Invalid source"}), 400
    if database_service.resolve_detected_book(source_id, source=source):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


# ---------------- Readiness ----------------


@api_bp.route("/readyz")
def readyz():
    """Readiness probe backing the Docker HEALTHCHECK.

    503 until database migrations/schema are known safe. Liveness/connectivity
    stays on the unconditional KOSync /healthcheck.
    """
    database_service = current_app.config.get("database_service")
    try:
        ready, reason = database_service.startup_ready()
    except Exception:
        ready, reason = False, "database service unavailable"
    return jsonify({"ready": ready, "reason": reason}), (200 if ready else 503)


# ---------------- Status ----------------


@api_bp.route("/api/status")
def api_status():
    """Return status of all books from database service"""
    database_service = get_database_service()
    books = database_service.get_all_books()

    # Bulk-fetch all states to avoid N+1 queries (one per book)
    all_states = database_service.get_all_states()
    states_by_book = {}
    for state in all_states:
        states_by_book.setdefault(state.book_id, []).append(state)

    mappings = []
    for book in books:
        state_by_client = {state.client_name: state for state in states_by_book.get(book.id, [])}

        mapping = {
            "id": book.id,
            "abs_id": book.abs_id,
            "title": book.title,
            "ebook_filename": book.ebook_filename,
            "kosync_doc_id": book.kosync_doc_id,
            "transcript_file": book.transcript_file,
            "status": book.status,
            "sync_mode": book.sync_mode,
            "duration": book.duration,
            "storyteller_uuid": book.storyteller_uuid,
            "states": {},
        }

        for client_name, state in state_by_client.items():
            pct_val = round(state.percentage * 100, 1) if state.percentage is not None else 0

            mapping["states"][client_name] = {
                "timestamp": state.timestamp or 0,
                "percentage": pct_val,
                "xpath": getattr(state, "xpath", None),
                "last_updated": state.last_updated,
            }

            if client_name == "kosync":
                mapping["kosync_pct"] = pct_val
                mapping["kosync_xpath"] = getattr(state, "xpath", None)
            elif client_name == "abs":
                mapping["abs_pct"] = pct_val
                mapping["abs_ts"] = state.timestamp
            elif client_name == "storyteller":
                mapping["storyteller_pct"] = pct_val
                mapping["storyteller_xpath"] = getattr(state, "xpath", None)
            elif client_name == "grimmory":
                mapping["grimmory_pct"] = pct_val
                mapping["grimmory_xpath"] = getattr(state, "xpath", None)

        # Compute unified_progress — max percentage across all clients
        all_pcts = [s["percentage"] for s in mapping["states"].values()]
        mapping["unified_progress"] = min(max(all_pcts), 100.0) if all_pcts else 0

        mappings.append(mapping)

    return jsonify({"mappings": mappings})


# ---------------- Processing Status ----------------


@api_bp.route("/api/processing-status")
def api_processing_status():
    """Return status and progress for all non-active (processing/pending/failed) books."""
    database_service = get_database_service()
    books = database_service.get_all_books()
    processing_books = [b for b in books if b.status in ("pending", "processing", "failed_retry_later")]
    jobs_by_book = database_service.get_latest_jobs_bulk([b.id for b in processing_books])
    result = {}
    for book in processing_books:
        job = jobs_by_book.get(book.id)
        result[str(book.id)] = {
            "status": book.status,
            "job_progress": round((job.progress or 0.0) * 100, 1) if job else 0.0,
            "retry_count": (job.retry_count or 0) if job else 0,
        }
    return jsonify(result)


# ---------------- Suggestions ----------------


@api_bp.route("/api/suggestions", methods=["GET"])
def get_suggestions():
    database_service = get_database_service()
    suggestions = database_service.get_all_actionable_suggestions()
    return jsonify([serialize_suggestion(s) for s in suggestions if s.matches])


@api_bp.route("/api/suggestions/rescan", methods=["POST"])
def rescan_suggestions():
    container = get_container()
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    stats = container.suggestion_service().request_rescan_library_suggestions(force=force)
    return jsonify({"success": True, **stats})


@api_bp.route("/api/suggestions/rescan-status", methods=["GET"])
def rescan_suggestions_status():
    container = get_container()
    status = container.suggestion_service().get_rescan_status()
    return jsonify({"success": True, **status})


@api_bp.route("/api/suggestions/<source_id>/hide", methods=["POST"])
def hide_suggestion(source_id):
    database_service = get_database_service()
    source = request.args.get("source", "abs")
    if source not in _VALID_SUGGESTION_SOURCES:
        return json_error("Invalid source", 400)
    if database_service.hide_suggestion(source_id, source=source):
        return jsonify({"success": True})
    return json_error("Not found", 404)


@api_bp.route("/api/suggestions/<source_id>/unhide", methods=["POST"])
def unhide_suggestion(source_id):
    database_service = get_database_service()
    source = request.args.get("source", "abs")
    if source not in _VALID_SUGGESTION_SOURCES:
        return json_error("Invalid source", 400)
    if database_service.unhide_suggestion(source_id, source=source):
        return jsonify({"success": True})
    return json_error("Not found", 404)


@api_bp.route("/api/suggestions/<source_id>/ignore", methods=["POST"])
def ignore_suggestion(source_id):
    database_service = get_database_service()
    source = request.args.get("source", "abs")
    if source not in _VALID_SUGGESTION_SOURCES:
        return json_error("Invalid source", 400)
    if database_service.ignore_suggestion(source_id, source=source):
        return jsonify({"success": True})
    return json_error("Not found", 404)


@api_bp.route("/api/suggestions/clear_stale", methods=["POST"])
def clear_stale_suggestions():
    database_service = get_database_service()
    count = database_service.clear_stale_suggestions()
    logger.info(f"Cleared {count} stale suggestions from database")
    return jsonify({"success": True, "count": count})


@api_bp.route("/api/suggestions/<source_id>/link-bookfusion", methods=["POST"])
def link_suggestion_bookfusion(source_id):
    database_service = get_database_service()
    container = get_container()
    data = request.get_json(silent=True) or {}
    source = data.get("source", "abs")
    if source not in _VALID_SUGGESTION_SOURCES:
        return json_error("Invalid source", 400)

    suggestion = database_service.get_pending_suggestion(source_id, source=source)
    if not suggestion:
        return json_error("Suggestion not found", 404)

    match_index = data.get("match_index")
    matches = suggestion.matches or []
    if match_index is None or not isinstance(match_index, int) or match_index < 0 or match_index >= len(matches):
        return json_error("Valid match_index required", 400)

    match = matches[match_index]
    bookfusion_ids = match.get("bookfusion_ids") or []
    if match.get("source_family") != "bookfusion" or not bookfusion_ids:
        return json_error("Selected match is not a BookFusion candidate", 400)

    # Find or create the book to link BookFusion to
    if source == "abs":
        book = database_service.get_book_by_ref(source_id)
        if not book:
            abs_client = container.abs_client()
            item = abs_client.get_item_details(source_id) if abs_client else None
            metadata = (item or {}).get("media", {}).get("metadata", {})
            book = Book(
                abs_id=source_id,
                title=metadata.get("title") or suggestion.title or source_id,
                status="not_started",
                duration=(item or {}).get("media", {}).get("duration"),
                sync_mode="audiobook",
            )
            database_service.save_book(book)
            abs_service = container.abs_service()
            if abs_service and abs_service.is_available():
                try:
                    abs_service.add_to_collection(source_id, current_app.config["ABS_COLLECTION_NAME"])
                except Exception as e:
                    logger.warning(f"Failed to add '{source_id}' to ABS collection during BookFusion link: {e}")
            book = database_service.get_book_by_ref(source_id)
    else:
        # Non-ABS source: look up by the field matching the source type
        if source == "storyteller":
            book = database_service.get_book_by_storyteller_uuid(source_id)
        elif source == "kosync":
            book = database_service.get_book_by_kosync_id(source_id)
        else:
            book = database_service.get_book_by_ebook_filename(source_id)
        if not book:
            book_kwargs = {
                "abs_id": None,
                "title": suggestion.title or source_id,
                "status": "not_started",
                "sync_mode": "ebook_only",
            }
            if source == "storyteller":
                book_kwargs["storyteller_uuid"] = source_id
            elif source == "grimmory":
                book_kwargs["ebook_filename"] = source_id
            elif source == "kosync":
                book_kwargs["kosync_doc_id"] = source_id
            book = Book(**book_kwargs)
            database_service.save_book(book, is_new=True)
            book = database_service.get_book_by_id(book.id)

    if not book:
        return json_error("Could not find or create book", 500)

    for bid in bookfusion_ids:
        database_service.set_bookfusion_book_match_by_book_id(bid, book.id)
        database_service.link_bookfusion_highlights_by_book_id(bid, book.id)

    database_service.resolve_suggestion(source_id, source=source)
    return jsonify({"success": True, "book_id": book.id})


@api_bp.route("/api/sync-reading-dates", methods=["POST"])
def sync_reading_dates_api():
    """Auto-complete books at 100% progress and fill missing dates."""
    container = get_container()
    stats = container.reading_date_service().auto_complete_finished_books(container)
    logger.info(f"Auto-complete check: {stats}")
    return jsonify({"success": True, **stats})


# ---------------- Storyteller ----------------


@api_bp.route("/api/storyteller/search", methods=["GET"])
def api_storyteller_search():
    container = get_container()
    query = request.args.get("q", "")
    if not query:
        return json_error("Query parameter 'q' is required", 400)
    results = container.storyteller_client().search_books(query)
    return jsonify(results)


@api_bp.route("/api/storyteller/link/<book_ref>", methods=["POST"])
def api_storyteller_link(book_ref):
    database_service = get_database_service()

    data = request.get_json()
    if not data or "uuid" not in data:
        return json_error("Missing 'uuid' in JSON payload", 400)

    storyteller_uuid = data["uuid"]
    book = get_book_or_404(book_ref)

    # Handle explicit unlinking
    if storyteller_uuid == "none" or not storyteller_uuid:
        logger.info(f"Unlinking Storyteller for '{book.title}'")
        book.storyteller_uuid = None
        book.status = "pending"
        database_service.save_book(book)
        return jsonify({"message": "Storyteller unlinked successfully", "filename": book.ebook_filename}), 200

    book.storyteller_uuid = storyteller_uuid
    book.status = "pending"
    database_service.save_book(book)
    if book.abs_id:
        database_service.resolve_suggestion(book.abs_id)
    return jsonify({"message": "Linked successfully"}), 200


# ---------------- Grimmory ----------------


def _get_grimmory_libraries(client_getter, name):
    container = get_container()
    client = client_getter(container)
    if not client.is_configured():
        return json_error(f"{name} not configured", 400)
    return jsonify(client.get_libraries())


@api_bp.route("/api/grimmory/libraries", methods=["GET"])
def get_grimmory_libraries():
    """Return available Grimmory libraries."""
    return _get_grimmory_libraries(lambda c: c.grimmory_client(), "Grimmory")


@api_bp.route("/api/grimmory2/libraries", methods=["GET"])
def get_grimmory2_libraries():
    """Return available Grimmory 2 libraries."""
    return _get_grimmory_libraries(lambda c: c.grimmory_client_2(), "Grimmory 2")


@api_bp.route("/api/grimmory/search", methods=["GET"])
def api_grimmory_search():
    """Search Grimmory books by title/author/filename."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    client = get_grimmory_client()
    if not client.is_configured():
        return jsonify([])

    try:
        label = current_app.config.get("GRIMMORY_LABEL", "Grimmory")
        results = []
        books = client.search_books(query)
        for b in books or []:
            results.append(
                {
                    "id": b.get("id"),
                    "title": b.get("title", ""),
                    "authors": b.get("authors", ""),
                    "fileName": b.get("fileName", ""),
                    "source": label,
                }
            )
        return jsonify(results)
    except Exception:
        logger.warning("Grimmory search failed", exc_info=True)
        return jsonify([])


@api_bp.route("/api/grimmory/link/<book_ref>", methods=["POST"])
def api_grimmory_link(book_ref):
    """Link or unlink a PageKeeper book to a Grimmory book by filename."""
    database_service = get_database_service()
    book = get_book_or_404(book_ref)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json_error("No data provided", 400)

    if "filename" not in data:
        return json_error("Missing 'filename' in JSON payload", 400)
    filename_raw = data.get("filename")
    if filename_raw is None:
        filename = ""
    elif not isinstance(filename_raw, str):
        return json_error("'filename' must be a string or null", 400)
    else:
        filename = filename_raw.strip()

    if not filename:
        logger.info(f"Unlinking Grimmory for '{book.title}'")
        book.ebook_filename = None
        book.original_ebook_filename = None
        book.kosync_doc_id = None
        database_service.save_book(book)
        return jsonify({"success": True, "message": "Grimmory unlinked"})

    book.ebook_filename = filename
    # Recompute KOSync ID for the new ebook file
    grimmory_id = None
    bl_book, bl_client = find_in_grimmory(filename)
    if bl_book:
        grimmory_id = bl_book.get("id")
    kosync_doc_id = get_kosync_id_for_ebook(filename, grimmory_id, bl_client=bl_client)
    if kosync_doc_id:
        book.kosync_doc_id = kosync_doc_id
    book.original_ebook_filename = book.original_ebook_filename or filename
    database_service.save_book(book)
    from src.services.kosync_service import ensure_kosync_document

    ensure_kosync_document(book, database_service)
    logger.info(f"Linked Grimmory file '{filename}' to '{book.title}'")
    return jsonify({"success": True, "message": "Linked successfully"})
