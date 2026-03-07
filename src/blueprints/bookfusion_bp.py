"""BookFusion blueprint — upload books and sync highlights."""

import difflib
import logging
import re
from collections import defaultdict
from datetime import datetime

from flask import Blueprint, current_app, jsonify, render_template, request

from src.blueprints.helpers import get_booklore_client, get_container, get_database_service
from src.db.models import Book, HardcoverDetails

logger = logging.getLogger(__name__)

bookfusion_bp = Blueprint('bookfusion', __name__)

SUPPORTED_FORMATS = {'.epub', '.mobi', '.azw3', '.pdf', '.azw', '.fb2', '.cbz', '.cbr'}


def _is_supported(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in SUPPORTED_FORMATS)


@bookfusion_bp.route('/bookfusion')
def bookfusion_page():
    return render_template('bookfusion.html')


@bookfusion_bp.route('/api/bookfusion/booklore-books')
def booklore_books():
    """List Booklore books for upload selection, filtered by supported formats."""
    q = request.args.get('q', '').strip()
    results = []

    client = get_booklore_client()
    if client.is_configured():
        try:
            label = current_app.config.get("BOOKLORE_LABEL", "Booklore")
            books = client.search_books(q) if q else client.get_all_books()
            for b in (books or []):
                fname = b.get('fileName', '')
                if not _is_supported(fname):
                    continue
                results.append({
                    'id': b.get('id'),
                    'title': b.get('title', ''),
                    'authors': b.get('authors', ''),
                    'fileName': fname,
                    'source': label,
                })
        except Exception as e:
            logger.warning(f"Booklore search failed: {e}")

    return jsonify(results)


@bookfusion_bp.route('/api/bookfusion/upload', methods=['POST'])
def upload_book():
    """Upload a book from Booklore to BookFusion."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    book_id = data.get('book_id')
    title = data.get('title', '')
    authors = data.get('authors', '')
    filename = data.get('fileName', '')

    if not book_id:
        return jsonify({'error': 'book_id required'}), 400

    container = get_container()
    bf_client = container.bookfusion_client()

    if not bf_client.upload_api_key:
        return jsonify({'error': 'BookFusion upload API key not configured'}), 400

    bl_client = get_booklore_client()
    if not bl_client.is_configured():
        return jsonify({'error': 'Booklore not configured'}), 400

    # Download from Booklore
    file_bytes = bl_client.download_book(book_id)
    if not file_bytes:
        return jsonify({'error': 'Failed to download book from Booklore'}), 500

    # Upload to BookFusion
    logger.info(f"BookFusion upload request: title='{title}', authors='{authors}', filename='{filename}'")
    result = bf_client.upload_book(filename, file_bytes, title, authors)
    if result:
        return jsonify({'success': True, 'result': result})
    return jsonify({'error': 'Upload to BookFusion failed'}), 500


@bookfusion_bp.route('/api/bookfusion/sync-highlights', methods=['POST'])
def sync_highlights():
    """Trigger highlight sync from BookFusion."""
    container = get_container()
    bf_client = container.bookfusion_client()
    db_service = get_database_service()

    if not bf_client.highlights_api_key:
        return jsonify({'error': 'BookFusion highlights API key not configured'}), 400

    data = request.get_json(silent=True) or {}
    if data.get('full_resync'):
        db_service.set_bookfusion_sync_cursor('')

    try:
        result = bf_client.sync_all_highlights(db_service)
        matched = _auto_match_highlights(db_service)
        return jsonify({
            'success': True,
            'new_highlights': result['new_highlights'],
            'books_saved': result['books_saved'],
            'auto_matched': matched,
        })
    except Exception:
        logger.exception("BookFusion highlight sync failed")
        return jsonify({'error': 'BookFusion highlight sync failed'}), 500


STRIP_EXTENSIONS = re.compile(r'\.(epub|mobi|azw3?|pdf|fb2|cbz|cbr|md)$', re.IGNORECASE)


def _normalize_title(title: str) -> str:
    """Normalize a title for matching: strip extensions, lowercase, collapse whitespace."""
    t = STRIP_EXTENSIONS.sub('', title)
    return ' '.join(t.lower().split())


def _auto_match_highlights(db_service) -> int:
    """Auto-match unlinked BookFusion highlights to PageKeeper books by title similarity."""
    unmatched = db_service.get_unmatched_bookfusion_highlights()
    if not unmatched:
        return 0

    books = db_service.get_all_books()
    if not books:
        return 0

    # Build normalized title → abs_id list map (detect ambiguous duplicates)
    book_map: dict[str, list[str]] = defaultdict(list)
    for b in books:
        if b.abs_title:
            norm = _normalize_title(b.abs_title)
            book_map[norm].append(b.abs_id)

    # Group unmatched by book_title
    title_groups: dict[str, list] = {}
    for hl in unmatched:
        title = _clean_book_title(hl.book_title or '')
        title_groups.setdefault(title, []).append(hl)

    matched_count = 0
    norm_keys = list(book_map.keys())

    for bf_title, highlights in title_groups.items():
        norm_bf = _normalize_title(bf_title)
        abs_id = None

        # Exact match (only if unambiguous)
        if norm_bf in book_map and len(book_map[norm_bf]) == 1:
            abs_id = book_map[norm_bf][0]
        else:
            # Fuzzy match (only if unambiguous)
            candidates = []
            for norm_pk in norm_keys:
                if len(book_map[norm_pk]) != 1:
                    continue
                ratio = difflib.SequenceMatcher(None, norm_bf, norm_pk).ratio()
                if ratio > 0.85:
                    candidates.append((ratio, book_map[norm_pk][0]))
            if candidates:
                candidates.sort(key=lambda c: c[0], reverse=True)
                if len(candidates) == 1 or (candidates[0][0] - candidates[1][0]) >= 0.05:
                    abs_id = candidates[0][1]

        if abs_id:
            bf_ids = {hl.bookfusion_book_id for hl in highlights if hl.bookfusion_book_id}
            for bf_id in bf_ids:
                db_service.link_bookfusion_book(bf_id, abs_id)
            matched_count += len(highlights)

    return matched_count


def _clean_book_title(title: str) -> str:
    """Strip .md suffix and wiki-link artifacts from book titles."""
    if title.endswith('.md'):
        title = title[:-3]
    return title.strip()


def _estimate_reading_dates(db_service, abs_id: str, bookfusion_ids: list[str], title: str) -> dict:
    """Attempt to set reading dates on a newly-linked book. Returns date info for the response."""
    book = db_service.get_book(abs_id)
    if not book or book.started_at or book.finished_at:
        return {}

    container = get_container()
    started_at = None
    finished_at = None
    source = None
    estimated = False

    # Priority 1: Hardcover (actual dates)
    try:
        hc_client = container.hardcover_client()
        if hc_client.is_configured():
            hc_details = db_service.get_hardcover_details(abs_id)
            user_book = None
            if hc_details and hc_details.hardcover_book_id:
                try:
                    user_book = hc_client.find_user_book(int(hc_details.hardcover_book_id))
                except (ValueError, TypeError):
                    logger.debug("Invalid hardcover_book_id: %s", hc_details.hardcover_book_id)
                    user_book = None
            elif title:
                search_result = hc_client.search_by_title_author(title)
                if search_result:
                    # Persist HardcoverDetails so the cover URL and link survive
                    if not hc_details:
                        new_details = HardcoverDetails(
                            abs_id=abs_id,
                            hardcover_book_id=str(search_result['book_id']),
                            hardcover_slug=search_result.get('slug'),
                            hardcover_cover_url=search_result.get('cached_image'),
                            matched_by='title',
                        )
                        db_service.save_hardcover_details(new_details)
                    user_book = hc_client.find_user_book(search_result['book_id'])
            if user_book:
                reads = user_book.get('user_book_reads', [])
                if reads:
                    read = reads[0]
                    if read.get('started_at'):
                        started_at = read['started_at']
                    if read.get('finished_at'):
                        finished_at = read['finished_at']
                    if started_at or finished_at:
                        source = 'hardcover'
    except Exception as e:
        logger.debug(f"Hardcover date lookup failed for '{abs_id}': {e}")

    # Priority 2: Highlight date range (estimated)
    if not source and bookfusion_ids:
        date_range = db_service.get_bookfusion_highlight_date_range(bookfusion_ids)
        if date_range:
            earliest, latest, count = date_range
            started_at = earliest.strftime('%Y-%m-%d') if earliest else None
            if count > 1 and latest:
                finished_at = latest.strftime('%Y-%m-%d')
            source = 'highlights'
            estimated = True

    if not source:
        return {}

    # Apply dates and status
    updates = {}
    if started_at:
        updates['started_at'] = started_at
    if finished_at:
        updates['finished_at'] = finished_at
    if updates:
        updated_book = db_service.update_book_reading_fields(abs_id, **updates)
        if updated_book:
            book = updated_book

    # Update status
    if finished_at:
        book.status = 'completed'
    elif started_at:
        book.status = 'active'
    if book.status != 'not_started':
        db_service.save_book(book)

    return {
        'dates_set': True,
        'dates_source': source,
        'dates_estimated': estimated,
        'started_at': started_at,
        'finished_at': finished_at,
    }


@bookfusion_bp.route('/api/bookfusion/highlights')
def get_highlights():
    """Return cached highlights from DB, grouped by book."""
    db_service = get_database_service()
    highlights = db_service.get_bookfusion_highlights()

    grouped = {}
    for hl in highlights:
        key = (hl.bookfusion_book_id, hl.matched_abs_id, _clean_book_title(hl.book_title or 'Unknown Book'))
        if key not in grouped:
            grouped[key] = {
                'highlights': [],
                'matched_abs_id': hl.matched_abs_id,
                'bookfusion_book_id': hl.bookfusion_book_id,
                'display_title': _clean_book_title(hl.book_title or 'Unknown Book'),
            }
        date_str = hl.highlighted_at.strftime('%Y-%m-%d %H:%M:%S') if hl.highlighted_at else None
        grouped[key]['highlights'].append({
            'id': hl.id,
            'quote': hl.quote_text or hl.content,
            'date': date_str,
            'chapter_heading': hl.chapter_heading,
            'matched_abs_id': hl.matched_abs_id,
        })

    # Sort highlights within each book by date
    for key in grouped:
        grouped[key]['highlights'].sort(key=lambda h: h['date'] or '', reverse=True)

    # Re-key by display title for the frontend (API contract uses title as key)
    display = {}
    for _key, group in grouped.items():
        title = group.pop('display_title')
        # Disambiguate if two different books share the same cleaned title
        display_key = title
        if display_key in display:
            display_key = f"{title} ({group['bookfusion_book_id']})"
        display[display_key] = group

    cursor = db_service.get_bookfusion_sync_cursor()

    # Include list of PageKeeper books for journal matching
    books = db_service.get_all_books()
    book_list = [{'abs_id': b.abs_id, 'title': b.abs_title} for b in books if b.abs_title]

    return jsonify({'highlights': display, 'has_synced': cursor is not None, 'books': book_list})


@bookfusion_bp.route('/api/bookfusion/link-highlight', methods=['POST'])
def link_highlight():
    """Manually link or unlink a BookFusion book's highlights to a PageKeeper book."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    bookfusion_book_id = data.get('bookfusion_book_id')
    abs_id = data.get('abs_id')  # None or empty to unlink

    if not bookfusion_book_id:
        return jsonify({'error': 'bookfusion_book_id required'}), 400

    db_service = get_database_service()
    db_service.link_bookfusion_book(bookfusion_book_id, abs_id or None)
    return jsonify({'success': True})


@bookfusion_bp.route('/api/bookfusion/save-journal', methods=['POST'])
def save_highlight_to_journal():
    """Save BookFusion highlights as reading journal entries for a book."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    abs_id = data.get('abs_id')
    highlights = data.get('highlights', [])

    if not abs_id:
        return jsonify({'error': 'abs_id required'}), 400
    if not highlights:
        return jsonify({'error': 'No highlights provided'}), 400

    db_service = get_database_service()
    book = db_service.get_book(abs_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    cleanup_stats = db_service.cleanup_bookfusion_import_notes(abs_id)
    saved = 0
    for hl in highlights:
        quote = hl.get('quote', '').strip()
        chapter = hl.get('chapter', '')
        highlighted_at_raw = (hl.get('highlighted_at') or '').strip()
        if not quote:
            continue
        entry = quote
        if chapter:
            entry += f"\n— {chapter}"
        created_at = None
        if highlighted_at_raw:
            try:
                created_at = datetime.strptime(highlighted_at_raw, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                logger.debug("Could not parse BookFusion highlight timestamp '%s'", highlighted_at_raw)
        try:
            db_service.add_reading_journal(abs_id, 'highlight', entry=entry, created_at=created_at)
            saved += 1
        except Exception as e:
            logger.warning(f"Failed to save journal entry: {e}")

    return jsonify({'success': True, 'saved': saved, 'cleanup': cleanup_stats})


@bookfusion_bp.route('/api/bookfusion/library')
def get_library():
    """Return BookFusion library catalog for the Library tab, merging duplicate titles."""
    db_service = get_database_service()
    bf_books = db_service.get_bookfusion_books()

    # Check which books are already on the dashboard (by bf- prefix or highlight match)
    all_books = db_service.get_all_books()
    dashboard_ids = {b.abs_id for b in all_books}
    book_list = [{'abs_id': b.abs_id, 'title': b.abs_title} for b in all_books if b.abs_title]

    # Group by normalized title to merge format duplicates
    groups = defaultdict(list)
    for b in bf_books:
        norm = _normalize_title(b.title or b.filename or '')
        groups[norm].append(b)

    result = []
    for _norm_title, group in groups.items():
        # Pick the entry with the most highlights as the "primary" for metadata
        group.sort(key=lambda b: b.highlight_count or 0, reverse=True)
        primary = group[0]

        title = _clean_book_title(primary.title or primary.filename or '')
        authors = ''
        series = ''
        tags = ''
        for b in group:
            if not authors and b.authors:
                authors = b.authors
            if not series and b.series:
                series = b.series
            if not tags and b.tags:
                tags = b.tags

        filenames = list(dict.fromkeys(b.filename for b in group if b.filename))
        bookfusion_ids = [b.bookfusion_id for b in group]
        highlight_count = sum(b.highlight_count or 0 for b in group)

        # Check dashboard match across all entries in the group
        matched_abs_id = None
        for b in group:
            bf_abs_id = f"bf-{b.bookfusion_id}"
            if b.matched_abs_id and b.matched_abs_id in dashboard_ids:
                matched_abs_id = b.matched_abs_id
                break
            elif bf_abs_id in dashboard_ids:
                matched_abs_id = bf_abs_id
                break

        # A group is hidden if any entry in the group is hidden
        is_hidden = any(b.hidden for b in group)

        result.append({
            'bookfusion_id': bookfusion_ids[0],
            'bookfusion_ids': bookfusion_ids,
            'title': title,
            'authors': authors,
            'filenames': filenames,
            'filename': primary.filename or '',
            'series': series,
            'tags': tags,
            'highlight_count': highlight_count,
            'on_dashboard': matched_abs_id is not None,
            'abs_id': matched_abs_id,
            'hidden': is_hidden,
        })

    return jsonify({'books': result, 'dashboard_books': book_list})


@bookfusion_bp.route('/api/bookfusion/add-to-dashboard', methods=['POST'])
def add_to_dashboard():
    """Add a BookFusion book to the reading dashboard."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    bookfusion_ids = data.get('bookfusion_ids') or []
    if not bookfusion_ids:
        single = data.get('bookfusion_id')
        if single:
            bookfusion_ids = [single]
    if not bookfusion_ids:
        return jsonify({'error': 'bookfusion_id required'}), 400

    primary_id = bookfusion_ids[0]
    db_service = get_database_service()
    bf_book = db_service.get_bookfusion_book(primary_id)
    if not bf_book:
        return jsonify({'error': 'BookFusion book not found in catalog'}), 404

    abs_id = f"bf-{primary_id}"

    # Check if already on dashboard
    existing = db_service.get_book(abs_id)
    if existing:
        return jsonify({'success': True, 'abs_id': abs_id, 'already_existed': True})

    # Create dashboard book entry
    title = _clean_book_title(bf_book.title or bf_book.filename or 'Unknown')
    book = Book(
        abs_id=abs_id,
        abs_title=title,
        status='not_started',
        sync_mode='ebook_only',
    )
    db_service.save_book(book)

    # Auto-link ALL catalog books + highlights in the group
    for bid in bookfusion_ids:
        db_service.set_bookfusion_book_match(bid, abs_id)
        db_service.link_bookfusion_book(bid, abs_id)

    # Auto-populate reading dates
    date_info = _estimate_reading_dates(db_service, abs_id, bookfusion_ids, title)

    resp = {'success': True, 'abs_id': abs_id}
    resp.update(date_info)
    return jsonify(resp)


@bookfusion_bp.route('/api/bookfusion/match-to-book', methods=['POST'])
def match_to_book():
    """Match a BookFusion catalog book to an existing dashboard book (link highlights)."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    bookfusion_ids = data.get('bookfusion_ids') or []
    if not bookfusion_ids:
        single = data.get('bookfusion_id')
        if single:
            bookfusion_ids = [single]
    abs_id = data.get('abs_id')  # None/empty to unlink

    if not bookfusion_ids:
        return jsonify({'error': 'bookfusion_id required'}), 400

    db_service = get_database_service()

    if abs_id and not db_service.get_book(abs_id):
        return jsonify({'error': 'Book not found'}), 404

    # Link ALL catalog books + highlights in the group
    for bid in bookfusion_ids:
        db_service.set_bookfusion_book_match(bid, abs_id or None)
        db_service.link_bookfusion_book(bid, abs_id or None)

    resp = {'success': True, 'abs_id': abs_id}

    # Auto-populate reading dates if linking (not unlinking)
    if abs_id:
        book = db_service.get_book(abs_id)
        title = book.abs_title if book else ''
        date_info = _estimate_reading_dates(db_service, abs_id, bookfusion_ids, title)
        resp.update(date_info)

    return jsonify(resp)


@bookfusion_bp.route('/api/bookfusion/hide', methods=['POST'])
def hide_book():
    """Hide or unhide a BookFusion library book."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    bookfusion_ids = data.get('bookfusion_ids') or []
    if not bookfusion_ids:
        single = data.get('bookfusion_id')
        if single:
            bookfusion_ids = [single]
    if not bookfusion_ids:
        return jsonify({'error': 'bookfusion_id required'}), 400

    hidden = data.get('hidden', True)
    db_service = get_database_service()
    db_service.set_bookfusion_books_hidden(bookfusion_ids, hidden)
    return jsonify({'success': True})


@bookfusion_bp.route('/api/bookfusion/unlink', methods=['POST'])
def unlink_book():
    """Unlink a BookFusion book from a dashboard book by abs_id."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    abs_id = data.get('abs_id')
    if not abs_id:
        return jsonify({'error': 'abs_id required'}), 400

    db_service = get_database_service()
    db_service.unlink_bookfusion_by_abs_id(abs_id)
    return jsonify({'success': True})
