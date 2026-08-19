"""Kobo blueprint — upload database copies, inspect device books, manage links."""

import logging
import time

from flask import Blueprint, jsonify, render_template, request

from src.blueprints.helpers import get_container, get_database_service

logger = logging.getLogger(__name__)

kobo_bp = Blueprint("kobo", __name__)


def _get_kobo_service():
    return get_container().kobo_service()


def _device_book_payloads(db_service):
    """Serialize all device books with match state and library titles."""
    books = db_service.get_kobo_books(include_hidden=True)
    bookmark_counts = db_service.get_kobo_bookmark_counts_by_content_id()
    titles = {book.id: book.title for book in db_service.get_all_books()}
    return [
        {
            "content_id": b.content_id,
            "title": b.title,
            "author": b.author,
            "isbn": b.isbn,
            "percent": b.percent,
            "read_status": b.read_status,
            "date_last_read": b.date_last_read.isoformat() if b.date_last_read else None,
            "time_spent_seconds": b.time_spent_seconds,
            "matched_book_id": b.matched_book_id,
            "matched_book_title": titles.get(b.matched_book_id),
            "hidden": b.hidden,
            "bookmark_count": bookmark_counts.get(b.content_id, 0),
        }
        for b in books
    ]


@kobo_bp.route("/api/kobo/upload", methods=["POST"])
def upload_database():
    """Accept a KoboReader.sqlite copy and ingest it."""
    kobo_service = _get_kobo_service()
    if kobo_service.upload_dir is None:
        return jsonify({"error": "Kobo uploads unavailable: no data dir configured"}), 400

    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "No file provided"}), 400
    if not file.filename.lower().endswith(".sqlite"):
        return jsonify({"error": "File must be a KoboReader.sqlite database"}), 400

    kobo_service.upload_dir.mkdir(parents=True, exist_ok=True)
    target = kobo_service.upload_dir / f"kobo-{int(time.time())}.sqlite"
    file.save(target)
    logger.info("Kobo: received database upload %s (%d bytes)", target.name, target.stat().st_size)

    changed = kobo_service.refresh_if_changed()
    kobo_books = kobo_service.database_service.get_kobo_books()
    matched = [b for b in kobo_books if b.matched_book_id]
    return jsonify(
        {
            "success": True,
            "ingested": changed,
            "device_books": len(kobo_books),
            "matched_books": len(matched),
        }
    )


@kobo_bp.route("/api/kobo/books", methods=["GET"])
def list_device_books():
    """List ingested Kobo device books and their library match state."""
    kobo_service = _get_kobo_service()
    kobo_service.refresh_if_changed()
    books = _device_book_payloads(kobo_service.database_service)
    return jsonify({"books": books, "configured": kobo_service.is_configured()})


@kobo_bp.route("/kobo-books")
def kobo_books_page():
    """Kobo device book management: match, link, hide, import highlights."""
    kobo_service = _get_kobo_service()
    kobo_service.refresh_if_changed()
    books = _device_book_payloads(kobo_service.database_service)
    return render_template(
        "kobo_books.html",
        books=books,
        configured=kobo_service.is_configured(),
        db_file_count=len(kobo_service.database_copies()),
    )


@kobo_bp.route("/api/kobo/hide", methods=["POST"])
def hide_device_book():
    """Hide (or unhide) a device book so it leaves the matching/suggestion flow."""
    data = request.get_json()
    if not data or not data.get("content_id"):
        return jsonify({"error": "content_id required"}), 400

    db_service = get_database_service()
    if not db_service.get_kobo_book(data["content_id"]):
        return jsonify({"error": "Kobo book not found"}), 404
    hidden = bool(data.get("hidden", True))
    db_service.set_kobo_books_hidden([data["content_id"]], hidden)
    return jsonify({"success": True, "hidden": hidden})


@kobo_bp.route("/api/kobo/link", methods=["POST"])
def link_device_book():
    """Manually link a Kobo device book to a library book."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    content_id = data.get("content_id")
    book_id = data.get("book_id")
    if not content_id or not book_id:
        return jsonify({"error": "content_id and book_id required"}), 400

    db_service = get_database_service()
    if not db_service.get_kobo_book(content_id):
        return jsonify({"error": "Kobo book not found"}), 404
    if not db_service.get_book_by_id(int(book_id)):
        return jsonify({"error": "Library book not found"}), 404

    _get_kobo_service().link_book(content_id, int(book_id))
    return jsonify({"success": True})


@kobo_bp.route("/api/kobo/unlink", methods=["POST"])
def unlink_device_book():
    """Remove a Kobo device book's library link (including its bookmarks)."""
    data = request.get_json()
    if not data or not data.get("content_id"):
        return jsonify({"error": "content_id required"}), 400

    kobo_service = _get_kobo_service()
    if not kobo_service.database_service.get_kobo_book(data["content_id"]):
        return jsonify({"error": "Kobo book not found"}), 404
    kobo_service.unlink_book(data["content_id"])
    return jsonify({"success": True})


@kobo_bp.route("/api/kobo/save-journal", methods=["POST"])
def save_bookmarks_to_journal():
    """Import a matched Kobo book's highlights/notes as reading journal entries."""
    data = request.get_json()
    if not data or not data.get("book_id"):
        return jsonify({"error": "book_id required"}), 400

    kobo_service = _get_kobo_service()
    result = kobo_service.save_bookmarks_to_journal(int(data["book_id"]))
    if result.get("error"):
        return jsonify({"error": result["error"]}), 404
    return jsonify(result)


@kobo_bp.route("/api/kobo/sync", methods=["POST"])
def sync_now():
    """Force a re-scan of database copies."""
    kobo_service = _get_kobo_service()
    changed = kobo_service.refresh_if_changed()
    # Force refresh even when file mtimes are unchanged (e.g. clock skew)
    if not changed:
        kobo_service._last_signatures = {}
        changed = kobo_service.refresh_if_changed()
    return jsonify({"success": True, "changed": changed})
