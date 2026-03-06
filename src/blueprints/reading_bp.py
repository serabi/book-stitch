"""Reading Tab blueprint — reading tracker pages and API endpoints."""

import logging
import math
import time
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from src.blueprints.helpers import get_abs_service, get_booklore_clients, get_database_service
from src.db.models import State

logger = logging.getLogger(__name__)

reading_bp = Blueprint('reading', __name__)


def _synthetic_journal(abs_id, event, date_str, percentage=None):
    """Create a lightweight object mimicking ReadingJournal for timeline display."""
    class _SyntheticJournal:
        def __init__(self):
            self.id = None
            self.abs_id = abs_id
            self.event = event
            self.entry = None
            self.percentage = percentage
            self.created_at = datetime.strptime(date_str, '%Y-%m-%d') if date_str else None
    return _SyntheticJournal()


def _build_book_reading_data(book, database_service, abs_service, states_by_book,
                             booklore_by_filename=None):
    """Build a reading-focused data dict for a single book."""
    sync_mode = getattr(book, 'sync_mode', 'audiobook')
    if sync_mode == 'ebook_only':
        book_type = 'ebook-only'
    elif not book.ebook_filename:
        book_type = 'audio-only'
    else:
        book_type = 'linked'

    # Get unified progress from states
    states = states_by_book.get(book.abs_id, [])
    max_progress = 0
    for state in states:
        if state.percentage:
            max_progress = max(max_progress, round(state.percentage * 100, 1))

    # Cover URL — try ABS proxy first, then KOSync cover, then Booklore cover
    cover_url = None
    if book.abs_id and book_type != 'ebook-only':
        cover_url = abs_service.get_cover_proxy_url(book.abs_id)
    if not cover_url and book.kosync_doc_id:
        cover_url = f'/covers/{book.kosync_doc_id}.jpg'

    # Enrich title from Booklore if it looks like a filename
    display_title = book.abs_title or ''
    bl_meta = None
    if booklore_by_filename:
        for fn in (book.ebook_filename, getattr(book, 'original_ebook_filename', None)):
            if fn:
                candidates = booklore_by_filename.get(fn.lower(), [])
                bl_meta = next((b for b in candidates if b.title), candidates[0] if candidates else None)
                if bl_meta and bl_meta.title:
                    stems = set()
                    for check_fn in (book.ebook_filename, getattr(book, 'original_ebook_filename', None)):
                        if check_fn:
                            stems.add(Path(check_fn).stem)
                    if display_title in stems or display_title == book.ebook_filename:
                        display_title = bl_meta.title
                    break

    # Booklore cover fallback
    if not cover_url and bl_meta:
        bl_id = (bl_meta.raw_metadata_dict or {}).get('id')
        if bl_id:
            cover_url = f"/api/cover-proxy/booklore/{bl_meta.source or 'booklore'}/{bl_id}"

    return {
        'abs_id': book.abs_id,
        'abs_title': display_title,
        'ebook_filename': book.ebook_filename,
        'kosync_doc_id': book.kosync_doc_id,
        'status': book.status,
        'book_type': book_type,
        'unified_progress': min(max_progress, 100.0),
        'cover_url': cover_url,
        'started_at': book.started_at,
        'finished_at': book.finished_at,
        'rating': book.rating,
        'read_count': book.read_count or 1,
    }


def _is_genuinely_reading(book_data):
    """Determine if a book is genuinely being read vs just synced.

    A book with status='active' might just be synced and not truly being read.
    We only count it as "currently reading" if it has meaningful progress (>1%).
    We don't trust started_at alone because ABS/Hardcover auto-set it on first sync.
    """
    if book_data['status'] == 'not_started':
        return False
    if book_data['status'] != 'active':
        return True  # paused/completed/dnf are explicit user actions
    return book_data['unified_progress'] > 1.0


@reading_bp.route('/reading')
def reading_index():
    """Render the main reading tab page."""
    database_service = get_database_service()
    abs_service = get_abs_service()

    books = database_service.get_all_books()

    # Only include books with reading-relevant statuses
    reading_statuses = {'active', 'completed', 'paused', 'dnf', 'not_started'}
    books = [b for b in books if b.status in reading_statuses]

    # Fetch all states at once to avoid N+1
    all_states = database_service.get_all_states()
    states_by_book = {}
    for state in all_states:
        states_by_book.setdefault(state.abs_id, []).append(state)

    # Fetch Booklore metadata for title enrichment
    all_booklore_books = database_service.get_all_booklore_books()
    booklore_by_filename = {}
    for bl_book in all_booklore_books:
        if bl_book.filename:
            booklore_by_filename.setdefault(bl_book.filename.lower(), []).append(bl_book)

    all_book_data = [
        _build_book_reading_data(b, database_service, abs_service, states_by_book, booklore_by_filename)
        for b in books
    ]

    # Classify books and assign display_status
    currently_reading = []
    finished = []
    paused = []
    dnf = []
    not_started = []

    for bd in all_book_data:
        if bd['status'] == 'completed':
            bd['display_status'] = 'finished'
            finished.append(bd)
        elif bd['status'] == 'paused':
            bd['display_status'] = 'paused'
            paused.append(bd)
        elif bd['status'] == 'dnf':
            bd['display_status'] = 'dnf'
            dnf.append(bd)
        elif bd['status'] == 'not_started':
            bd['display_status'] = 'not_started'
            not_started.append(bd)
        elif _is_genuinely_reading(bd):
            bd['display_status'] = 'reading'
            currently_reading.append(bd)
        else:
            bd['display_status'] = 'not_started'
            not_started.append(bd)

    # Sort each section for default ordering
    currently_reading.sort(key=lambda b: b['unified_progress'], reverse=True)
    finished.sort(key=lambda b: b['finished_at'] or '', reverse=True)
    paused.sort(key=lambda b: (b['abs_title'] or '').lower())
    dnf.sort(key=lambda b: (b['abs_title'] or '').lower())
    not_started.sort(key=lambda b: (b['abs_title'] or '').lower())

    # Build flat list in default display order
    all_books = currently_reading + finished + paused + dnf + not_started

    section_counts = {
        'reading': len(currently_reading),
        'finished': len(finished),
        'paused': len(paused),
        'dnf': len(dnf),
        'not_started': len(not_started),
    }

    # Collect unique years from finished books for year dividers
    finished_years = sorted(
        {bd['finished_at'][:4] for bd in finished if bd.get('finished_at')},
        reverse=True,
    )

    current_year = date.today().year
    stats = database_service.get_reading_stats(current_year)
    goal = database_service.get_reading_goal(current_year)

    return render_template(
        'reading.html',
        all_books=all_books,
        section_counts=section_counts,
        finished_years=finished_years,
        stats=stats,
        goal=goal,
        current_year=current_year,
        total_books=len(all_book_data),
    )


@reading_bp.route('/reading/book/<abs_id>')
def reading_detail(abs_id):
    """Render the book detail view with journal."""
    database_service = get_database_service()
    abs_service = get_abs_service()

    book = database_service.get_book(abs_id)
    if not book:
        return render_template('reading.html', all_books=[], section_counts={},
                               finished_years=[], stats={}, goal=None,
                               current_year=date.today().year, total_books=0), 404

    all_states = database_service.get_all_states()
    states_by_book = {}
    for state in all_states:
        states_by_book.setdefault(state.abs_id, []).append(state)

    # Booklore enrichment
    all_booklore_books = database_service.get_all_booklore_books()
    booklore_by_filename = {}
    for bl_book in all_booklore_books:
        if bl_book.filename:
            booklore_by_filename.setdefault(bl_book.filename.lower(), []).append(bl_book)

    book_data = _build_book_reading_data(book, database_service, abs_service, states_by_book,
                                         booklore_by_filename)
    journals = database_service.get_reading_journals(abs_id)

    # Synthesize started/finished timeline entries from book dates if missing
    existing_events = {j.event for j in journals}
    synthetic = []
    if book.started_at and 'started' not in existing_events:
        synthetic.append(_synthetic_journal(abs_id, 'started', book.started_at))
    if book.finished_at and 'finished' not in existing_events:
        synthetic.append(_synthetic_journal(abs_id, 'finished', book.finished_at, percentage=1.0))
    if synthetic:
        journals = list(journals) + synthetic
        journals.sort(key=lambda j: j.created_at or datetime.min, reverse=True)

    # BookFusion highlights matched to this book
    bf_highlights = database_service.get_bookfusion_highlights_for_book(abs_id)

    has_bookfusion_link = (
        abs_id.startswith('bf-')
        or len(bf_highlights) > 0
        or database_service.is_bookfusion_linked(abs_id)
    )

    # ── Build enriched metadata from all linked services ──
    metadata = {}

    # ABS metadata (subtitle, author, narrator, duration, genres, description)
    sync_mode = getattr(book, 'sync_mode', 'audiobook')
    if sync_mode != 'ebook_only':
        try:
            abs_item = abs_service.get_item_details(abs_id)
            if abs_item:
                abs_meta = abs_item.get('media', {}).get('metadata', {})
                metadata['author'] = abs_meta.get('authorName') or ''
                metadata['narrator'] = abs_meta.get('narratorName') or ''
                metadata['subtitle'] = abs_meta.get('subtitle') or ''
                metadata['description'] = abs_meta.get('description') or ''
                metadata['genres'] = abs_meta.get('genres') or []
                duration = abs_item.get('media', {}).get('duration')
                if duration:
                    hrs = int(duration // 3600)
                    mins = int((duration % 3600) // 60)
                    metadata['duration'] = f"{hrs}h {mins}m" if hrs else f"{mins}m"
        except Exception as e:
            logger.debug("abs_service.get_item_details failed for abs_id=%s: %s", abs_id, e, exc_info=True)

    # Fallback duration from stored book data (in case ABS API call failed or was skipped)
    if not metadata.get('duration') and book.duration and book.duration > 0:
        hrs = int(book.duration // 3600)
        mins = int((book.duration % 3600) // 60)
        metadata['duration'] = f"{hrs}h {mins}m" if hrs else f"{mins}m"

    # Hardcover details (ISBN, ASIN, pages, slug)
    hardcover = database_service.get_hardcover_details(abs_id)
    if hardcover:
        metadata['isbn'] = hardcover.isbn
        metadata['asin'] = hardcover.asin
        if hardcover.hardcover_pages and hardcover.hardcover_pages > 0:
            metadata['pages'] = hardcover.hardcover_pages
        metadata['hardcover_slug'] = hardcover.hardcover_slug
        metadata['hardcover_url'] = (
            f"https://hardcover.app/books/{hardcover.hardcover_slug}"
            if hardcover.hardcover_slug
            else None
        )

    # Booklore metadata (description, publisher, language)
    if book.ebook_filename:
        for bl_client in get_booklore_clients():
            try:
                if not bl_client.is_configured():
                    continue
                bl_book = bl_client.find_book_by_filename(book.ebook_filename, allow_refresh=False)
                if not bl_book and getattr(book, 'original_ebook_filename', None):
                    bl_book = bl_client.find_book_by_filename(book.original_ebook_filename, allow_refresh=False)
                if bl_book:
                    if not metadata.get('description') and bl_book.get('description'):
                        metadata['description'] = bl_book['description']
                    bl_url = f"{bl_client.base_url}/book/{bl_book.get('id')}?tab=view"
                    metadata['booklore_url'] = bl_url
                    break
            except Exception as e:
                logger.debug("Booklore lookup failed for ebook_filename=%s, original=%s, client=%s: %s",
                             book.ebook_filename, getattr(book, 'original_ebook_filename', None),
                             bl_client.base_url, e)
                continue

    # BookFusion catalog entry (tags, series)
    bf_book = database_service.get_bookfusion_book_by_abs_id(abs_id)
    if bf_book:
        metadata['bf_tags'] = bf_book.tags or ''
        metadata['bf_series'] = bf_book.series or ''

    # ABS item URL
    if sync_mode != 'ebook_only':
        metadata['abs_url'] = abs_service.get_abs_item_url(abs_id)

    return render_template(
        'reading_detail.html',
        book=book_data,
        journals=journals,
        bf_highlights=bf_highlights,
        has_bookfusion_link=has_bookfusion_link,
        metadata=metadata,
    )


# ─── API Endpoints ───────────────────────────────────────────────────


@reading_bp.route('/api/reading/book/<abs_id>/rating', methods=['POST'])
def update_rating(abs_id):
    """Set or update the rating for a book."""
    database_service = get_database_service()
    data = request.json or {}
    rating = data.get('rating')

    if rating is not None:
        try:
            rating = float(rating)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Invalid rating value"}), 400
        if not math.isfinite(rating) or rating < 0 or rating > 5:
            return jsonify({"success": False, "error": "Rating must be between 0 and 5"}), 400

    book = database_service.update_book_reading_fields(abs_id, rating=rating)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    return jsonify({"success": True, "rating": book.rating})


@reading_bp.route('/api/reading/book/<abs_id>/progress', methods=['POST'])
def update_progress(abs_id):
    """Manually set reading progress for a book (e.g. BookFusion books without auto-sync)."""
    database_service = get_database_service()
    data = request.json or {}
    percentage = data.get('percentage')

    if percentage is None:
        return jsonify({"success": False, "error": "percentage is required"}), 400

    try:
        percentage = float(percentage)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid percentage value"}), 400

    if not math.isfinite(percentage) or percentage < 0 or percentage > 1:
        return jsonify({"success": False, "error": "percentage must be between 0 and 1"}), 400

    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    # Mark book as active if it hasn't been started yet
    if percentage > 0 and book.status not in ('active', 'paused', 'dnf', 'completed'):
        book.status = 'active'
        if not book.started_at:
            book.started_at = date.today().isoformat()
        database_service.save_book(book)

    state = State(
        abs_id=abs_id,
        client_name='manual',
        percentage=percentage,
        last_updated=time.time(),
        timestamp=time.time(),
    )
    database_service.save_state(state)

    # Trigger sync to propagate progress to other linked services
    try:
        from src.blueprints.helpers import get_container
        from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
        container = get_container()
        sync_clients = container.sync_clients()
        locator = LocatorResult(percentage=percentage)
        req = UpdateProgressRequest(locator_result=locator)
        for client_name, client in sync_clients.items():
            if client.is_configured():
                try:
                    client.update_progress(book, req)
                except Exception as e:
                    logger.debug(f"Progress sync to {client_name} failed: {e}")
    except Exception as e:
        logger.debug(f"Could not propagate progress: {e}")

    return jsonify({"success": True, "percentage": percentage})


@reading_bp.route('/api/reading/book/<abs_id>/dates', methods=['POST'])
def update_dates(abs_id):
    """Update started_at and/or finished_at dates."""
    database_service = get_database_service()
    data = request.json or {}
    updates = {}

    for field in ('started_at', 'finished_at'):
        if field in data:
            val = data[field]
            if val:
                try:
                    datetime.strptime(val, '%Y-%m-%d')
                except ValueError:
                    return jsonify({"success": False, "error": f"Invalid date format for {field}"}), 400
            updates[field] = val or None

    if not updates:
        return jsonify({"success": False, "error": "No date fields provided"}), 400

    # Cross-validate against existing DB values when only one date is provided
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    effective_started = updates.get('started_at') or (book.started_at if 'started_at' not in updates else None)
    effective_finished = updates.get('finished_at') or (book.finished_at if 'finished_at' not in updates else None)
    if effective_started and effective_finished and effective_started > effective_finished:
        return jsonify({"success": False, "error": "started_at cannot be after finished_at"}), 400

    book = database_service.update_book_reading_fields(abs_id, **updates)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    return jsonify({"success": True, "started_at": book.started_at, "finished_at": book.finished_at})


@reading_bp.route('/api/reading/book/<abs_id>/journal', methods=['POST'])
def add_journal(abs_id):
    """Add a journal note for a book."""
    database_service = get_database_service()
    data = request.json or {}
    entry = (data.get('entry') or '').strip()

    if not entry:
        return jsonify({"success": False, "error": "Entry text is required"}), 400

    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    # Get current progress for the journal entry
    book_states = database_service.get_states_for_book(abs_id)
    max_pct = 0
    for state in book_states:
        if state.percentage:
            max_pct = max(max_pct, state.percentage)

    journal = database_service.add_reading_journal(
        abs_id, event='note', entry=entry, percentage=max_pct if max_pct > 0 else None
    )

    return jsonify({
        "success": True,
        "journal": {
            "id": journal.id,
            "event": journal.event,
            "entry": journal.entry,
            "percentage": journal.percentage,
            "created_at": journal.created_at.isoformat() if journal.created_at else None,
        }
    })


@reading_bp.route('/api/reading/journal/<int:journal_id>', methods=['DELETE'])
def delete_journal(journal_id):
    """Delete a journal entry."""
    database_service = get_database_service()
    deleted = database_service.delete_reading_journal(journal_id)
    if not deleted:
        return jsonify({"success": False, "error": "Journal entry not found"}), 404
    return jsonify({"success": True})


@reading_bp.route('/api/reading/goal/<int:year>', methods=['GET'])
def get_goal(year):
    """Get the reading goal for a given year."""
    database_service = get_database_service()
    stats = database_service.get_reading_stats(year)
    goal = database_service.get_reading_goal(year)

    return jsonify({
        "year": year,
        "target": goal.target_books if goal else None,
        "completed": stats['books_finished'],
    })


@reading_bp.route('/api/reading/goal/<int:year>', methods=['POST'])
def set_goal(year):
    """Set or update the yearly reading goal."""
    database_service = get_database_service()
    data = request.json or {}
    target = data.get('target_books')

    if target is None:
        return jsonify({"success": False, "error": "target_books is required"}), 400

    try:
        target = int(target)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "target_books must be an integer"}), 400

    if target < 1:
        return jsonify({"success": False, "error": "target_books must be at least 1"}), 400

    goal = database_service.save_reading_goal(year, target)
    return jsonify({"success": True, "year": goal.year, "target_books": goal.target_books})
