"""Books blueprint — delete, clear_progress, sync_now, mark_complete, pause, dnf, resume, update_hash."""

import logging
import threading

from flask import Blueprint, flash, jsonify, redirect, request, url_for

from src.blueprints.helpers import (
    cleanup_mapping_resources,
    find_in_grimmory,
    get_book_or_404,
    get_container,
    get_database_service,
    get_kosync_id_for_ebook,
    get_manager,
)
from src.services.reading_service import ReadingService
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


def _get_reading_service():
    return ReadingService(get_database_service())


books_bp = Blueprint("books", __name__)


@books_bp.route("/delete/<book_ref>", methods=["POST"])
def delete_mapping(book_ref):
    database_service = get_database_service()
    book = get_book_or_404(book_ref)
    cleanup_mapping_resources(book)

    database_service.delete_book(book.id)
    next_url = request.args.get("next") or request.form.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("dashboard.index"))


@books_bp.route("/clear-progress/<book_ref>", methods=["POST"])
def clear_progress(book_ref):
    """Clear progress for a mapping by setting all systems to 0%"""
    manager = get_manager()
    book = get_book_or_404(book_ref)

    title = sanitize_log_data(book.title or str(book.id))

    def _run():
        try:
            logger.info(f"Clearing progress for {title}")
            manager.clear_progress(book.id)
            logger.info(f"Progress cleared successfully for {title}")
        except Exception as e:
            logger.error(f"Failed to clear progress for book {book.id}: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("dashboard.index"))


@books_bp.route("/api/retry-transcription/<book_ref>", methods=["POST"])
def retry_transcription(book_ref):
    database_service = get_database_service()
    book = get_book_or_404(book_ref)

    if book.status not in ("failed_retry_later", "failed_permanent"):
        return jsonify({"success": False, "error": "Book is not in a failed state"}), 400

    logger.info(f"Retrying transcription for '{sanitize_log_data(book.title or str(book.id))}'")
    database_service.delete_jobs_for_book(book.id)
    book.status = "pending"
    database_service.save_book(book)
    return jsonify({"success": True})


@books_bp.route("/api/realign/<book_ref>", methods=["POST"])
def realign_book(book_ref):
    """Delete existing alignment and requeue book for re-processing."""
    book = get_book_or_404(book_ref)

    if book.sync_mode == "ebook_only":
        return jsonify({"success": False, "error": "Ebook-only books do not use alignment"}), 400

    container = get_container()
    alignment_service = container.alignment_service()

    logger.info(f"Re-aligning '{sanitize_log_data(book.title or str(book.id))}'")
    alignment_service.realign_book(book.id)
    return jsonify({"success": True})


@books_bp.route("/api/sync-now/<book_ref>", methods=["POST"])
def sync_now(book_ref):
    manager = get_manager()
    book = get_book_or_404(book_ref)

    if book.status == "completed":
        container = get_container()
        _get_reading_service().mark_complete_with_sync(book.id, container)
        return jsonify({"success": True, "reload": True})
    else:
        threading.Thread(target=manager.sync_cycle, kwargs={"target_book_id": book.id}, daemon=True).start()
        return jsonify({"success": True})


@books_bp.route("/api/mark-complete/<book_ref>", methods=["POST"])
def mark_complete(book_ref):
    book = get_book_or_404(book_ref)
    perform_delete = request.json.get("delete", False) if request.json else False
    container = get_container()
    result = _get_reading_service().mark_complete_with_sync(book.id, container, perform_delete=perform_delete)
    if not result["success"]:
        return jsonify(result), 404
    return jsonify(result)


@books_bp.route("/api/pause/<book_ref>", methods=["POST"])
def pause_book(book_ref):
    book = get_book_or_404(book_ref)
    container = get_container()
    result = _get_reading_service().update_status(book.id, "paused", container, allowed_from=("active", "not_started"))
    if not result["success"]:
        status_code = 404 if result["error"] == "Book not found" else 400
        return jsonify(result), status_code
    return jsonify(result)


@books_bp.route("/api/dnf/<book_ref>", methods=["POST"])
def dnf_book(book_ref):
    book = get_book_or_404(book_ref)
    container = get_container()
    result = _get_reading_service().update_status(
        book.id, "dnf", container, allowed_from=("active", "paused", "not_started")
    )
    if not result["success"]:
        status_code = 404 if result["error"] == "Book not found" else 400
        return jsonify(result), status_code
    return jsonify(result)


@books_bp.route("/api/resume/<book_ref>", methods=["POST"])
def resume_book(book_ref):
    book = get_book_or_404(book_ref)
    container = get_container()
    result = _get_reading_service().update_status(
        book.id, "active", container, allowed_from=("paused", "dnf", "not_started")
    )
    if not result["success"]:
        status_code = 404 if result["error"] == "Book not found" else 400
        return jsonify(result), status_code
    return jsonify(result)


@books_bp.route("/update-hash/<book_ref>", methods=["POST"])
def update_hash(book_ref):
    manager = get_manager()
    book = get_book_or_404(book_ref)

    new_hash = request.form.get("new_hash", "").strip()

    old_hash = book.kosync_doc_id

    if new_hash:
        book.kosync_doc_id = new_hash
        get_database_service().save_book(book)
        logger.info(f"Updated KoSync hash for '{sanitize_log_data(book.title)}' to manual input: '{new_hash}'")
        updated = True
    else:
        target_filename = book.original_ebook_filename or book.ebook_filename

        grimmory_id = None
        bl_book, matched_bl_client = find_in_grimmory(target_filename)
        if bl_book:
            grimmory_id = bl_book.get("id")
        grimmory_file_id = bl_book.get("bookFileId") if bl_book and bl_book.get("isPrimary") is False else None

        recalc_hash = get_kosync_id_for_ebook(
            target_filename,
            grimmory_id,
            original_filename=book.ebook_filename,
            bl_client=matched_bl_client,
            grimmory_file_id=grimmory_file_id,
        )

        if recalc_hash:
            book.kosync_doc_id = recalc_hash
            get_database_service().save_book(book)
            logger.info(f"Auto-regenerated KoSync hash for '{sanitize_log_data(book.title)}': '{recalc_hash}'")
            updated = True
        else:
            flash("Could not recalculate hash (file not found?)", "error")
            return redirect(url_for("dashboard.index"))

    if updated:
        from src.services.kosync_service import ensure_kosync_document

        ensure_kosync_document(book, get_database_service())
        if book.kosync_doc_id != old_hash:
            logger.info(
                f"Hash changed for '{sanitize_log_data(book.title)}' -- triggering instant sync to reconcile progress"
            )
            threading.Thread(target=manager.sync_cycle, kwargs={"target_book_id": book.id}, daemon=True).start()

    flash(f"Updated KoSync Hash for {book.title}", "success")
    return redirect(url_for("dashboard.index"))
