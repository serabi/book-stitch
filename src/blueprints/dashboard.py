"""Dashboard blueprint — GET / (index)."""

import logging
import os
import threading
import time
from pathlib import Path

from flask import Blueprint, render_template

from src.blueprints.helpers import (
    find_grimmory_metadata,
    get_abs_service,
    get_container,
    get_database_service,
    get_enabled_grimmory_server_ids,
)
from src.utils.cover_resolver import resolve_book_covers
from src.utils.service_url_helper import get_hardcover_book_url, get_service_web_url
from src.version import APP_VERSION

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    """
    Render the dashboard with enriched book and progress data.

    Loads books, listening states, hardcover and Grimmory metadata, and integration statuses, then renders the dashboard page with per-book mappings, overall progress, app version and update information.

    Returns:
        Rendered template response for the dashboard page.
    """
    container = get_container()
    database_service = get_database_service()

    books = database_service.get_all_books()

    # Fire date sync in a background thread so it doesn't block page render.
    # Results will be visible on the next dashboard load.
    _THROTTLE_KEY = "dashboard_date_sync_last_run"
    _THROTTLE_SECONDS = 300
    last_run_raw = database_service.get_setting(_THROTTLE_KEY)
    try:
        last_run = float(last_run_raw) if last_run_raw else 0.0
    except (TypeError, ValueError):
        last_run = 0.0

    if (time.time() - last_run) >= _THROTTLE_SECONDS:
        database_service.set_setting(_THROTTLE_KEY, str(time.time()))

        def _run_date_sync():
            try:
                rds = container.reading_date_service()
                ac_stats = rds.auto_complete_finished_books(container)
                if ac_stats["completed"]:
                    logger.info(f"Auto-completed {ac_stats['completed']} book(s) at 100% progress")
            except Exception:
                logger.exception("Background auto-complete failed")

        threading.Thread(target=_run_date_sync, daemon=True).start()

    abs_service = get_abs_service()

    # Fetch ABS metadata once for the whole dashboard (single API call)
    abs_metadata_by_id = {}
    try:
        all_abs_books = abs_service.get_audiobooks()
        for ab in all_abs_books:
            ab_id = ab.get("id")
            if ab_id:
                metadata = ab.get("media", {}).get("metadata", {})
                abs_metadata_by_id[ab_id] = {
                    "subtitle": metadata.get("subtitle") or "",
                    "author": metadata.get("authorName") or "",
                }
    except Exception as e:
        logger.warning(f"Could not fetch ABS metadata for dashboard enrichment: {e}")

    # Fetch all states at once to avoid N+1 queries
    states_by_book = database_service.get_states_by_book()

    # Fetch all hardcover details at once
    all_hardcover = database_service.get_all_hardcover_details()
    hardcover_by_book = {h.book_id: h for h in all_hardcover}

    # Fetch Grimmory metadata for ebook-only title/author enrichment
    enabled_bl_ids = get_enabled_grimmory_server_ids()
    grimmory_by_filename = database_service.get_grimmory_by_filename(enabled_server_ids=enabled_bl_ids)
    grimmory_by_filename_all = database_service.get_grimmory_by_filename()  # unfiltered fallback

    integrations = {}
    sync_clients = container.sync_clients()
    for client_name, client in sync_clients.items():
        integrations[client_name.lower()] = client.is_configured()

    # Merge Grimmory 2 into grimmory flag so templates show the service
    # when either instance is configured
    if integrations.get("grimmory2") and not integrations.get("grimmory"):
        integrations["grimmory"] = True

    # BookFusion integration status
    bf_client = container.bookfusion_client()
    integrations["bookfusion"] = bf_client.is_configured()

    # Bulk-fetch BookFusion link data (avoid N+1)
    # Always load persisted state so books show links/highlights even when BF client is unconfigured
    bf_linked_ids = set()
    bf_highlight_counts = {}
    try:
        bf_linked_ids = database_service.get_bookfusion_linked_book_ids()
        bf_highlight_counts = database_service.get_bookfusion_highlight_counts_by_book_id()
    except Exception as e:
        logger.warning(f"Could not fetch BookFusion link data: {e}")

    # Bulk-fetch Storyteller submission statuses (avoid N+1)
    st_submissions_by_book = {}
    if integrations.get("storyteller"):
        try:
            st_submissions_by_book = database_service.get_all_storyteller_submissions_latest()
        except Exception as e:
            logger.warning(f"Could not fetch Storyteller submissions: {e}")

    # Bulk-fetch latest jobs for books in pending/processing/retry states (avoid N+1)
    job_status_book_ids = [b.id for b in books if b.status in ("pending", "processing", "failed_retry_later")]
    jobs_by_book = database_service.get_latest_jobs_bulk(job_status_book_ids)

    mappings = []
    books_needing_title_save = []
    total_duration = 0
    total_listened = 0

    for book in books:
        states = states_by_book.get(book.id, [])
        state_by_client = {state.client_name: state for state in states}

        sync_mode = book.sync_mode
        if sync_mode == "ebook_only":
            book_type = "ebook-only"
        elif not book.ebook_filename:
            book_type = "audio-only"
        else:
            book_type = "linked"

        # bl_meta: filtered to enabled instances (used for covers/deep-links)
        bl_meta = find_grimmory_metadata(book, grimmory_by_filename)

        # bl_meta_enrichment: unfiltered fallback for title/author (stale metadata is fine)
        bl_meta_enrichment = bl_meta or find_grimmory_metadata(book, grimmory_by_filename_all)

        # Skip ABS metadata enrichment for ebook-only books (synthetic ID won't resolve)
        if book_type == "ebook-only":
            abs_subtitle = ""
            abs_author = (bl_meta_enrichment.authors or "") if bl_meta_enrichment else ""
        else:
            _abs_meta = abs_metadata_by_id.get(book.abs_id, {})
            abs_subtitle = _abs_meta.get("subtitle", "") or book.subtitle or ""
            abs_author = _abs_meta.get("author", "") or book.author or ""

        # Enrich title from Grimmory if stored title looks like a filename
        enriched_title = book.title
        if bl_meta_enrichment and bl_meta_enrichment.title:
            stems = set()
            for fn in (book.ebook_filename, book.original_ebook_filename):
                if fn:
                    stems.add(Path(fn).stem)
            if book.title in stems or book.title in (
                book.ebook_filename,
                book.original_ebook_filename,
            ):
                enriched_title = bl_meta_enrichment.title
                # Persist the improved title so it sticks (batched after loop)
                book.title = bl_meta_enrichment.title
                books_needing_title_save.append(book)

        # Opportunistic refresh: cache author/subtitle from live ABS data
        if book_type != "ebook-only" and book.abs_id in abs_metadata_by_id:
            _live = abs_metadata_by_id[book.abs_id]
            _live_author = _live.get("author", "")
            _live_subtitle = _live.get("subtitle", "")
            if _live_author and _live_author != book.author:
                book.author = _live_author
                if book not in books_needing_title_save:
                    books_needing_title_save.append(book)
            if _live_subtitle and _live_subtitle != book.subtitle:
                book.subtitle = _live_subtitle
                if book not in books_needing_title_save:
                    books_needing_title_save.append(book)

        display_title = book.title_override or enriched_title
        display_author = book.author_override or abs_author

        mapping = {
            "id": book.id,
            "abs_id": book.abs_id,
            "title": display_title,
            "abs_subtitle": abs_subtitle,
            "abs_author": display_author,
            "ebook_filename": book.ebook_filename,
            "kosync_doc_id": book.kosync_doc_id,
            "transcript_file": book.transcript_file,
            "status": book.status,
            "sync_mode": sync_mode,
            "book_type": book_type,
            "activity_flag": book.activity_flag,
            "unified_progress": 0,
            "duration": book.duration or 0,
            "storyteller_uuid": book.storyteller_uuid,
            "finished_at": book.finished_at,
            "storyteller_submission_status": None,
            "states": {},
        }

        # Storyteller submission status (from bulk-fetched dict)
        st_submission = st_submissions_by_book.get(book.id)
        if st_submission:
            mapping["storyteller_submission_status"] = st_submission.status

        if book.status in ("pending", "processing", "failed_retry_later"):
            job = jobs_by_book.get(book.id)
            if job:
                mapping["job_progress"] = round((job.progress or 0.0) * 100, 1)
                mapping["retry_count"] = job.retry_count or 0
            else:
                mapping["job_progress"] = 0.0
                mapping["retry_count"] = 0

        latest_update_time = 0
        max_progress = 0

        for client_name, state in state_by_client.items():
            if state.last_updated and state.last_updated > latest_update_time:
                latest_update_time = state.last_updated

            mapping["states"][client_name] = {
                "timestamp": state.timestamp or 0,
                "percentage": round(state.percentage * 100, 1) if state.percentage else 0,
                "last_updated": state.last_updated,
            }

            if state.percentage:
                progress_pct = round(state.percentage * 100, 1)
                max_progress = max(max_progress, progress_pct)

        # Hardcover details
        hardcover_details = hardcover_by_book.get(book.id)
        if hardcover_details:
            # HC out-of-sync indicator: cached HC status ≠ local status means push pending/failed
            hc_to_local = {1: "not_started", 2: "active", 3: "completed", 4: "paused", 5: "dnf"}
            hc_local_equiv = hc_to_local.get(hardcover_details.hardcover_status_id)
            hc_mismatch = (
                hc_local_equiv is not None
                and book.status in ("not_started", "active", "paused", "dnf", "completed")
                and hc_local_equiv != book.status
            )
            mapping.update(
                {
                    "hardcover_book_id": hardcover_details.hardcover_book_id,
                    "hardcover_slug": hardcover_details.hardcover_slug,
                    "hardcover_edition_id": hardcover_details.hardcover_edition_id,
                    "hardcover_pages": hardcover_details.hardcover_pages,
                    "isbn": hardcover_details.isbn,
                    "asin": hardcover_details.asin,
                    "matched_by": hardcover_details.matched_by,
                    "hardcover_linked": True,
                    "hardcover_title": display_title,
                    "hardcover_cover_url": hardcover_details.hardcover_cover_url,
                    "hardcover_status_mismatch": hc_mismatch,
                }
            )
        else:
            mapping.update(
                {
                    "hardcover_book_id": None,
                    "hardcover_slug": None,
                    "hardcover_edition_id": None,
                    "hardcover_pages": None,
                    "isbn": None,
                    "asin": None,
                    "matched_by": None,
                    "hardcover_linked": False,
                    "hardcover_title": None,
                }
            )

        # Legacy Storyteller link check
        has_storyteller_state = "storyteller" in state_by_client
        is_legacy_link = has_storyteller_state and not book.storyteller_uuid
        mapping["storyteller_legacy_link"] = is_legacy_link

        # Platform deep links
        if book_type != "ebook-only" and book.abs_id and not book.abs_id.startswith("bf-"):
            abs_base = get_service_web_url("ABS") or (
                abs_service.abs_client.base_url if abs_service.is_available() else ""
            )
            mapping["abs_url"] = f"{abs_base}/item/{book.abs_id}" if abs_base else None
        else:
            mapping["abs_url"] = None

        # Grimmory deep links (from pre-built lookup — avoids per-book fuzzy matching)
        mapping["grimmory_id"] = None
        mapping["grimmory_url"] = None
        bl_id = bl_meta.raw_metadata_dict.get("id") if bl_meta else None
        if bl_id:
            mapping["grimmory_id"] = bl_id
            bl_prefix = f"GRIMMORY{'_2' if bl_meta.server_id == '2' else ''}"
            bl_web = get_service_web_url(bl_prefix)
            mapping["grimmory_url"] = f"{bl_web}/book/{bl_id}?tab=view" if bl_web else None

        if mapping.get("hardcover_slug"):
            mapping["hardcover_url"] = get_hardcover_book_url(mapping["hardcover_slug"])
        elif mapping.get("hardcover_book_id"):
            mapping["hardcover_url"] = get_hardcover_book_url(mapping["hardcover_book_id"])
        else:
            mapping["hardcover_url"] = None

        # BookFusion link data
        is_bf_linked = (book.id in bf_linked_ids) or (book.abs_id or "").startswith("bf-")
        mapping["bookfusion_linked"] = is_bf_linked
        mapping["bookfusion_highlight_count"] = bf_highlight_counts.get(book.id, 0)

        mapping["unified_progress"] = min(max_progress, 100.0)
        mapping["latest_activity_at"] = latest_update_time or None

        if latest_update_time > 0:
            diff = time.time() - latest_update_time
            if diff < 60:
                mapping["last_sync"] = f"{int(diff)}s ago"
            elif diff < 3600:
                mapping["last_sync"] = f"{int(diff // 60)}m ago"
            else:
                mapping["last_sync"] = f"{int(diff // 3600)}h ago"
        else:
            mapping["last_sync"] = "Never"

        covers = resolve_book_covers(
            book,
            abs_service,
            database_service,
            book_type,
            grimmory_meta=bl_meta,
            hardcover_details=hardcover_details,
        )
        mapping["cover_url"] = covers["cover_url"]
        mapping["placeholder_logo"] = covers["placeholder_logo"]

        duration = mapping.get("duration", 0)
        progress_pct = mapping.get("unified_progress", 0)

        if duration > 0:
            total_duration += duration
            total_listened += (progress_pct / 100.0) * duration

        mappings.append(mapping)

    # Batch-save books that had their titles enriched from Grimmory
    for book in books_needing_title_save:
        database_service.save_book(book)

    if total_duration > 0:
        overall_progress = round((total_listened / total_duration) * 100, 1)
    elif mappings:
        overall_progress = round(sum(m["unified_progress"] for m in mappings) / len(mappings), 1)
    else:
        overall_progress = 0

    grimmory_label = os.environ.get("GRIMMORY_LABEL", "Grimmory") or "Grimmory"

    # Unlinked KoSync documents — for dashboard toast + pending identification section
    kosync_unlinked_count = 0
    unlinked_reading = []
    kosync_active = os.environ.get("KOSYNC_ENABLED", "").lower() in ("true", "1", "yes", "on") or os.environ.get(
        "KOSYNC_SERVER", ""
    )
    if kosync_active:
        try:
            unlinked_docs = database_service.get_unlinked_kosync_documents()
            kosync_unlinked_count = len(unlinked_docs)
            unlinked_reading = [
                {
                    "document_hash": doc.document_hash,
                    "percentage": float(doc.percentage) if doc.percentage else 0,
                    "device": doc.device,
                    "last_updated": doc.last_updated.isoformat() if doc.last_updated else None,
                }
                for doc in unlinked_docs
                if doc.percentage and float(doc.percentage) > 0
            ]
        except Exception:
            pass

    # Compact preview only; the Pairings page owns the resolution workflow.
    detected_books = []
    try:
        active_detected = database_service.get_active_detected_books(limit=3)
        for d in active_detected:
            detected_books.append(
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
                    "matches": d.matches,
                    "device": d.device,
                    "ebook_filename": d.ebook_filename,
                }
            )
    except Exception:
        pass

    return render_template(
        "index.html",
        mappings=mappings,
        integrations=integrations,
        progress=overall_progress,
        app_version=APP_VERSION,
        grimmory_label=grimmory_label,
        kosync_unlinked_count=kosync_unlinked_count,
        unlinked_reading=unlinked_reading,
        detected_books=detected_books,
    )
