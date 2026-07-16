"""Matching blueprint — suggestions, single match, batch match."""

import logging
import os
from pathlib import Path

from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, session, url_for
from markupsafe import Markup

from src.blueprints.helpers import (
    any_grimmory_configured,
    attempt_hardcover_automatch,
    audiobook_matches_search,
    find_in_grimmory,
    get_abs_service,
    get_audiobook_author,
    get_audiobooks_conditionally,
    get_container,
    get_database_service,
    get_ebook_dir,
    get_kosync_id_for_ebook,
    get_manager,
    get_searchable_ebooks,
    serialize_suggestion,
)
from src.services.book_intake_service import BookIntakeService
from src.utils.logging_utils import sanitize_log_data
from src.utils.path_utils import sanitize_filename

logger = logging.getLogger(__name__)

matching_bp = Blueprint("matching", __name__)


def _escape_template_value(value):
    return Markup.escape(value or "")


def _redirect_search_value():
    """Return a bounded same-route search value for redirects."""
    return (request.form.get("search") or "")[:200]


def _plain_error_response(message, status_code):
    return Response(message or "Request failed", status=status_code, mimetype="text/plain")


def _copy_book_merge_metadata(existing_book, overrides=None):
    return BookIntakeService._copy_book_merge_metadata(existing_book, overrides)


def _get_book_intake_service(container=None):
    container = container or get_container()
    return BookIntakeService(
        container=container,
        database_service=get_database_service(),
        abs_service=get_abs_service(),
        collection_name=current_app.config["ABS_COLLECTION_NAME"],
        books_dir=current_app.config.get("BOOKS_DIR", ""),
        epub_cache_dir=current_app.config.get("EPUB_CACHE_DIR", ""),
        find_in_grimmory=find_in_grimmory,
        get_kosync_id_for_ebook=get_kosync_id_for_ebook,
        attempt_hardcover_automatch=attempt_hardcover_automatch,
    )


def _create_book_mapping(
    container,
    abs_id,
    title,
    ebook_filename,
    duration,
    ebook_source_id=None,
    storyteller_uuid=None,
    storyteller_submit=False,
    author=None,
    subtitle=None,
):
    """Compatibility wrapper around the Book Intake Module."""
    result = _get_book_intake_service(container).map_audiobook_ebook(
        abs_id=abs_id,
        title=title,
        ebook_filename=ebook_filename,
        ebook_source_id=ebook_source_id,
        duration=duration,
        storyteller_uuid=storyteller_uuid,
        storyteller_submit=storyteller_submit,
        author=author,
        subtitle=subtitle,
    )
    return result.book, result.error


def _build_batch_queue_item(item):
    """Annotate queue entries with display-oriented fields without mutating session data."""
    ebook_label = item.get("ebook_display_name") or item.get("ebook_filename") or "Not selected"
    storyteller_selected = bool(item.get("storyteller_uuid"))
    storyteller_label = "Selected" if storyteller_selected else "None / Skip"

    if item.get("audio_only"):
        status_label = "Audio Only"
        status_kind = "audio-only"
    elif item.get("ebook_only"):
        status_label = "Ebook Only"
        status_kind = "ebook-only"
    elif item.get("abs_id") and item.get("ebook_filename"):
        status_label = "Ready"
        status_kind = "ready"
    else:
        status_label = "Incomplete"
        status_kind = "incomplete"

    return {
        **item,
        "ebook_label": ebook_label,
        "storyteller_label": storyteller_label,
        "storyteller_selected": storyteller_selected,
        "status_label": status_label,
        "status_kind": status_kind,
    }


def _build_batch_queue_view(queue):
    queue_items = [_build_batch_queue_item(item) for item in queue]
    return {
        "items": queue_items,
        "total_count": len(queue_items),
        "ready_count": sum(1 for item in queue_items if item["status_kind"] in {"ready", "audio-only", "ebook-only"}),
        "audio_only_count": sum(1 for item in queue_items if item["status_kind"] == "audio-only"),
        "ebook_only_count": sum(1 for item in queue_items if item["status_kind"] == "ebook-only"),
        "incomplete_count": sum(1 for item in queue_items if item["status_kind"] == "incomplete"),
    }


@matching_bp.route("/suggestions")
def suggestions():
    """Dedicated page for browsing and acting on pairing suggestions."""
    container = get_container()
    database_service = get_database_service()
    raw_suggestions = database_service.get_all_actionable_suggestions()
    suggestions_list = [serialize_suggestion(s) for s in raw_suggestions if s.matches]
    visible_count = sum(1 for s in suggestions_list if not s.get("hidden"))
    hidden_count = sum(1 for s in suggestions_list if s.get("hidden"))
    suggestions_enabled = current_app.config.get("SUGGESTIONS_ENABLED", False)
    bookfusion_enabled = container.bookfusion_client().is_configured()
    bookfusion_catalog_count = len(database_service.get_bookfusion_books()) if bookfusion_enabled else 0
    initial_search = request.args.get("search", "").strip()
    selected_source_id = request.args.get("source_id", "").strip()
    return render_template(
        "suggestions.html",
        suggestions=suggestions_list,
        visible_count=visible_count,
        hidden_count=hidden_count,
        suggestions_enabled=suggestions_enabled,
        bookfusion_enabled=bookfusion_enabled,
        bookfusion_catalog_count=bookfusion_catalog_count,
        suggestions_data=suggestions_list,
        initial_search=_escape_template_value(initial_search),
        selected_source_id=_escape_template_value(selected_source_id),
    )


@matching_bp.route("/match", methods=["GET", "POST"])
def match():
    container = get_container()
    manager = get_manager()
    database_service = get_database_service()

    if request.method == "POST":
        action = request.form.get("action", "")
        intake_service = _get_book_intake_service(container)

        # --- Audio-only import (no ebook required) ---
        if action == "audio_only":
            abs_service = get_abs_service()
            if not abs_service.is_available():
                return "ABS is not configured", 400
            abs_id = request.form.get("audiobook_id")
            audiobooks = abs_service.get_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab["id"] == abs_id), None)
            if not selected_ab:
                return "Audiobook not found", 404
            intake_service.import_audio_only(
                abs_id=abs_id,
                title=manager.get_audiobook_title(selected_ab),
                duration=manager.get_duration(selected_ab),
                author=get_audiobook_author(selected_ab),
                subtitle=selected_ab.get("media", {}).get("metadata", {}).get("subtitle") or None,
            )
            return redirect(url_for("dashboard.index"))

        # --- Ebook-only import (no audiobook required) ---
        if action == "ebook_only":
            ebook_filename = sanitize_filename(request.form.get("ebook_filename"))
            ebook_source_id = request.form.get("ebook_source_id") or None
            ebook_display_name = request.form.get("ebook_display_name", "")
            storyteller_uuid = request.form.get("storyteller_uuid") or None
            storyteller_title = request.form.get("storyteller_title", "")

            if not ebook_filename and not storyteller_uuid:
                return "An ebook or Storyteller selection is required", 400

            result = intake_service.import_ebook_only(
                ebook_filename=ebook_filename,
                ebook_source_id=ebook_source_id,
                ebook_display_name=ebook_display_name,
                storyteller_uuid=storyteller_uuid,
                storyteller_title=storyteller_title,
            )
            if result.error:
                return _plain_error_response(result.error, result.status_code)
            return redirect(url_for("dashboard.index"))

        # --- Attach ebook to audio-only book ---
        if action == "attach_ebook":
            attach_abs_id = request.form.get("attach_abs_id")
            ebook_filename = sanitize_filename(request.form.get("ebook_filename"))
            ebook_source_id = request.form.get("ebook_source_id") or None
            result = intake_service.attach_ebook(
                abs_id=attach_abs_id,
                ebook_filename=ebook_filename,
                ebook_source_id=ebook_source_id,
            )
            if result.error:
                return _plain_error_response(result.error, result.status_code)
            return redirect(url_for("dashboard.index"))

        # --- Attach audiobook to ebook-only book ---
        if action == "attach_audiobook":
            abs_service = get_abs_service()
            if not abs_service.is_available():
                return "ABS is not configured", 400
            link_book_id = request.form.get("link_book_id")
            abs_id = request.form.get("audiobook_id")
            if not link_book_id or not abs_id:
                return "Missing book ID or audiobook ID", 400
            book = database_service.get_book_by_ref(link_book_id)
            if not book:
                return "Book not found", 404
            audiobooks = abs_service.get_audiobooks()
            selected_ab = next((ab for ab in audiobooks if ab["id"] == abs_id), None)
            if not selected_ab:
                return "Audiobook not found", 404
            result = intake_service.attach_audiobook(
                source_book_id=link_book_id,
                abs_id=abs_id,
                title=manager.get_audiobook_title(selected_ab),
                duration=manager.get_duration(selected_ab),
                author=get_audiobook_author(selected_ab),
                subtitle=selected_ab.get("media", {}).get("metadata", {}).get("subtitle") or None,
            )
            if result.error:
                return _plain_error_response(result.error, result.status_code)
            return redirect(url_for("dashboard.index"))

        # --- Standard flow (requires audiobook) ---
        abs_service = get_abs_service()
        abs_id = request.form.get("audiobook_id")
        ebook_filename = sanitize_filename(request.form.get("ebook_filename"))
        ebook_source_id = request.form.get("ebook_source_id") or None
        storyteller_uuid = request.form.get("storyteller_uuid")
        storyteller_submit = request.form.get("storyteller_submit")

        if not ebook_filename:
            return "An ebook selection is required for audiobook + ebook matching", 400

        audiobooks = abs_service.get_audiobooks()
        selected_ab = next((ab for ab in audiobooks if ab["id"] == abs_id), None)
        if not selected_ab:
            return "Audiobook not found", 404

        _ab_meta = selected_ab.get("media", {}).get("metadata", {})
        book, error = _create_book_mapping(
            container,
            abs_id,
            title=manager.get_audiobook_title(selected_ab),
            ebook_filename=ebook_filename,
            ebook_source_id=ebook_source_id,
            duration=manager.get_duration(selected_ab),
            storyteller_uuid=storyteller_uuid,
            storyteller_submit=bool(storyteller_submit),
            author=get_audiobook_author(selected_ab),
            subtitle=_ab_meta.get("subtitle") or None,
        )
        if error:
            return _plain_error_response(error, 404)

        return redirect(url_for("dashboard.index"))

    # GET request
    search = request.args.get("search", "").strip().lower()
    attach_to = request.args.get("attach_to", "").strip()
    link_to = request.args.get("link_to", "").strip()
    preselect_abs_id = request.args.get("abs_id", "").strip()
    attach_title = ""
    link_title = ""

    if attach_to:
        attach_book = database_service.get_book_by_ref(attach_to)
        if attach_book:
            attach_title = attach_book.title or attach_to

    if link_to:
        link_book = database_service.get_book_by_ref(link_to)
        if link_book:
            link_title = link_book.title or link_to

    abs_service = get_abs_service()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        if not attach_to:
            audiobooks = get_audiobooks_conditionally()
            audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
            for ab in audiobooks:
                ab["cover_url"] = abs_service.get_cover_proxy_url(ab["id"])

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
        already_listed = any(ab["id"] == preselect_abs_id for ab in audiobooks)
        if not already_listed:
            all_audiobooks = get_audiobooks_conditionally()
            preselected_audiobook = next((ab for ab in all_audiobooks if ab["id"] == preselect_abs_id), None)
            if preselected_audiobook:
                preselected_audiobook["cover_url"] = abs_service.get_cover_proxy_url(preselect_abs_id)
                audiobooks.insert(0, preselected_audiobook)

    storyteller_submit_available = False
    try:
        st_sub_svc = container.storyteller_submission_service()
        storyteller_submit_available = st_sub_svc.is_available()
    except Exception as e:
        logger.debug("Storyteller submission availability check failed: %s", e)

    storyteller_force_mode = os.environ.get("STORYTELLER_FORCE_MODE", "false").lower() == "true"
    storyteller_configured = container.storyteller_client().is_configured()

    # Detect available services for smart mode defaults
    abs_configured = abs_service.is_available()
    has_ebook_sources = (
        any_grimmory_configured()
        or container.cwa_client().is_configured()
        or abs_service.has_ebook_libraries()
        or get_ebook_dir().exists()
    )

    # Build sets of IDs already in the library for "In Library" badges
    library_abs_ids = set()
    library_ebook_filenames = set()
    if search:
        all_books = database_service.get_all_books()
        library_abs_ids = {b.abs_id for b in all_books}
        library_ebook_filenames = {b.ebook_filename for b in all_books if b.ebook_filename}
        library_ebook_filenames |= {b.original_ebook_filename for b in all_books if b.original_ebook_filename}

    return render_template(
        "match.html",
        audiobooks=audiobooks,
        ebooks=ebooks,
        storyteller_books=storyteller_books,
        search=_escape_template_value(search),
        get_title=manager.get_audiobook_title,
        attach_to=_escape_template_value(attach_to),
        attach_title=_escape_template_value(attach_title),
        link_to=_escape_template_value(link_to),
        link_title=_escape_template_value(link_title),
        preselect_abs_id=_escape_template_value(preselect_abs_id),
        storyteller_submit_available=storyteller_submit_available,
        storyteller_force_mode=storyteller_force_mode,
        storyteller_configured=storyteller_configured,
        library_abs_ids=library_abs_ids,
        library_ebook_filenames=library_ebook_filenames,
        abs_configured=abs_configured,
        has_ebook_sources=has_ebook_sources,
    )


@matching_bp.route("/batch-match", methods=["GET", "POST"])
def batch_match():
    container = get_container()
    manager = get_manager()

    abs_service = get_abs_service()

    if request.method == "POST":
        action = request.form.get("action")
        intake_service = _get_book_intake_service(container)
        if action == "add_to_queue":
            session.setdefault("queue", [])
            abs_id = request.form.get("audiobook_id") or ""
            ebook_filename = sanitize_filename(request.form.get("ebook_filename", "")) or ""
            ebook_source_id = request.form.get("ebook_source_id", "")
            ebook_display_name = request.form.get("ebook_display_name", ebook_filename)
            storyteller_uuid = request.form.get("storyteller_uuid", "")

            if not abs_id and not ebook_filename and not storyteller_uuid:
                return redirect(url_for("matching.batch_match", search=_redirect_search_value()))

            # Resolve audiobook metadata if present
            selected_ab = None
            if abs_id:
                audiobooks = abs_service.get_audiobooks()
                selected_ab = next((ab for ab in audiobooks if ab["id"] == abs_id), None)
                if not selected_ab:
                    return redirect(url_for("matching.batch_match", search=_redirect_search_value()))

            # Dedup key: audiobook, qualified ebook source, then legacy filename.
            queue_key = abs_id or ebook_source_id or ebook_filename
            if not any(item.get("queue_key") == queue_key for item in session["queue"]):
                is_ebook_only = not abs_id and (ebook_filename or storyteller_uuid)
                is_audio_only = abs_id and not ebook_filename and not storyteller_uuid
                title = (
                    manager.get_audiobook_title(selected_ab)
                    if selected_ab
                    else ebook_display_name or Path(ebook_filename).stem
                    if ebook_filename
                    else "Storyteller Book"
                )
                _ab_meta = (selected_ab or {}).get("media", {}).get("metadata", {})
                session["queue"].append(
                    {
                        "queue_key": queue_key,
                        "abs_id": abs_id,
                        "title": title,
                        "ebook_filename": ebook_filename,
                        "ebook_source_id": ebook_source_id,
                        "ebook_display_name": ebook_display_name,
                        "storyteller_uuid": storyteller_uuid,
                        "storyteller_submit": bool(request.form.get("storyteller_submit")),
                        "duration": manager.get_duration(selected_ab) if selected_ab else 0,
                        "cover_url": abs_service.get_cover_proxy_url(abs_id) if abs_id else None,
                        "audio_only": is_audio_only,
                        "ebook_only": is_ebook_only,
                        "author": get_audiobook_author(selected_ab) if selected_ab else None,
                        "subtitle": _ab_meta.get("subtitle") or None,
                    }
                )
                session.modified = True
            return redirect(url_for("matching.batch_match", search=_redirect_search_value()))
        elif action == "remove_from_queue":
            remove_key = request.form.get("queue_key") or request.form.get("abs_id")
            session["queue"] = [
                item for item in session.get("queue", []) if item.get("queue_key", item.get("abs_id")) != remove_key
            ]
            session.modified = True
            return redirect(url_for("matching.batch_match"))
        elif action == "clear_queue":
            session["queue"] = []
            session.modified = True
            return redirect(url_for("matching.batch_match"))
        elif action == "process_queue":
            failed_items = []
            for item in session.get("queue", []):
                item_label = item.get("ebook_display_name") or item.get("ebook_filename") or item.get("abs_id")
                try:
                    if item.get("audio_only"):
                        intake_service.import_audio_only(
                            abs_id=item["abs_id"],
                            title=item["title"],
                            duration=item["duration"],
                            author=item.get("author"),
                            subtitle=item.get("subtitle"),
                        )
                        continue

                    if item.get("ebook_only"):
                        result = intake_service.import_ebook_only(
                            ebook_filename=item["ebook_filename"],
                            ebook_source_id=item.get("ebook_source_id") or None,
                            ebook_display_name=item.get("ebook_display_name") or "",
                            storyteller_uuid=item.get("storyteller_uuid") or None,
                            storyteller_title=item.get("title", "Storyteller Book"),
                        )
                        if result.error:
                            failed_items.append(item.get("ebook_display_name") or item["ebook_filename"])
                        continue

                    if not item.get("ebook_filename"):
                        failed_items.append(item_label)
                        continue

                    _book, error = _create_book_mapping(
                        container,
                        abs_id=item["abs_id"],
                        title=item["title"],
                        ebook_filename=item["ebook_filename"],
                        ebook_source_id=item.get("ebook_source_id") or None,
                        duration=item["duration"],
                        storyteller_uuid=item.get("storyteller_uuid", ""),
                        storyteller_submit=bool(item.get("storyteller_submit")),
                        author=item.get("author"),
                        subtitle=item.get("subtitle"),
                    )
                    if error:
                        failed_items.append(item.get("ebook_display_name") or item["ebook_filename"])

                except Exception as e:
                    logger.error(f"Failed to process queue item '{sanitize_log_data(item_label)}': {e}")
                    failed_items.append(item_label)

            if failed_items:
                names = ", ".join(failed_items)
                flash(f"Could not compute KOSync ID for: {names}", "warning")
            session["queue"] = []
            session.modified = True
            return redirect(url_for("dashboard.index"))

    # GET request
    search = request.args.get("search", "").strip().lower()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        audiobooks = get_audiobooks_conditionally()
        audiobooks = [ab for ab in audiobooks if audiobook_matches_search(ab, search)]
        for ab in audiobooks:
            ab["cover_url"] = abs_service.get_cover_proxy_url(ab["id"])

        ebooks = get_searchable_ebooks(search)
        ebooks.sort(key=lambda x: x.name.lower())

        if container.storyteller_client().is_configured():
            try:
                storyteller_books = container.storyteller_client().search_books(search)
            except Exception as e:
                logger.warning(f"Storyteller search failed in batch_match route: {e}")

    storyteller_submit_available = False
    try:
        st_sub_svc = container.storyteller_submission_service()
        storyteller_submit_available = st_sub_svc.is_available()
    except Exception as e:
        logger.debug("Storyteller submission availability check failed: %s", e)

    storyteller_force_mode = os.environ.get("STORYTELLER_FORCE_MODE", "false").lower() == "true"
    storyteller_configured = container.storyteller_client().is_configured()

    abs_configured = abs_service.is_available()
    has_ebook_sources = (
        any_grimmory_configured()
        or container.cwa_client().is_configured()
        or abs_service.has_ebook_libraries()
        or get_ebook_dir().exists()
    )

    queue_view = _build_batch_queue_view(session.get("queue", []))
    return render_template(
        "batch_match.html",
        audiobooks=audiobooks,
        ebooks=ebooks,
        storyteller_books=storyteller_books,
        queue=queue_view["items"],
        queue_summary=queue_view,
        search=_escape_template_value(search),
        get_title=manager.get_audiobook_title,
        storyteller_submit_available=storyteller_submit_available,
        storyteller_force_mode=storyteller_force_mode,
        storyteller_configured=storyteller_configured,
        abs_configured=abs_configured,
        has_ebook_sources=has_ebook_sources,
    )
