"""Dashboard blueprint — GET / (index)."""

import logging
import os
import time
from pathlib import Path

from flask import Blueprint, render_template

from src.blueprints.helpers import get_abs_service, get_booklore_clients, get_container, get_database_service
from src.version import APP_VERSION, get_update_status

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
def index():
    """
    Render the dashboard with enriched book and progress data.

    Loads books, listening states, pending suggestions, hardcover and Booklore metadata, and integration statuses, then renders the dashboard page with per-book mappings, overall progress, suggestions, app version and update information.

    Returns:
        Rendered template response for the dashboard page.
    """
    container = get_container()
    database_service = get_database_service()

    books = database_service.get_all_books()

    abs_service = get_abs_service()

    # Fetch ABS metadata once for the whole dashboard (single API call)
    abs_metadata_by_id = {}
    try:
        all_abs_books = abs_service.get_audiobooks()
        for ab in all_abs_books:
            ab_id = ab.get('id')
            if ab_id:
                metadata = ab.get('media', {}).get('metadata', {})
                abs_metadata_by_id[ab_id] = {
                    'subtitle': metadata.get('subtitle') or '',
                    'author': metadata.get('authorName') or '',
                }
    except Exception as e:
        logger.warning(f"Could not fetch ABS metadata for dashboard enrichment: {e}")

    # Fetch all states at once to avoid N+1 queries
    all_states = database_service.get_all_states()
    states_by_book = {}
    for state in all_states:
        if state.abs_id not in states_by_book:
            states_by_book[state.abs_id] = []
        states_by_book[state.abs_id].append(state)

    # Fetch pending suggestions
    suggestions_raw = database_service.get_all_pending_suggestions()
    suggestions = [s for s in suggestions_raw if len(s.matches) > 0]

    # Fetch all hardcover details at once
    all_hardcover = database_service.get_all_hardcover_details()
    hardcover_by_book = {h.abs_id: h for h in all_hardcover}

    # Fetch Booklore metadata for ebook-only title/author enrichment
    # Collect a list per filename so dual instances don't overwrite each other
    all_booklore_books = database_service.get_all_booklore_books()
    booklore_by_filename = {}
    for bl_book in all_booklore_books:
        if not bl_book.filename:
            continue
        booklore_by_filename.setdefault(bl_book.filename.lower(), []).append(bl_book)

    integrations = {}
    sync_clients = container.sync_clients()
    for client_name, client in sync_clients.items():
        integrations[client_name.lower()] = client.is_configured()

    mappings = []
    total_duration = 0
    total_listened = 0

    for book in books:
        states = states_by_book.get(book.abs_id, [])
        state_by_client = {state.client_name: state for state in states}

        sync_mode = getattr(book, 'sync_mode', 'audiobook')
        if sync_mode == 'ebook_only':
            book_type = 'ebook-only'
        elif not book.ebook_filename:
            book_type = 'audio-only'
        else:
            book_type = 'linked'

        # Look up Booklore metadata by ebook_filename or original_ebook_filename
        # Prefer entries that have a title, since we use this for display enrichment
        bl_meta = None
        for fn in (book.ebook_filename, getattr(book, 'original_ebook_filename', None)):
            if fn:
                candidates = booklore_by_filename.get(fn.lower(), [])
                bl_meta = next((b for b in candidates if b.title), candidates[0] if candidates else None)
                if bl_meta:
                    break

        # Skip ABS metadata enrichment for ebook-only books (synthetic ID won't resolve)
        if book_type == 'ebook-only':
            abs_subtitle = ''
            abs_author = (bl_meta.authors or '') if bl_meta else ''
        else:
            _abs_meta = abs_metadata_by_id.get(book.abs_id, {})
            abs_subtitle = _abs_meta.get('subtitle', '')
            abs_author = _abs_meta.get('author', '')

        # Enrich title from Booklore if stored title looks like a filename
        enriched_title = book.abs_title
        if bl_meta and bl_meta.title:
            stems = set()
            for fn in (book.ebook_filename, getattr(book, 'original_ebook_filename', None)):
                if fn:
                    stems.add(Path(fn).stem)
            if book.abs_title in stems or book.abs_title in (book.ebook_filename, getattr(book, 'original_ebook_filename', None)):
                enriched_title = bl_meta.title
                # Persist the improved title so it sticks
                book.abs_title = bl_meta.title
                database_service.save_book(book)

        mapping = {
            'abs_id': book.abs_id,
            'abs_title': enriched_title,
            'abs_subtitle': abs_subtitle,
            'abs_author': abs_author,
            'ebook_filename': book.ebook_filename,
            'kosync_doc_id': book.kosync_doc_id,
            'transcript_file': book.transcript_file,
            'status': book.status,
            'sync_mode': sync_mode,
            'book_type': book_type,
            'unified_progress': 0,
            'duration': book.duration or 0,
            'storyteller_uuid': book.storyteller_uuid,
            'states': {}
        }

        if book.status == 'processing':
            job = database_service.get_latest_job(book.abs_id)
            if job:
                mapping['job_progress'] = round((job.progress or 0.0) * 100, 1)
            else:
                mapping['job_progress'] = 0.0

        latest_update_time = 0
        max_progress = 0

        for client_name, state in state_by_client.items():
            if state.last_updated and state.last_updated > latest_update_time:
                latest_update_time = state.last_updated

            mapping['states'][client_name] = {
                'timestamp': state.timestamp or 0,
                'percentage': round(state.percentage * 100, 1) if state.percentage else 0,
                'last_updated': state.last_updated
            }

            if state.percentage:
                progress_pct = round(state.percentage * 100, 1)
                max_progress = max(max_progress, progress_pct)

        # Hardcover details
        hardcover_details = hardcover_by_book.get(book.abs_id)
        if hardcover_details:
            mapping.update({
                'hardcover_book_id': hardcover_details.hardcover_book_id,
                'hardcover_slug': hardcover_details.hardcover_slug,
                'hardcover_edition_id': hardcover_details.hardcover_edition_id,
                'hardcover_pages': hardcover_details.hardcover_pages,
                'isbn': hardcover_details.isbn,
                'asin': hardcover_details.asin,
                'matched_by': hardcover_details.matched_by,
                'hardcover_linked': True,
                'hardcover_title': book.abs_title
            })
        else:
            mapping.update({
                'hardcover_book_id': None,
                'hardcover_slug': None,
                'hardcover_edition_id': None,
                'hardcover_pages': None,
                'isbn': None,
                'asin': None,
                'matched_by': None,
                'hardcover_linked': False,
                'hardcover_title': None
            })

        # Legacy Storyteller link check
        has_storyteller_state = 'storyteller' in state_by_client
        is_legacy_link = has_storyteller_state and not book.storyteller_uuid
        mapping['storyteller_legacy_link'] = is_legacy_link

        # Platform deep links
        if book_type != 'ebook-only':
            mapping['abs_url'] = abs_service.get_abs_item_url(book.abs_id)
        else:
            mapping['abs_url'] = None

        # Booklore deep links (check all instances)
        mapping['booklore_id'] = None
        mapping['booklore_url'] = None
        mapping['booklore_2_url'] = None
        if book.ebook_filename:
            for bl_client in get_booklore_clients():
                try:
                    if not bl_client.is_configured():
                        continue
                    bl_book = bl_client.find_book_by_filename(book.ebook_filename, allow_refresh=False)
                    if not bl_book and book.original_ebook_filename:
                        bl_book = bl_client.find_book_by_filename(book.original_ebook_filename, allow_refresh=False)
                    if bl_book:
                        url = f"{bl_client.base_url}/book/{bl_book.get('id')}?tab=view"
                        if bl_client.source_tag == 'booklore':
                            mapping['booklore_id'] = bl_book.get('id')
                            mapping['booklore_url'] = url
                        else:
                            mapping['booklore_2_url'] = url
                except Exception:
                    logger.debug(f"Booklore lookup failed for '{getattr(bl_client, 'source_tag', '?')}', skipping")
                    continue

        if mapping.get('hardcover_slug'):
            mapping['hardcover_url'] = f"https://hardcover.app/books/{mapping['hardcover_slug']}"
        elif mapping.get('hardcover_book_id'):
            mapping['hardcover_url'] = f"https://hardcover.app/books/{mapping['hardcover_book_id']}"
        else:
            mapping['hardcover_url'] = None

        mapping['unified_progress'] = min(max_progress, 100.0)

        if latest_update_time > 0:
            diff = time.time() - latest_update_time
            if diff < 60:
                mapping['last_sync'] = f"{int(diff)}s ago"
            elif diff < 3600:
                mapping['last_sync'] = f"{int(diff // 60)}m ago"
            else:
                mapping['last_sync'] = f"{int(diff // 3600)}h ago"
        else:
            mapping['last_sync'] = "Never"

        if book.abs_id and book_type != 'ebook-only':
            mapping['cover_url'] = abs_service.get_cover_proxy_url(book.abs_id)
        else:
            mapping['cover_url'] = None

        duration = mapping.get('duration', 0)
        progress_pct = mapping.get('unified_progress', 0)

        if duration > 0:
            total_duration += duration
            total_listened += (progress_pct / 100.0) * duration

        mappings.append(mapping)

    if total_duration > 0:
        overall_progress = round((total_listened / total_duration) * 100, 1)
    elif mappings:
        overall_progress = round(sum(m['unified_progress'] for m in mappings) / len(mappings), 1)
    else:
        overall_progress = 0

    latest_version, update_available = get_update_status()

    booklore_label = os.environ.get('BOOKLORE_LABEL', 'Booklore') or 'Booklore'
    booklore_2_label = os.environ.get('BOOKLORE_2_LABEL', 'Booklore 2') or 'Booklore 2'

    return render_template(
        'index.html',
        mappings=mappings,
        integrations=integrations,
        progress=overall_progress,
        suggestions=suggestions,
        app_version=APP_VERSION,
        update_available=update_available,
        latest_version=latest_version,
        booklore_label=booklore_label,
        booklore_2_label=booklore_2_label
    )
